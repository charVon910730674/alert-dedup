# alert-dedup

Alertmanager → 钉钉 webhook 之间的轻量去重/限流代理。

`prometheus-webhook-dingtalk` 本身没有去重和限流能力：同一条告警在恢复前会按
`repeat_interval` 反复推送，多实例告警还会在一条消息里堆叠。这个代理夹在
Alertmanager 与 webhook 之间，负责把重复、超频、超量的通知挡掉。

## 架构

```
Alertmanager ──POST──> alert-dedup :8065 ──POST──> prometheus-webhook-dingtalk :8060 ──> 钉钉机器人
```

本服务只依赖 Python 3 标准库，无第三方包。

## 功能

- **去重**：相同 `(alertname, instance, name, device, job, status)` 在
  `cooldown_seconds` 内只转发一次。`status` 参与指纹，因此 `firing` 与
  `resolved` 各自计数，恢复通知不会被当成重复丢弃。
- **限流**：同一 `alertname` 在 `window_seconds` 内最多转发
  `max_messages` 条消息。
- **截断**：单条消息最多携带 `max_alerts_per_message` 个告警；被截断的告警
  也标记为已发送，不在后续消息中补发。
- **失败回滚**：转发上游失败时撤销本次去重/限流标记并返回 `502`，
  Alertmanager 会按自己的重试策略重发。
- **状态持久化**：去重与限流状态写入 `state.json`，服务重启不丢失。

## 快速开始

要求：Linux、Python 3.8+、systemd。

```bash
git clone git@github.com:charVon910730674/alert-dedup.git
cd alert-dedup
sudo ./scripts/install.sh
```

安装脚本会：

1. 复制程序到 `/opt/alert-dedup/`；
2. 首次安装时用 `config.example.json` 生成 `config.json`（已存在则备份后保留）；
3. 安装 `systemd/alert-dedup.service` 并 `enable + restart`。

卸载（保留配置和状态）：

```bash
sudo ./scripts/uninstall.sh
```

彻底删除（含 `/opt/alert-dedup`）：

```bash
sudo ./scripts/uninstall.sh --purge
```

## 配置

默认读取 `/opt/alert-dedup/config.json`，可用环境变量 `DEDUP_CONF` 覆盖路径。

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `0.0.0.0` | 监听地址，建议只绑内网/回环 |
| `listen_port` | `8065` | 监听端口 |
| `upstream` | `http://127.0.0.1:8060/dingtalk/webhook1/send` | prometheus-webhook-dingtalk 地址 |
| `cooldown_seconds` | `21600` | 同指纹告警的去重窗口（6 小时） |
| `max_alerts_per_message` | `3` | 单条消息最多告警数 |
| `rate_limit.max_messages` | `3` | 同 alertname 在窗口内最多消息数 |
| `rate_limit.window_seconds` | `3600` | 限流窗口（1 小时） |
| `state_file` | `/opt/alert-dedup/state.json` | 状态文件路径 |

`state.json` 由程序自动维护，**不要**提交到仓库。

## Alertmanager 配置示例

```yaml
route:
  group_by: ['alertname']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 8h
  receiver: 'web.hook'

receivers:
- name: 'web.hook'
  webhook_configs:
  - url: 'http://<alert-dedup-host>:8065/dingtalk/webhook1/send'
    send_resolved: true
    max_alerts: 3
```

`send_resolved: true` 是恢复通知能送达的前提。

## 运维

```bash
systemctl status alert-dedup
journalctl -u alert-dedup -f
systemctl restart alert-dedup
curl http://127.0.0.1:8065/          # {"status":"ok","upstream":"..."}
```

日志语义：

- `forwarded N alerts; {'dedup_skip':0,'rate_skip':0,'cap_skip':0}`：已转发；
- `suppressed all: {...}`：全部被去重/限流/截断挡下；
- `forward failed: ...`：上游失败，标记已回滚。

回滚到旧版本时，恢复 `/opt/alert-dedup/dedup_proxy.py` 备份并
`systemctl restart alert-dedup` 即可。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 已知限制

- `state.json` 中的去重条目只增不删（过期后不再命中，但文件会缓慢增长）。
  条目很小，长期运行可定期清理或后续加入 GC。
- 状态保存在单机文件，不支持多实例共享；同一 alertname 的限流是进程内语义。
- 服务本身不做鉴权，请只监听回环或可信内网，不要直接暴露公网。
- 上游返回非 2xx 时整批回滚；Alertmanager 重试会重新经过去重判断。

## 变更记录

- **v1.1** 2026-10-27：`status` 纳入去重指纹，修复 `resolved` 通知被
  `firing` 的去重记录吞掉的问题。
- **v1.0** 2026-10-12：初始版本，支持去重、限流、单消息截断与状态持久化。
