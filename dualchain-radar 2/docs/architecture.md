# Architecture and evaluation boundaries

## Data flow

GeckoTerminal (solana / robinhood) or normalized JSONL -> validated Snapshot ->
append-only observation table -> strategy + optional statistical filter -> risk
gate -> pending paper order -> later snapshot -> estimated fill -> position ledger.

One SQLite transaction commits each observation, decision and balance update.
Repeated event IDs cannot double-fill. Late events cannot rewind the portfolio.
Keys include chain and token; a Solana and EVM address never share a position.
Positions pin their original pool. New pools are selected one per token from the
observed discovery universe; the choice is not claimed to be the deepest pool.

## Learning target

The baseline learns a target-first outcome for eligible strategy setups, not
wallet behavior. Inputs use only information available at the signal time.
Entry uses a subsequent snapshot. A target requires a subsequent observation to
remain above the target after costs. Stop/time outcomes are negative; incomplete
future paths are censored. Pool disappearance without subsequent observations
cannot be cleanly labeled and is reported as a data gap.

The learner requires 100 complete labels and 50 training/20 holdout rows, with
both labels in training. These are software minimums, not claims of statistical
sufficiency. The first 75% of signal times determines the training boundary;
training examples whose outcomes cross it are purged. Scaling is fitted on
training only. Holdout does not choose model weights or threshold. Training
includes neither a network nor an LLM API, only logistic gradient descent with
L2 regularization. Training can run without paid services.

The target differs from the trading policy when trailing or liquidity exits
fire earlier: model classification metrics are not portfolio-return metrics.
Always replay and run forward paper experiments with the actual exit policy.
Repeated market regimes and correlated tokens reduce effective sample size.
No uncertainty intervals, probability calibration or chain-by-chain model
selection are implemented. This remains a research baseline.

## Evaluation rules

1. Keep every sampled loser; discovery has selection bias and limited coverage.
2. Never backfill historical pool state using today's liquidity or market cap.
3. Never assume the candle high was executable for the requested order size.
4. Evaluate unseen dates and launch cohorts; do not tune on the reported holdout.
5. Report closed PnL, open/stale exposure, costs, missed/blocked exits, drawdown and
   net PnL excluding the best trade. The HTML report and SQLite audit expose these.
6. Public snapshot replay is a screening stage. Validate against true venue state
   and tiny live fills before concluding that an apparent edge is tradable.

## Data schema

JSONL has one Snapshot object per line. `ts` is epoch seconds at which the data
became available, not the start of an interval whose future values are included.
`source_ts` is optional underlying observation time; null means unknown freshness.
`change_m5` is a fractional return (0.20 = +20%), not percentage points.
`buyers_m5` is unique buyers, never buy transaction count. `market_cap` is nullable
and separate from FDV. `created_at` is pool creation time, not proven token birth.
Identifiers for Robinhood are lowercased; Solana identifiers remain case-sensitive.
Zero price/liquidity is permitted so liquidity-loss events reach exit logic.

