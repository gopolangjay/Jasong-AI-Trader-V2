Jasong AI Trader — V6.3 Clean Core
Purpose
This package removes `backend/main.py` and `backend/execution_reliability.py` from the Render startup chain.
The current runtime becomes:
`run_server.py`
→ `clean_core_runtime.py`
→ `IGDemoBroker`
→ `EliteCompoundEngine`
→ `install_specialist_market_system(...)`
The specialist integration already owns the current category intelligence, broker-settled forward PRIME authority, category execution, MFE/MAE tracking, mobile sync and ChatGPT diagnostics.
What is deliberately NOT imported by Clean Core
`main.py`
`execution_reliability.py`
`v64_learning_engine.py`
`auto_manager.py`
`trade_watcher.py`
`deep_validator.py`
`sequential_scanner.py`
`global_market_engine.py`
`ig_demo_bridge.py`
This means those modules can no longer start workers, submit learning trades, queue historical validation or monkey-patch the live runtime merely because Render booted.
Install
Copy these files into `backend/`:
`clean_core_runtime.py` — new
`clean_core_smoke.py` — new
`run_server.py` — replace existing file
`Dockerfile` — content remains intentionally simple; included for a clean package
Do not delete legacy files in the same commit that activates Clean Core.
Validation sequence
From `backend/`:
```bash
python -m py_compile clean_core_runtime.py run_server.py clean_core_smoke.py
python clean_core_smoke.py
```
Then deploy to Render and verify:
```text
GET /health
GET /market-categories/status
GET /market-categories/FOREX
GET /category-portfolio/status
GET /market-categories/compound-candidates
GET /mobile/sync
GET /trade-excursions/status
```
Expected `/health` markers:
`runtime = CLEAN_CORE`
`legacy_main_imported = false`
`legacy_execution_reliability_imported = false`
`live_money_execution = false`
broker environment = `DEMO`
Safe deletion policy
Phase A — activate first; do NOT delete yet
Keep the old files for one successful Render deploy and runtime verification. They are inert because `run_server.py` no longer imports them.
Phase B — high-confidence deletion after Clean Core passes
These are legacy startup/orchestration modules that the new bootstrap does not import:
`backend/main.py`
`backend/execution_reliability.py`
`backend/main_intergration_snippet.py`
Before deletion, use GitHub code search to confirm there are no remaining imports from tests/workflows.
Phase C — dependency-confirmed legacy deletion
Delete these only after repository-wide code search shows no remaining imports from active code:
`backend/v64_learning_engine.py`
`backend/auto_manager.py`
`backend/trade_watcher.py`
`backend/deep_validator.py`
`backend/sequential_scanner.py`
`backend/global_market_engine.py`
`backend/ig_demo_bridge.py`
old historical/backtest-only scanners and optimizer modules imported solely by legacy `main.py`
Do not delete merely because a module looks old. Current modules such as `specialist_market_integration.py`, `category_strategy_engine.py`, `category_execution_engine.py`, `prime_policy.py`, `forward_validation.py`, `forward_store.py`, `trade_excursions.py`, `mobile_sync.py`, `chatgpt_actions.py`, `chatgpt_mcp.py`, `specialist_market_data.py`, `market_data_router.py`, `engine.py`, `indicators.py`, `compound_engine.py`, and `ig_demo_broker.py` remain part of Clean Core.
Suggested commits
Commit 1:
```text
feat: introduce V6.3 Clean Core runtime
```
Commit 2 after smoke/deploy validation:
```text
refactor: make Clean Core the exclusive Render startup path
```
Commit 3 only after repository-wide import audit:
```text
chore: remove disconnected legacy runtime modules
```
Important safety boundary
This package keeps `IGDemoBroker`, whose base URL is hard-coded to the IG DEMO gateway in the current repository. It does not add a live-money broker path.
