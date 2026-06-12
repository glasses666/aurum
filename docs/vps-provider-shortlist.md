# Aurum VPS Provider Shortlist

Snapshot date: 2026-06-13

## Bottom line

Avoid mainland China cloud nodes for Aurum's Polymarket recorder. The existing Guangzhou Alibaba Cloud ECS failed direct Polymarket HTTPS/WebSocket preflight, and Alibaba-style DNS/routing controls add avoidable uncertainty.

For Phase 0/1, Aurum needs a small, boring overseas VPS:

- direct HTTPS/WebSocket reachability to Polymarket public endpoints;
- stable DNS;
- enough disk for orderbook/trade retention;
- no wallet, no private key, no live order placement;
- low rate, read-only public API usage.

## Platform-risk interpretation

Phase 0/1 is low-risk if it stays read-only:

- allowed behavior: public market metadata, public orderbook/trade data, local paper trading, daily reports;
- no login required;
- no wallet/private key/USDC;
- no live order endpoint;
- no geoblock bypass;
- no account automation;
- no aggressive scraping or captcha/security bypass.

The realistic risks are operational, not dramatic:

1. cloud provider DNS/routing blocks or unstable international egress;
2. Polymarket API/WebSocket reachability varies by region/provider;
3. future live-trading rules may depend on user jurisdiction, platform terms, and IP geography;
4. cheap VPS providers can be oversold or have poor abuse reputation;
5. too-small disks lose forward orderbook history.

Do not place future live orders from a jurisdiction/location that Polymarket restricts. Keep Phase 0/1 read-only.

## Shortlist

### 1. Hetzner CAX11 — cheapest strong default if Europe is acceptable

Evidence captured from Hetzner official cloud data JSON:

- plan: `CAX11`
- cores: `2`
- core type: `ampere` / ARM64
- locations observed: `HEL1`, `NBG1`
- price observed: `€4.49/month` or `$5.49/month`
- traffic pricing data indicated a 20TB tier in the official JSON snapshot

Why it fits:

- excellent price/performance;
- ARM64 is fine for Python recorder + SQLite/DuckDB + small services;
- boring Linux images available;
- good first 24/7 recorder candidate if preflight passes.

Caveats:

- no Asia region in this cheap ARM plan;
- account signup/KYC/payment review can be stricter than small VPS resellers;
- verify RAM/disk/current console details before purchase;
- do not use it for future live trading if geography/jurisdiction would be restricted.

Recommended use:

- first choice for cheapest serious recorder if Europe latency is acceptable.

### 2. Vultr regular cloud — best Asia-region low-friction candidate

Evidence captured from Vultr public plans API:

- `vc2-1c-1gb`: 1 vCPU, 1GB RAM, 25GB disk, `$5/month`, locations include `sea`, `lax`, `ams`, `lhr` and more in the API sample.
- `vc2-1c-2gb`: 1 vCPU, 2GB RAM, 55GB disk, `$10/month`.
- `vhf-1c-1gb`: 1 vCPU, 1GB RAM, 32GB disk, `$6/month`.

Why it fits:

- broad regions including Asia/US/EU;
- simple signup and hourly billing;
- good for quick preflight across Tokyo/Singapore/Los Angeles/Seattle-style regions.

Caveats:

- 1GB RAM / 25GB disk is tight for long retention;
- `$10/month` 2GB/55GB is safer for sustained recorder + reports;
- test the selected region before installing Aurum.

Recommended use:

- first choice if Queen Glasser wants Asia proximity and low setup friction.

### 3. OVHcloud VPS-1 — cheap Europe/Canada-style alternative

Evidence captured from OVH official VPS page:

- `VPS-1` was shown as “From `$4.54/month`” in the page snapshot.

Why it fits:

- cheap;
- major provider;
- likely less China-cloud egress uncertainty than Alibaba mainland.

Caveats:

- exact RAM/disk/region should be confirmed at checkout;
- some very cheap VPS tiers have smaller disks;
- must pass Aurum preflight before deployment.

Recommended use:

- good fallback if Hetzner signup is annoying or if an OVH region tests better.

### 4. Linode / Akamai Nanode — stable but less cost-effective above 1GB

Evidence captured from Linode official API:

- `g6-nanode-1`: 1GB RAM, 25GB disk, 1 vCPU, `$5/month`.
- `g6-standard-1`: 2GB RAM, 50GB disk, 1 vCPU, `$12/month`.

Why it fits:

- mature cloud;
- good reliability;
- clean Linux images.

Caveats:

- `$5` tier is small;
- `$12` 2GB tier is less attractive than Vultr/Hetzner for this use.

Recommended use:

- choose only if Linode has a region/payment/account advantage.

### 5. Oracle Always Free Ampere — free if already available, not a fast procurement path

Why it fits:

- potentially very good free ARM resources;
- enough RAM/CPU for recorder + analysis if quota is available.

Caveats:

- signup can fail or be slow;
- capacity availability is uneven;
- free resources can have opaque limits;
- not ideal if we need a reliable node tonight.

Recommended use:

- use if Queen Glasser already has a working Oracle Cloud account; do not block Aurum on it.

## Providers to avoid for the first recorder

Avoid using these as the first primary Aurum recorder unless there is a strong reason and preflight passes:

- mainland Alibaba/Tencent/Huawei nodes for Polymarket data;
- random ultra-cheap yearly VPS from unknown resellers;
- app-image servers with preinstalled panels/bots;
- providers with unclear acceptable-use policies around financial/trading automation;
- any node that requires proxying around DNS/geoblocks just to reach public endpoints.

## Recommended purchase order

1. If a Hetzner account is easy: try `CAX11` in `NBG1` or `HEL1`, then run Aurum preflight.
2. If Asia region matters more: try Vultr Tokyo/Singapore-style region, preferably the `$10/month` 2GB/55GB plan if budget allows, otherwise `$5/month` as a short-retention prototype.
3. If Europe/Canada cheap fallback is fine: try OVH `VPS-1` and preflight.
4. Use Linode only if payment/account/region convenience beats price.
5. Keep Alibaba Hong Kong as a backup, not the default, because Alibaba-family networking already showed unpleasant behavior on the Guangzhou node.

## Post-purchase acceptance gate

Immediately after creating any candidate VPS:

```bash
scp scripts/vps_network_preflight.py USER@HOST:/tmp/aurum-vps-preflight.py
ssh USER@HOST 'python3 /tmp/aurum-vps-preflight.py --json'
```

Accept only if:

- HTTPS endpoints pass for 3 consecutive runs;
- WebSocket passes, or we explicitly accept a polling-only first phase;
- worst successful HTTPS latency is preferably under 3s, tolerable under 8s for Phase 0;
- disk has enough free space for retention;
- NTP/time sync is correct;
- no special proxy is needed for normal operation.

If a node fails preflight, destroy it before the billing window grows teeth. Dragons dislike paying rent to broken tunnels.
