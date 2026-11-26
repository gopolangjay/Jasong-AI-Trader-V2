# OpenAI DEMO analyst

The server reuses its existing `OPENAI_API_KEY`. No credential is copied to the
repository, API responses, model evidence, mobile client or logs. The model reads
only a bounded snapshot of closed price candles and trusted market identifiers.
It has no broker tools and cannot change account configuration or execution limits.

## Policy

The AI portfolio covers 28 liquid FX pairs, Gold and Bitcoin. Each batch returns
zero to three BUY/SELL proposals; all other markets WAIT. A proposal supplies its
symbol, direction, stop, target, 15–240 minute horizon and concise rationale. It
must echo the exact evidence context ID and cannot include size, EPIC, account,
live-money, override or arbitrary execution parameters. Duplicate/unknown symbols,
wrong-side stops/targets, incomplete outputs and refusals reject the entire batch.

EMA crosses, ADX regimes, RSI divergence, ML probability/FAST floors, geographical
session hours, fixed minimum reward/risk and historical PRIME status do not decide
AI entry eligibility. The model chooses a defensible setup from observed prices;
WAIT remains a valid outcome. No win-probability claim is derived from its output.

Execution retains strict UTC M15 alignment, valid OHLCV, closed bars, fresh evidence,
fresh exact-instrument quotes, IG tradeability and existing category spread caps.
Analysis, risk sizing and final broker preflight share the same parser for flat
quotes, v4 price ladders and a successfully fetched v3 fallback pair. Missing,
non-finite, inverted or excessively wide fresh quotes still prevent submission.
Ranked eligibility is false whenever an operational rejection reason remains.
Freshness validation reads the clock after analysis and broker I/O completes,
so network latency cannot make a newly received quote appear future-dated.
An explicitly configured required news guard still applies. Data older than 20
minutes, decisions older than 5 minutes and quotes older than 3 minutes cannot
authorize an entry. Cached decisions retain their original creation timestamp.

The protected category executor continues account-risk sizing (maximum 1%), daily
entry caps, duplicate setup/symbol/EPIC protection and portfolio/theme limits. Model
levels transfer proportionally across analysis/broker price units. Spread buffers
reduce available reward/risk; no artificial target is invented to force admission.
Broker minimum size and dealing-distance failures reject the order. A stop and
target are submitted initially; absent protection or excessive filled cash risk
requires an immediate close. Stops, targets and holding-time exits remain managed.

Bitcoin uses the previously verified IG DEMO EPIC and a fresh Bitcoin instrument
name/quote check. It has no weekend-only veto in this mode. The independent legacy
weekend entry worker and allocation-sized Compound lane cannot originate competing
AI entries. Existing holdings and prior-generation daily entry counts remain visible.

## Configuration and usage

| Setting | Default | Purpose |
| --- | --- | --- |
| `JASONG_ANALYSIS_MODE` | `OPENAI` | Use `REGIME` to restore the previous rule-based entry path |
| `OPENAI_API_KEY` | Existing Render secret | OpenAI authentication; absence produces WAIT |
| `JASONG_OPENAI_ANALYST_MODEL` | `JASONG_OPENAI_MODEL`, then `gpt-5.4-mini` | Explicit model override, preserving an existing model selection |
| `OPENAI_ANALYST_MAX_REQUESTS_PER_DAY` | `96` | Durable UTC-day request reservation; maximum allowed configuration 288 |
| `OPENAI_ANALYST_TIMEOUT_SECONDS` | `45` | Per-request transport timeout, bounded to 5–90 seconds |
| `OPENAI_ANALYST_BUDGET_PATH` | Alongside strategy state | Durable request/token accounting on the existing persistent disk |

At most one paid batch is attempted per M15 period, shared by all scanners. Its
input contains at most 300,000 characters and its output is bounded to 4,000
tokens. No automatic provider retries, hidden alternative-model calls or legacy
entry fallback occur. Reservations are persisted before a paid call. Request count,
reported token usage, model, last success/error and current proposals are visible
without revealing the key. API consumption is separate from ChatGPT chat usage.

The existing single-process, single-instance Render service is the supported
runtime for this budget/scanner design. Scaling it requires a shared distributed
budget and execution lock first.

`GET /strategy/openai-analysis` is a cached, side-effect-free diagnostic endpoint.
`/health` and `/market-categories/status` also expose analyst state. API refusals,
timeouts, quota/auth/model errors and unavailable data remain observable; the
system does not substitute a forced trade. Authenticated Actions retain API-key,
write-enable and explicit-confirmation checks; their only order-open route still
accepts a symbol and rechecks the server's current AI eligibility and risk controls.

## Validation

The regression suite mocks both OpenAI transport and IG. It checks structured
response validation, WAIT, context binding, unknown/duplicate proposals, protected
level geometry, refusal/incomplete/error handling, durable budgets, per-bar caching,
weekday Bitcoin admission, stale/invalid-data rejection, account and duplicate
limits, initial protection and unsafe-fill closes. The original strategy tests
remain as rollback regression coverage. Offline startup smoke must pass in both
`OPENAI` and `REGIME` modes before release. Actual key/model availability and
post-deploy decisions must additionally be verified on Render.
