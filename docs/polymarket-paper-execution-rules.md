# Polymarket-style paper execution rules

Aurum's paper duel must not score fantasy fills. This document records the execution assumptions used by the local ledger and public dashboard.

Source checked: Polymarket Trading Fees documentation, `https://docs.polymarket.com/trading/fees`.

## Current rule model

- Mode: paper-only, local ledger only.
- No wallet, no private key, no live CLOB credential, no real order.
- Current fills are modeled as immediate **taker** fills.
- Maker queue placement, maker rebates, partial queue priority, cancels, and official sandbox fills are not yet credited. They belong in recorder/order-manager v2.

## Fee formula

Polymarket's public fee docs describe taker fees as:

```text
fee = C × feeRate × p × (1 - p)
```

Where:

- `C`: number of shares traded.
- `p`: fill price/probability.
- `feeRate`: market/category fee rate.

The fee is rounded to 5 decimals in USDC-equivalent paper accounting.

## Category rates used by Aurum

| Category | Taker fee rate | Maker fee rate | Maker rebate note |
|---|---:|---:|---|
| Crypto | 0.07 | 0 | maker rebate not credited in current paper engine |
| Sports | 0.03 | 0 | not used in first BTC contest |
| Finance / Politics / Mentions / Tech | 0.04 | 0 | not used in first BTC contest |
| Economics / Culture / Weather / Other | 0.05 | 0 | fallback for uncategorized markets |
| Geopolitics / world events | 0 | 0 | fee-free per docs |

For the first 7-day contest, Aurum is Bitcoin-only, so BTC/crypto markets use the Crypto taker fee rate.

## How the ledger applies fees

For a buy order with requested notional `N`:

1. Get observed outcome price from the shared snapshot.
2. Apply conservative paper slippage: `fill_price = observed_price + SLIPPAGE_BPS / 10000`, capped below `0.99`.
3. Reject if `fill_price > limit_price`.
4. Compute shares: `shares = N / fill_price`.
5. Compute fee: `fee = round(shares × feeRate × fill_price × (1 - fill_price), 5)`.
6. Compute cash impact: `gross_cost = N + fee`.
7. Apply reserve, total risk, and per-market risk caps against `gross_cost`, not just `N`.
8. For applied paper fills, reduce cash by `gross_cost` and add `gross_cost` to cost basis.

This means ROI now includes Polymarket-like taker fees instead of assuming fee-free execution.

## Dashboard visibility

The public dashboard shows:

- Polymarket Rules section in the left rail.
- Fill role: taker.
- Fee formula.
- BTC/Crypto taker fee rate.
- Maker fee/rebate status.
- Fee precision and minimum paper order.
- Event log fill rows include fee and fee category when present.

## Known limitations

- The public Gamma/search records do not always expose exact CLOB `feesEnabled` / fee descriptor fields. Aurum uses explicit fields when present, otherwise maps visible market category/text to the official fee table.
- Market-specific min order size, tick size, book depth, partial fills, queue priority, and maker rebates require recorder/order-manager v2.
- Deposit/withdraw Polymarket fees are modeled as zero. Intermediary fees such as Coinbase/MoonPay are intentionally excluded because the contest is paper-only and has no deposit/withdraw path.
