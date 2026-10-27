#!/usr/bin/env python3
# /opt/alert-dedup/dedup_proxy.py
# Alertmanager -> (本中间件: 去重/限流) -> prometheus-webhook-dingtalk
# 仅用 Python3 标准库，无第三方依赖。
#
# 策略：
#   1) 去重：同一告警(alertname+instance+name+device+job+status)在 cooldown 秒内只发一次。
#      status 参与指纹，保证 firing 与 resolved 各自只发一次，恢复通知不会被去重吞掉。
#   2) 限流：同一 alertname 在 window 秒内最多发 max_messages 条消息。
#   3) 截断：单条消息最多 max_alerts_per_message 条告警(其余一并视为已发, 不再补发)。
import json
import os
import time
import gzip
import threading
import hashlib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF_PATH = os.environ.get("DEDUP_CONF", "/opt/alert-dedup/config.json")

with open(CONF_PATH, "r", encoding="utf-8") as f:
    CONF = json.load(f)

LISTEN_HOST = CONF.get("listen_host", "0.0.0.0")
LISTEN_PORT = int(CONF.get("listen_port", 8065))
UPSTREAM = CONF["upstream"]
COOLDOWN = int(CONF.get("cooldown_seconds", 21600))
MAX_ALERTS = int(CONF.get("max_alerts_per_message", 3))
RL = CONF.get("rate_limit", {})
RL_MAX = int(RL.get("max_messages", 3))
RL_WINDOW = int(RL.get("window_seconds", 3600))
STATE_PATH = CONF.get("state_file", "/opt/alert-dedup/state.json")

LOCK = threading.Lock()
STATE = {"dedup": {}, "rate": {}}   # dedup: fp -> last_sent_ts ; rate: alertname -> [ts,...]


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            STATE = json.load(f)
    except Exception:
        STATE = {"dedup": {}, "rate": {}}
    STATE.setdefault("dedup", {})
    STATE.setdefault("rate", {})


def save_state():
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f)
    os.replace(tmp, STATE_PATH)


def fingerprint(alert):
    l = alert.get("labels", {})
    key = "|".join(str(l.get(k, "")) for k in
                   ("alertname", "instance", "name", "device", "job"))
    key += "|status=" + str(alert.get("status", ""))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def filter_alerts(alerts, now):
    """返回 (to_send, passed, stat)
    to_send : 实际要发出的（已按 MAX_ALERTS 截断）
    passed  : 通过“限流+去重”的全部告警（都要标记已发，含被截断的）
    """
    stat = {"dedup_skip": 0, "rate_skip": 0, "cap_skip": 0}
    rate_hit = set()
    for name, ts_list in STATE["rate"].items():
        ts_list = [t for t in ts_list if now - t < RL_WINDOW]
        STATE["rate"][name] = ts_list
        if len(ts_list) >= RL_MAX:
            rate_hit.add(name)

    passed = []
    for a in alerts:
        name = a.get("labels", {}).get("alertname", "?")
        fp = fingerprint(a)
        if name in rate_hit:
            stat["rate_skip"] += 1
            continue
        last = STATE["dedup"].get(fp)
        if last is not None and now - last < COOLDOWN:
            stat["dedup_skip"] += 1
            continue
        passed.append((fp, name, a))

    to_send = passed[:MAX_ALERTS]
    stat["cap_skip"] = len(passed) - len(to_send)
    return to_send, passed, stat


def forward(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(UPSTREAM, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"status": "ok", "upstream": UPSTREAM})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            if (self.headers.get("Content-Encoding", "") or "").lower().find("gzip") >= 0:
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            log("bad request:", e)
            self._json(400, {"error": "bad payload"})
            return

        alerts = payload.get("alerts", []) or []
        now = time.time()
        with LOCK:
            to_send, passed, stat = filter_alerts(alerts, now)
            if not to_send:
                log("suppressed all:", stat)
                self._json(200, {"status": "suppressed", **stat})
                return
            # 标记“通过”的全部告警已发（含被截断的），避免下次补发
            for fp, name, _ in passed:
                STATE["dedup"][fp] = now
            for name in {n for _, n, _ in passed}:
                STATE["rate"].setdefault(name, []).append(now)
            save_state()

        payload["alerts"] = [a for _, _, a in to_send]
        try:
            forward(payload)
            log("forwarded", len(to_send), "alerts;", stat)
            self._json(200, {"status": "forwarded", "count": len(to_send), **stat})
        except Exception as e:
            # 转发失败：回滚标记，让 alertmanager 重试
            with LOCK:
                for fp, name, _ in passed:
                    STATE["dedup"].pop(fp, None)
                for name in {n for _, n, _ in passed}:
                    if STATE["rate"].get(name):
                        STATE["rate"][name].pop()
                save_state()
            log("forward failed:", e)
            self._json(502, {"error": "upstream failed"})

    def log_message(self, *a):
        pass


def main():
    load_state()
    log("start; listen %s:%d -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM))
    log("cooldown=%ss max_alerts=%d rate=%d/%ss" % (COOLDOWN, MAX_ALERTS, RL_MAX, RL_WINDOW))
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
