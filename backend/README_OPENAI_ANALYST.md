# OpenAI DEMO analyst and executor

The server reuses its existing `OPENAI_API_KEY`. Credentials never enter model
context, source control, public responses or logs. The hard-coded broker remains
IG DEMO, and live-money execution is unavailable.

## Guarded DEMO policy

`JASONG_ANALYSIS_MODE=OPENAI` and `JASONG_DEMO_EXECUTION_POLICY=GUARDED` are
the defaults. The existing `DISCRETIONARY` setting selects this same policy.
The model analyses completed M15/H1/H4 candles for 28 liquid FX pairs, Gold and
Bitcoin. Orders require an explicit `ENTER_NOW`, a completed M15 confirmation
and a fresh IG quote within the trigger, spread, stop and target limits.
The account book allows one open position per market and one per direction of
yen exposure. Open positions from before the switch consume this capacity.

A positive model `size` is in IG contract units; null uses the existing configured
IG default. IG enforces minimum-size, increment, market, margin and order rules.
The executor requires a broker confirmation to record a fill.

Guarded entries use market orders. The model's stop and target validate entry
geometry and do not attach an order. Models explicitly choose `HOLD` or `CLOSE`
for owned positions using their reconciled broker quotes, available P&L and
original rationale. The server R/profit watchdog and native amendments are
disabled in this mode. Excursion reporting remains observational.

Each accepted decision is bound to its exact input context and expires after five
minutes. Unknown symbols/position aliases, malformed output, invalid closed data
and live environments cannot submit orders. Position aliases exclude account and
broker deal identifiers from model input. Only owned JSCAT positions can be closed.
The app reserves each submission durably before the external request. The same
model decision cannot repeat after a tick, manual request or restart. A timeout is
reconciled by the reserved broker reference, without blindly opening a duplicate.
A pending close acknowledgement is reported as pending until verified/reconciled.

## Rollback

`JASONG_DEMO_EXECUTION_POLICY=PROTECTED` retains the prior 1% account-risk sizing,
capacity/exposure/daily limits, quote/spread/news checks, attached stop/target and
watchdog management. `JASONG_ANALYSIS_MODE=REGIME` restores the rule-based path.
Existing broker positions and historical trade evidence are retained.

## API configuration and operation

| Setting | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Existing Render secret | Provider authentication |
| `JASONG_OPENAI_ANALYST_MODEL` | `JASONG_OPENAI_MODEL`, then `gpt-5.4-mini` | Model override |
| `OPENAI_ANALYST_MAX_REQUESTS_PER_DAY` | 96 | Durable API-use budget, configurable up to 288 |
| `OPENAI_ANALYST_TIMEOUT_SECONDS` | 45 | Timeout bounded to 5–90 seconds |
| `OPENAI_ANALYST_BUDGET_PATH` | Alongside strategy state | Durable usage accounting |

At most one batch per M15 period is shared by scanners. Input remains bounded to
300,000 characters, output to 4,000 tokens, with `store=false` and no automatic
provider retries. API usage is charged separately from ChatGPT chat usage. These
operating-cost controls are not trading risk caps and remain unchanged.

`GET /strategy/openai-analysis` exposes cached model decisions, closes, usage and
provider errors. `/category-portfolio/status` reports active execution policy and
order/close errors; `/category-portfolio/positions` reports tracked confirmations.
`GET /strategy/entry-trial` reports new guarded fills and broker-verified cash P&L.
The prior comparison is retained under `shadow_comparison`.
`/mobile/sync` is a cached combined snapshot. GET status never starts an analysis
or order. Existing Actions authentication, write-enable and explicit confirmation
remain enforced; the chat is not given a plaintext API key or a new bypass route.

The existing single-process/single-instance Render deployment is required for the
in-process scanner/order locks. A scale-out deployment requires distributed locks.
Tests cover both guarded and protected execution, close ownership, actual
confirmation versus pending acknowledgement, restart/timeout recovery, provider
redaction and budgets, strict candle handling, and live-environment rejection.
Offline startup checks exercise guarded, protected and REGIME modes.
