# Dualchain Radar

**v0.2 adds a password-protected browser dashboard.** Start with
[START-HERE.md](START-HERE.md) for browser-only hosting on Render. No local terminal
or administrator access is required to operate the hosted app. Hosting is paid;
this archive has not been deployed to your account.

An executable **research and paper-trading MVP** for Solana and Robinhood Chain.
It collects public pool snapshots, evaluates microcap momentum or pullbacks,
simulates delayed entries/exits, persists its state, and exports research data.

**This version cannot place real trades. It has no signer or private-key input.**
It has no trained production model, no demonstrated trading edge, and no claim
of consistent 2–5× returns. Default thresholds are test hypotheses.

## Start in five commands

Python 3.11+; macOS, Linux, or Windows (use `.venv\Scripts\activate` on Windows).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
dualradar --db data/demo.db demo --out demo-report.html
python -m pytest -q
```

Open `demo-report.html` in a browser. The demo deliberately contains synthetic
winners AND losers on both chains. Its returns are a software check, not evidence
that a strategy works. Use a fresh database filename to rerun it.

## Collect both chains and paper trade

```bash
dualradar --db data/paper.db run --once
dualradar --db data/paper.db run
```

The public GeckoTerminal adapter needs no API key. It fetches new/trending pools
and follows a limited candidate list once a minute. Open-position pools remain
pinned even when they disappear from discovery or exceed $500k. The two chain
accounts have separate simulated cash and risk limits; they do not bridge funds.

An unsuccessful network request is a collection error, never synthetic market
data. A live smoke check on 2026-09-13 returned HTTP 200 on both network discovery
routes and the Robinhood multi-pool route. The engine recorded 36 Solana snapshots
and one selected Robinhood pool snapshot. This was one collection pass, not a
sustained feed test or execution test. Verify coverage locally before relying on it.

REST freshness/coverage are limited. This is **not** a fast launch sniper and is
not full-chain surveillance. New pools are not necessarily new tokens. It sees
indexed pools, not every pre-migration launch or bonding curve. Missing market
caps and unique-buyer counts are rejected by default. You may explicitly enable
`allow_fdv_proxy` to research FDV-filtered entries, but that is not verified MC.

## Controls and output

```bash
dualradar --db data/paper.db status
dualradar --db data/paper.db pause
dualradar --db data/paper.db resume
dualradar --db data/paper.db report --out paper-report.html
dualradar --db data/paper.db export observations.jsonl
```

`pause` stops entries, cancels pending buys, and requests exits on later valid
snapshots. It cannot sell without data. Drawdown halts latch; `resume` does not
clear them. Stale positions remain recorded, are valued at zero for risk checks,
and block new entries on their chain. Daily loss limits use UTC equity baselines.
Configuration and optional model identity are tied to the database; use a new
database when changing an experiment. Back up with SQLite's backup API, not by
copying a live WAL database's main file alone.

## Two independent baseline experiments

`strategy = "momentum"`: sufficient liquidity/volume/unique buyers, positive
five-minute momentum, observed price continuation and increasing buyer count.

`strategy = "pullback"`: an observed prior run, a pullback from the observed
peak, and a recovery from the previous observation. The hard liquidity, age and
participation filters still apply. This implementation does not claim to detect
independent buyers or insider wallets; the public feed cannot establish those.

Copy `config.toml` to `pullback.toml`, change strategy, and use a separate database.
Test 2×, 3× and 5× full-exit targets in separate configurations. No partial exits
in this baseline, which keeps target multiples and accounting unambiguous.

## Replay and learning

```bash
dualradar --db data/replay.db replay observations.jsonl --out replay-report.html
dualradar train observations.jsonl --out model.json
dualradar --db data/model-paper.db --model model.json run
```

Replay preserves event availability order and never fills at a signal's earlier
price. It does not invent exits at the dataset boundary. Sparse snapshots can
miss intrainterval stops/targets; replay is not tick-accurate.

`train` fits a small regularized logistic baseline after enough labeled setups
exist. It purges labels overlapping the chronological holdout boundary and
reports Brier score against a constant baseline, selection precision and coverage.
Insufficient history or demo input is rejected. Missing forward observations are
censored, not invented wins/losses; that can bias the measured population and the
censored count must be considered. There is no automatic promotion. Output is an
**uncalibrated research score**, not a validated probability. It must be tested
on more future periods and separately by chain, with clustered-token uncertainty,
before any capital decision. A model cannot score timestamps preceding its
training cutoff. Details: `docs/architecture.md`.

## Fills and costs

All fills use a constant-product pool approximation with half TVL as quote reserve,
configurable swap fees, extra adverse slippage and fixed USD transaction costs.
It is **not an executable quote**, and is unsuitable as an exact simulation of
concentrated liquidity, bonding curves, MEV, taxes, routing or failed transactions.
Default costs are estimates, not measured chain fees. Entry and exit both incur
costs and execute on later observations. Ordinary stops may overshoot. A missing
or zero-liquidity pool produces a blocked exit, never a fabricated sale.

## What is not built yet

* Real transaction signing, submission, confirmation and reconciliation.
* Solana streaming venue decoders and Robinhood receipt/intent verification.
* Actual executable quotes and venue-specific state replay.
* Creator/holder funding clusters and contract security checks.
* Wallet-copy prediction, trained profitable models, or reliable 2–5× returns.
* Telegram delivery. Browser dashboard authentication is included in v0.2.

The normalized JSONL `ingest` interface is ready for faster upstream decoders.
See `docs/live-integration.md` for exact remaining integration boundaries.

## Sources

* [GeckoTerminal public API](https://apiguide.geckoterminal.com/)
* [Public endpoint reference](https://www.geckoterminal.com/dex-api)
* [Helius streaming](https://www.helius.dev/)
* [Jupiter swap APIs](https://dev.jup.ag/)
* [Jito transaction delivery](https://docs.jito.wtf/)

Original implementation; no source files copied from fomo-robinhood-radar.
