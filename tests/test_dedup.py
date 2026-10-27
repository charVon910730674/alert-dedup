#!/usr/bin/env python3
"""alert-dedup 核心过滤逻辑单元测试（标准库 unittest）。"""

import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp(prefix="alert-dedup-test-"))
CONF_PATH = TMP / "config.json"
CONF_PATH.write_text(
    json.dumps(
        {
            "listen_host": "127.0.0.1",
            "listen_port": 18065,
            "upstream": "http://127.0.0.1:18060/send",
            "cooldown_seconds": 21600,
            "max_alerts_per_message": 3,
            "rate_limit": {"max_messages": 3, "window_seconds": 3600},
            "state_file": str(TMP / "state.json"),
        }
    ),
    encoding="utf-8",
)
os.environ["DEDUP_CONF"] = str(CONF_PATH)

SPEC = importlib.util.spec_from_file_location("dedup_proxy", ROOT / "dedup_proxy.py")
dedup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dedup)


def alert(name="TestAlert", status="firing", instance="host-1"):
    return {
        "status": status,
        "labels": {
            "alertname": name,
            "instance": instance,
            "job": "node_exporter",
            "dev": "test",
        },
        "annotations": {"summary": "test"},
    }


class FingerprintTest(unittest.TestCase):
    def test_status_is_part_of_fingerprint(self):
        firing = alert(status="firing")
        resolved = alert(status="resolved")
        self.assertNotEqual(dedup.fingerprint(firing), dedup.fingerprint(resolved))

    def test_same_status_same_fingerprint(self):
        self.assertEqual(dedup.fingerprint(alert()), dedup.fingerprint(alert()))


class FilterTest(unittest.TestCase):
    def setUp(self):
        dedup.STATE = {"dedup": {}, "rate": {}}
        self.now = time.time()

    def test_firing_is_suppressed_within_cooldown(self):
        a = alert()
        dedup.STATE["dedup"][dedup.fingerprint(a)] = self.now
        to_send, passed, stat = dedup.filter_alerts([a], self.now)
        self.assertEqual(to_send, [])
        self.assertEqual(passed, [])
        self.assertEqual(stat["dedup_skip"], 1)

    def test_resolved_passes_after_firing(self):
        firing = alert(status="firing")
        resolved = alert(status="resolved")
        dedup.STATE["dedup"][dedup.fingerprint(firing)] = self.now
        to_send, passed, stat = dedup.filter_alerts([resolved], self.now)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(len(passed), 1)
        self.assertEqual(stat["dedup_skip"], 0)

    def test_fingerprint_expires_after_cooldown(self):
        a = alert()
        dedup.STATE["dedup"][dedup.fingerprint(a)] = self.now - dedup.COOLDOWN - 1
        to_send, _, stat = dedup.filter_alerts([a], self.now)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(stat["dedup_skip"], 0)

    def test_rate_limit_per_alertname(self):
        dedup.STATE["rate"]["TestAlert"] = [self.now] * dedup.RL_MAX
        to_send, passed, stat = dedup.filter_alerts([alert()], self.now)
        self.assertEqual(to_send, [])
        self.assertEqual(passed, [])
        self.assertEqual(stat["rate_skip"], 1)

    def test_message_cap_marks_truncated_as_passed(self):
        alerts = [alert(name="A%d" % i, instance="host-%d" % i) for i in range(5)]
        to_send, passed, stat = dedup.filter_alerts(alerts, self.now)
        self.assertEqual(len(to_send), dedup.MAX_ALERTS)
        self.assertEqual(len(passed), 5)
        self.assertEqual(stat["cap_skip"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
