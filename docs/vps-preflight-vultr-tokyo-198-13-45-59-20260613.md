# Aurum VPS Preflight Report — Vultr Tokyo 198.13.45.59

Timestamp: 2026-06-13 22:27 CST / 2026-06-13 14:27 UTC

## Verdict

`pass` — the Vultr Tokyo node is acceptable for Aurum Phase 0/1 recorder preflight.

## Scope

Read-only access and network checks only:

- SSH key authentication as `root`;
- host snapshot (`whoami`, `hostname`, `uname`, `python3 --version`, `df`, `free`, `date`);
- copied the read-only preflight script to `/tmp/aurum-vps-preflight.py`;
- ran `python3 /tmp/aurum-vps-preflight.py --json --timeout 12` three times;
- no package installation;
- no service deployment;
- no firewall changes;
- no credentials, wallet, private key, API key, or trading action.

## Host snapshot

- IP: `198.13.45.59`
- User: `root`
- Hostname: `aurum-tokyo-01`
- Kernel: `Linux aurum-tokyo-01 6.8.0-124-generic #124-Ubuntu SMP PREEMPT_DYNAMIC Tue May 26 13:00:45 UTC 2026 x86_64`
- Python: `Python 3.12.3`
- Disk `/`: `52G` total, `8.8G` used, `40G` available, `19%` used
- Memory: `1.9Gi` total, `1.6Gi` available
- Swap: `5.3Gi` total, `0B` used

## SSH result

- TCP `198.13.45.59:22`: reachable
- SSH key login: successful
- Local key used: `/Users/dracoglasser/.ssh/aurum_vultr_20260613_ed25519`

## Preflight runs

### Run 1

- verdict: `pass`
- OK: `4/4`
- HTTPS OK: `true`
- WebSocket OK: `true`
- worst OK latency: `605 ms`
- `gamma-api.polymarket.com`: HTTP `200`, `86 ms`
- `clob.polymarket.com`: HTTP `200`, `85 ms`
- `data-api.polymarket.com`: HTTP `200`, `82 ms`
- `ws-subscriptions-clob.polymarket.com`: `HTTP/1.1 101 Switching Protocols`, `605 ms`

### Run 2

- verdict: `pass`
- OK: `4/4`
- HTTPS OK: `true`
- WebSocket OK: `true`
- worst OK latency: `591 ms`
- `gamma-api.polymarket.com`: HTTP `200`, `89 ms`
- `clob.polymarket.com`: HTTP `200`, `84 ms`
- `data-api.polymarket.com`: HTTP `200`, `89 ms`
- `ws-subscriptions-clob.polymarket.com`: `HTTP/1.1 101 Switching Protocols`, `591 ms`

### Run 3

- verdict: `pass`
- OK: `4/4`
- HTTPS OK: `true`
- WebSocket OK: `true`
- worst OK latency: `609 ms`
- `gamma-api.polymarket.com`: HTTP `200`, `84 ms`
- `clob.polymarket.com`: HTTP `200`, `319 ms`
- `data-api.polymarket.com`: HTTP `200`, `82 ms`
- `ws-subscriptions-clob.polymarket.com`: `HTTP/1.1 101 Switching Protocols`, `609 ms`

## Interpretation

This node passes the current Aurum acceptance gate:

- HTTPS endpoints passed for three consecutive runs;
- WebSocket opening handshake passed for three consecutive runs;
- no DNS override was needed;
- worst successful latency was under one second;
- free disk is about `40G`, enough for Phase 0/1 with rotation/data management.

Next deployment should keep Phase 0/1 boundaries: public data only, local paper simulation, no wallet/private key/live orders.
