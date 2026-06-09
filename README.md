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
