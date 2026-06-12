# Aurum Deployment Architecture

## Verdict

Use a **hybrid deployment**:

1. **Cloud CPU node** for 24/7 data capture, paper-account simulation, scheduled reports, and alerts.
2. **Draco MacBook Pro** for development, manual analysis, local replay, dashboards, and small experiments.
3. **Ephemeral GPU training node** only when model training actually needs GPU compute.

Do not keep the main recorder only on the laptop. Do not train models on the same always-on recorder machine.

## Why not local-only

Local-only is acceptable for Phase 0 prototyping, but not for stable market recording:

- the laptop can sleep, move networks, switch proxies, or run out of foreground attention;
- local Clash/proxy rules can affect WebSocket/API behavior;
- historical orderbook capture needs continuous uptime;
- training jobs can contend with the recorder and corrupt latency/coverage measurements.

The Mac remains excellent for development and ML experiments because it has large unified memory, fast local storage, and good interactive tooling.

## Why not one big Aliyun box

A single large cloud box is tempting but wrong for this project:

- recorder workload is mostly CPU/network/storage and should be cheap, boring, and always on;
- model training is bursty and GPU-expensive;
- long-running GPU instances waste money when no training is active;
- mixing recorder + training makes outages and performance regressions harder to attribute.

Keep the recorder boring. Burst GPU separately.

## Recommended deployment split

### 1. Recorder / paper engine node

Role:

- public Gamma/CLOB/Data API polling;
- WebSocket/orderbook recording;
- local paper execution engine;
- risk ledger and daily reports;
- Telegram/Hermes alerts;
- no wallet, no private key, no live order placement.

Initial size:

- 2–4 vCPU;
- 4–8 GB RAM;
- 80–200 GB SSD depending on orderbook retention;
- Linux x86_64;
- Docker Compose or systemd services;
- UTC time sync enabled.

Region guidance:

- If using Alibaba Cloud, prefer **Hong Kong / Singapore / Japan** style international regions for Polymarket public API stability.
- Avoid mainland-only deployment for Polymarket data capture unless direct API/WebSocket reachability is tested first.
- If raw Polymarket latency becomes the bottleneck, a US/EU cloud node may beat Aliyun. Treat region as an empirical preflight, not a brand choice.

### 2. Draco local workstation

Role:

- repo development;
- schema migrations and replay tests;
- notebook analysis;
- lightweight ML experiments;
- dashboard/product work;
- manual review before any money-facing decision.

The Mac can also run a backup recorder for short windows, but it should not be the sole source of 24/7 market history.

### 3. Ephemeral training node

Role:

- train small financial models from curated features;
- run batch feature generation if the dataset becomes large;
- benchmark model candidates;
- export model artifacts back to object storage/repo releases.

Use only when needed. Shut it down after the job.

Good first model lane:

- feature engineering + tabular/time-series models first: Logistic Regression, LightGBM/XGBoost-style classifiers/regressors, calibration models;
- only move to neural/LLM fine-tuning after the ledger has enough clean labels and forward data;
- do not start by training an LLM from scratch.

## Data layout

Preferred shape:

- raw captures: append-only JSONL or compressed Parquet by date/source;
- normalized tables: Postgres or SQLite/DuckDB for Phase 0/1;
- reports: markdown artifacts committed or archived daily;
- model datasets: versioned feature snapshots, not ad-hoc notebooks only.

Suggested directories on the cloud node:

```text
/opt/aurum/
  app/                  # git checkout
  data/
    raw/                # raw API/ws captures, rotated
    normalized/         # sqlite/duckdb/parquet/postgres dumps
    features/           # training feature snapshots
    reports/            # generated reports
  logs/
  backups/
  .env                  # chmod 600, never committed
```

## Service boundaries

Keep these services separate:

- `market_recorder`: captures public market/orderbook/trade data;
- `paper_engine`: consumes forward data and simulates fills/risk;
- `reporter`: emits summaries and anomaly alerts;
- `trainer`: offline or scheduled batch job, not always running;
- `dashboard`: optional read-only UI.

No live trading service in Phase 0/1.

## Preflight before deploying to cloud

Check and record:

- SSH access works with key auth;
- OS, CPU arch, RAM, disk;
- Docker or Python runtime availability;
- outbound HTTPS to `gamma-api.polymarket.com`, `clob.polymarket.com`, and `data-api.polymarket.com`;
- WebSocket reachability if used;
- NTP/time sync;
- disk write speed and free space;
- systemd user/service ability;
- backup/rsync path back to Draco or object storage.

## Operational rules

- Store secrets only in `.env` with `chmod 600`.
- Do not store wallets or private keys on the recorder.
- Phase 0/1 uses public data + local paper simulation only.
- Training jobs must not block or degrade recorder uptime.
- Every model must be evaluated against forward-held-out data, not only historical replay.
- If cloud networking is unstable, switch region/provider before changing trading logic.

## First deployment target

Start with one small cloud CPU node and no GPU:

1. run the VPS network preflight in [`docs/vps-preflight.md`](vps-preflight.md);
2. if Queen Glasser's existing Guangzhou server passes HTTPS/WebSocket checks, use it first because it costs nothing extra;
3. if Guangzhou only passes HTTPS, it may still run the first polling recorder while a non-mainland WebSocket recorder is evaluated;
4. if Guangzhou fails HTTPS or is unstable, buy a Hong Kong/Singapore/Japan/US/EU VPS before changing trading logic;
5. deploy recorder;
6. collect continuous public data;
7. run paper engine;
8. produce daily reports;
9. after data quality is stable, schedule model-training experiments on separate hardware.

Conclusion: **cloud for uptime, local for development, ephemeral GPU for training**.
