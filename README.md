# Aurum

Aurum is a research-only paper-trading and market-microstructure recorder for Polymarket CLOB markets.

It records public orderbook data, replays strategy decisions, and evaluates simulated fills, cancels, partial fills, post-only behavior, rate limits, and risk controls before any live trading.

## Purpose

Aurum is designed to test prediction-market execution ideas safely before any live funds or real order placement.

The project focuses on:

- public CLOB orderbook recording;
- market microstructure replay;
- paper-trading journals;
- simulated order lifecycle analysis;
- strategy/risk evaluation;
- official sandbox or paper CLOB integration if available.

## Sandbox access goal

Aurum is seeking the closest official simulated environment for Polymarket execution testing, including:

- order validation;
- simulated fills and partial fills;
- cancel behavior;
- post-only behavior;
- taker delay;
- rate limits;
- user-channel lifecycle events.

## Safety boundary

Aurum is research-only at this stage.

It does not require live funds, a real trading wallet, private keys, USDC deposits, or live order placement.

## Status

Early builder/research prototype.

## Current local-first path

Polymarket Builder/sandbox access is not required for Phase 0/1. While waiting for official access, Aurum should run a local paper account over public market/orderbook data.

- Local paper account rules: [`docs/local-paper-account-rules.md`](docs/local-paper-account-rules.md)
- Deployment architecture: [`docs/deployment-architecture.md`](docs/deployment-architecture.md)
- VPS preflight runbook: [`docs/vps-preflight.md`](docs/vps-preflight.md)
- Guangzhou ECS preflight result: [`docs/vps-preflight-ailcloud-esc-20260613.md`](docs/vps-preflight-ailcloud-esc-20260613.md)
- VPS provider shortlist: [`docs/vps-provider-shortlist.md`](docs/vps-provider-shortlist.md)
- DeepSeek/SuperWing paper-wallet duel: [`docs/deepseek-agent-duel.md`](docs/deepseek-agent-duel.md)
- Bitcoin-only trading terminal dashboard v2: [`docs/bitcoin-terminal-dashboard-v2.md`](docs/bitcoin-terminal-dashboard-v2.md)
- Paper duel scheduling and VPS timer: [`docs/paper-duel-scheduling.md`](docs/paper-duel-scheduling.md)
- Public dashboard and 5h strategy review: [`docs/public-dashboard-and-strategy-review.md`](docs/public-dashboard-and-strategy-review.md)
- Polymarket-style paper execution rules and taker-fee model: [`docs/polymarket-paper-execution-rules.md`](docs/polymarket-paper-execution-rules.md)
