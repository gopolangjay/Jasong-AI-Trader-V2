Jasong Broker-Settled Forward PRIME
This integration changes what is allowed to veto PRIME without deleting historical research.
Execution chain
`live specialist signal -> Quant >= 28 -> directional AI >= 40 -> live FAST >= 45 -> provenance/freshness -> IG tradeable/spread -> STRONG -> controlled IG DEMO Category learning -> broker-settled results -> forward PF/expectancy/WR/drawdown/bootstrap -> PRIME -> Compound rank #1/#2`
Capacity, duplicate, EPIC and theme/exposure limits remain enforced by the Category and Compound execution engines at broker-entry time.
Historical validation
Holdout WR, historical PF, sample count, variant stability and walk-forward metrics are retained under `historical_validation`, with:
```json
{
  "mode": "INFORMATIONAL_ONLY",
  "execution_veto": false
}
```
They can still help us understand a strategy but cannot prevent a live STRONG signal from entering the controlled IG DEMO forward-learning lane.
Forward PRIME defaults
minimum settled trades: 12
rolling window: 40 trades
profit factor: >= 1.20
expectancy: >= +0.05R
win rate: >= 45%
bootstrap probability of positive expectancy: >= 75%
max rolling drawdown: <= 6R
All values can be changed with `FORWARD_*` environment variables.
Provenance
The current V6.9.3 specialist frame loader uses Yahoo Finance (`yfinance`) for public analysis data; the execution/preflight quote remains IG DEMO. The new registry records them separately and exposes missing/stale broker quotes instead of silently treating `UNAVAILABLE` as usable.
Key fields are `analysis_price_source`, `analysis_price_timestamp`, `broker_quote_source`, `broker_quote_timestamp`, `news_sources`, `news_timestamp`, `fallback_source`, `signal_age_seconds`, and `quote_age_seconds`.
Learning
`strategy_learning.py` only diagnoses recurring broker-settled mistakes. It does not rewrite a strategy after a single loss. Findings require repeated occurrences and include weak-trend entries, overextended RSI entries, stale signals/quotes, spread/slippage problems, entry timing and profit giveback.
API diagnostics
`GET /forward-validation/status`
`GET /forward-validation/learning`
`GET /forward-validation/trades`
`GET /market-categories/compound-candidates`
The system remains IG DEMO only; no live-money endpoint is introduced by this patch.
