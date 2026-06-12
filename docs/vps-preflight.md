# VPS Preflight Runbook for Aurum

## Current buying decision

Do **not** buy a new Hong Kong VPS before testing the existing Guangzhou server.

Use this gate:

1. Test the existing Guangzhou machine first.
2. If HTTPS public endpoints pass and latency is acceptable, use Guangzhou for the first polling recorder.
3. If WebSocket also passes, Guangzhou can run the fuller CLOB recorder.
4. If HTTPS/WebSocket fail or are unstable, buy a Hong Kong/Singapore/Japan/US/EU VPS.

The Alibaba Cloud Hong Kong lightweight server shown by Queen Glasser is acceptable if needed:

- region: China (Hong Kong);
- size: 2 vCPU / 2 GB RAM / 40 GB ESSD;
- bandwidth: 200 Mbps peak;
- public IPv4: 1;
- price: ¥56/month.

However, do not use the OpenClaw app image for Aurum. Choose a boring Linux image such as Debian 12 or Ubuntu 24.04 LTS.

## Why Guangzhou is worth testing first

Aurum Phase 0/1 is read-only and cheap:

- public Gamma/CLOB/Data API polling;
- local paper account simulation;
- SQLite/DuckDB/Postgres ledger;
- daily reports;
- no wallet;
- no private keys;
- no live orders.

A Guangzhou server is fine if it can reach the endpoints reliably. If it cannot, region/provider changes come before code changes.

## Preflight script

Local repo script:

```bash
/Users/dracoglasser/workspace/aurum/scripts/vps_network_preflight.py
```

It tests:

- `https://gamma-api.polymarket.com/markets?limit=1`
- `https://clob.polymarket.com/markets?limit=1`
- `https://data-api.polymarket.com/trades?limit=1`
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` WebSocket opening handshake

It is read-only: no login, no credentials, no orders.

## How to run on the Guangzhou server

From Draco Mac, after SSH access is available:

```bash
scp /Users/dracoglasser/workspace/aurum/scripts/vps_network_preflight.py USER@GUANGZHOU_SERVER:/tmp/aurum-vps-preflight.py
ssh USER@GUANGZHOU_SERVER 'python3 /tmp/aurum-vps-preflight.py --json'
```

If SSH uses a key or non-standard port:

```bash
scp -P PORT -i /path/to/key /Users/dracoglasser/workspace/aurum/scripts/vps_network_preflight.py USER@GUANGZHOU_SERVER:/tmp/aurum-vps-preflight.py
ssh -p PORT -i /path/to/key USER@GUANGZHOU_SERVER 'python3 /tmp/aurum-vps-preflight.py --json'
```

## SSH information needed

Preferred safe access pattern:

- server public IP or hostname;
- SSH port;
- username, ideally a non-root sudo user;
- temporary SSH public-key access for Draco;
- no password pasted into chat;
- no cloud console password screenshots.

If only password SSH is available, use it once to install Queen Glasser's public key, then disable password login later.

## Interpreting results

### pass

All HTTPS + WebSocket checks passed.

Use the server for recorder + paper engine.

### partial_https_only

HTTPS passed, WebSocket failed.

Acceptable for a first polling-only recorder. Not ideal for full microstructure capture.

Next step:

- run repeated tests at different times;
- if WebSocket remains blocked, use Hong Kong/Singapore/Japan/US/EU VPS for the WebSocket recorder.

### fail

One or more HTTPS public endpoints failed.

Do not deploy Aurum there. Change region/provider before changing trading logic.

## Acceptance threshold for first deployment

For the first 24/7 recorder:

- HTTPS endpoints pass for 3 consecutive runs;
- worst successful HTTPS latency is preferably under 3 seconds, tolerable under 8 seconds for Phase 0;
- no TLS/DNS resets;
- WebSocket pass is strongly preferred but not required for the first polling prototype;
- system clock/NTP is correct;
- disk has at least 40 GB free after OS.

## Recommendation

1. Test Guangzhou first because it costs nothing extra.
2. If Guangzhou is `pass`, use it.
3. If Guangzhou is `partial_https_only`, start polling there while looking for a cheap non-mainland VPS for WebSocket capture.
4. If Guangzhou is `fail`, buy the ¥56/month Hong Kong server or a cheaper reputable VPS.

Do not buy random ultra-cheap VPS from unknown resellers if the goal is continuous financial-data capture. Uptime and clean networking matter more than saving ¥10–20/month.
