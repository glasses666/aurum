# Polymarket Market Recorder

Aurum's recorder is separate from the paper bot loop. It captures public Polymarket market data continuously, then the paper engine consumes the latest normalized recorder frame.

## Service split

```text
aurum-market-recorder.service
  -> scripts/run_market_recorder.sh
  -> scripts/market_recorder.py
  -> /opt/aurum/data/raw/polymarket/YYYY-MM-DD/*.jsonl
  -> /opt/aurum/data/normalized/polymarket/latest_markets.json
  -> /opt/aurum/data/reports/market_recorder_health.json

aurum-bot-loop.service
  -> reads /opt/aurum/data/normalized/polymarket/latest_markets.json when fresh
  -> falls back to direct fetch only if recorder data is missing/stale
  -> writes paper decisions/fills/ledger under /opt/aurum/data/paper_duel
```

## Captured public sources

- Gamma public market/search frame
- CLOB markets frame
- Data API trades frame
- CLOB orderbook frame per selected token id

The recorder is read-only. It does not authenticate, place orders, use wallets, read private keys, or require live funds. The systemd unit intentionally does **not** load `/opt/aurum/.env`; it runs with recorder-only `Environment=` values under the unprivileged `aurum` user.

## Raw storage

Per UTC day:

```text
/opt/aurum/data/raw/polymarket/YYYY-MM-DD/
  gamma_markets.jsonl
  clob_markets.jsonl
  data_trades.jsonl
  clob_books.jsonl
  manifest.jsonl
```

Each frame includes:

- `ts`
- `source`
- `url`
- `ok/status/elapsed_ms`
- `payload_sha256`
- raw `payload`

`manifest.jsonl` is hash-chained with:

- source frame path
- frame line hash
- payload hash
- previous manifest hash
- current manifest hash

This makes the capture stream append-only and replay/audit friendly.

## Normalized handoff

The paper engine reads:

```text
/opt/aurum/data/normalized/polymarket/latest_markets.json
```

This file contains the simplified market shape used by mechanical bot scripts:

```json
{
  "ts": "...",
  "source": "polymarket_market_recorder_v0",
  "markets": [
    {
      "market_id": "...",
      "question": "...",
      "outcomes": [
        {"name": "Yes", "price": 0.42, "token_id": "..."},
        {"name": "No", "price": 0.58, "token_id": "..."}
      ]
    }
  ]
}
```

## Health

Health summary:

```text
/opt/aurum/data/reports/market_recorder_health.json
```

Watchdog/maintenance/reporting must check:

- `aurum-market-recorder.service` active/enabled
- latest health age <= stale threshold
- Gamma/CLOB/Data frames present
- CLOB book frames present
- bot latest tick `market_source.source == polymarket_market_recorder_v0`

## Local smoke

```bash
python3 scripts/market_recorder.py --data-dir /tmp/aurum-recorder --once --max-books 2
python3 scripts/market_recorder.py --data-dir /tmp/aurum-recorder --health
```

## Backup requirements

Aurum stability backup must include:

- `/opt/aurum/data/raw`
- `/opt/aurum/data/normalized`
- `/opt/aurum/data/features`
- `/opt/aurum/data/reports`
- `/opt/aurum/data/paper_duel/snapshots`
- paper duel state/ticks/events/decisions
- market recorder journal tail
