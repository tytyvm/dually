# Validation — 2026-09-13

## Dashboard release v0.2

* 62 Python tests pass, covering the previous engine plus authentication, session
  expiry/logout, CSRF/origin restrictions, login throttling, oversized requests,
  collector controls, saved experiments, demo isolation, and settings validation.
* JavaScript syntax checks pass. DOM-level interaction checks verify initial
  rendering, demo/open positions, SVG chart generation, chain filtering, tabs,
  action error visibility, and disabled controls after a connection failure.
* An installable wheel builds with all six dashboard assets packaged.
* Visual desktop/mobile browser verification was not completed: the local browser
  could not start and its download timed out; the remote browser could not connect
  to the local app. DOM checks do not substitute for visual layout verification.
* Render hosting configuration is included, but no deployment in the user's
  account or provider-side billing/Blueprint validation has been performed.

## Initial engine release v0.1

* 48 offline tests pass under Python 3.11+ compatible source (actual local runtime
  version recorded in `examples/environment.json`).
* CLI demo -> SQLite -> JSONL export -> CLI replay produces identical account
  balances on both chains. Each synthetic chain has one winner and one loser.
* Tests cover duplicate/reordered/stale events, restart persistence, database
  rollback, entry latency, adverse exit latency, loss limits, drawdown latch,
  zero-liquidity blocked exits, pool pinning, pending-order expiry, both strategy
  predicates, chain separation, post-exit reentry accounting, and HTML escaping.
* The learning test fits a model on synthetic fixtures solely to verify training
  code, outcome purging and file output. This is not a fitted real-world strategy.
* A real public API pass returned HTTP 200 for new/trending pools on both chains
  and multi-pool retrieval on Robinhood. Four malformed/incomplete Solana records
  were safely rejected. 36 Solana snapshots and one selected Robinhood snapshot
  were persisted; one sample per chain is in `examples/public-sample.jsonl`.
* An initial urllib request failed with HTTP 403; the later configured httpx client
  succeeded. Network availability and provider behavior are not guaranteed.

Not tested: prolonged collection, actual transaction execution, Docker build,
browser rendering across devices, streaming decoders, venue-specific quotes,
trading profitability, robustness across market regimes, or calibrated prediction.

`examples/demo-report.html` and `examples/demo-snapshots.jsonl` are SYNTHETIC.
Do not use their returns to estimate real performance.
