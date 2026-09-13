# Path from this MVP to real execution

This release deliberately has no private-key field and no route capable of
placing a real order. `radar.execution.sign_and_send` explicitly refuses calls.

## Solana integration

1. Choose the exact venues: pre-migration launch curves and post-migration AMMs
   need different decoders and swap construction. Do not assume Jupiter covers
   every new launch immediately.
2. Subscribe to venue transactions/account updates through a supported streaming
   service. Persist slot, signature, instruction index and arrival timestamp.
3. Verify mint/freeze authority, relevant Token-2022 extensions, sell behavior,
   liquidity and concentrated creator holdings. No check guarantees safety.
4. Quote actual route output for the proposed size; apply minimum output, priority
   fee, transaction-age and price-impact bounds before signing.
5. Use a bounded-risk signer, submit, confirm, reconcile actual balance changes,
   and ensure a timeout never causes a second economic order.

## Robinhood integration

1. Verify RPC chain identity (the reviewed upstream uses chain ID 4663) and the
   selected deployed DEX/router contracts from official deployment records.
2. Index block/log events with a persistent cursor and reorg recovery. Wallet
   receipts must positively establish intent: relayers and pushed tokens make
   token receipt alone insufficient proof of purchase.
3. Implement the selected router's quote/simulation, allowance and gas semantics.
   Restrict token/router/chain combinations; use exact bounded allowances.
4. Persist nonces and signed transaction hashes before submission; reconcile
   confirmations, replacement transactions, approvals and actual token balances.

## Shared boundary

Both decoders can feed `dualradar ingest` today using Snapshot JSONL. That only
updates paper positions. Faster streams alone do not improve signal validity.
The snapshot engine is a starting point; event-time ordering, chain finality and
wallet-risk features must be implemented at the transaction layer.

Required choices before live work: venues on each chain, streaming/RPC provider
credentials, execution wallet design, maximum total capital/exposure, measured
fee settings and acceptable slippage. Public APIs and demo data cannot substitute
for these. No live readiness or profitability is claimed by this build.

