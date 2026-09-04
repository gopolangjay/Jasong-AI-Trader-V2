from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from weekend_market_policy import STRATEGY_ID, assess_market, execution_guard

VERSION = "6.13-weekend-structure-execution-v3"

CRYPTO_SEEDS = (
    {"symbol": "BITCOIN", "terms": ["Bitcoin"], "tokens": ["BITCOIN"]},
    {"symbol": "ETHER", "terms": ["Ether", "Ethereum"], "tokens": ["ETHER"]},
    {"symbol": "SOLANA", "terms": ["Solana"], "tokens": ["SOLANA"]},
    {"symbol": "XRP", "terms": ["XRP", "Ripple"], "tokens": ["XRP"]},
    {"symbol": "LITECOIN", "terms": ["Litecoin"], "tokens": ["LITECOIN"]},
)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, dict): value = value.get("value")
    try:
        out = float(value); return out if out == out else None
    except Exception: return None


def _price(row: Dict[str, Any], field: str) -> Optional[float]:
    block = row.get(field) or {}
    if isinstance(block, dict):
        vals = [_num(block.get(k)) for k in ("bid", "ask", "offer", "lastTraded")]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None
    return _num(block)


def _candles(payload: Dict[str, Any]) -> List[Dict[str, float]]:
    rows = []
    for raw in payload.get("prices", []) or []:
        if not isinstance(raw, dict): continue
        o, h, l, c = (_price(raw, x) for x in ("openPrice", "highPrice", "lowPrice", "closePrice"))
        if None in (o, h, l, c): continue
        rows.append({"open": float(o), "high": float(h), "low": float(l), "close": float(c)})
    return rows


def structure_signal(m5: List[Dict[str, float]], m1: List[Dict[str, float]], spread: float) -> Dict[str, Any]:
    if len(m5) < 24 or len(m1) < 8 or spread <= 0: return {"eligible": False, "reason": "INSUFFICIENT_CANDLES_OR_SPREAD"}
    breakout, prior = m5[-2], m5[-22:-2]
    swing_high, swing_low = max(x["high"] for x in prior), min(x["low"] for x in prior)
    direction = level = None
    if breakout["close"] > swing_high: direction, level = "BUY", swing_high
    elif breakout["close"] < swing_low: direction, level = "SELL", swing_low
    if not direction: return {"eligible": False, "reason": "NO_M5_STRUCTURE_CLOSE"}
    recent = m1[-7:-1]; tolerance = max(spread * 1.5, abs(level) * 0.00015)
    if direction == "BUY": retest = any(x["low"] <= level + tolerance and x["close"] >= level - tolerance for x in recent)
    else: retest = any(x["high"] >= level - tolerance and x["close"] <= level + tolerance for x in recent)
    if not retest: return {"eligible": False, "reason": "NO_RETEST"}
    trigger, prev = m1[-2], m1[-3]
    if direction == "BUY":
        triggered = trigger["close"] > prev["high"] and trigger["close"] > trigger["open"]
        stop = min(x["low"] for x in recent) - spread * 1.25; entry = trigger["close"]; risk = entry - stop; target = entry + risk * 1.5
    else:
        triggered = trigger["close"] < prev["low"] and trigger["close"] < trigger["open"]
        stop = max(x["high"] for x in recent) + spread * 1.25; entry = trigger["close"]; risk = stop - entry; target = entry - risk * 1.5
    if not triggered or risk <= spread * 1.25: return {"eligible": False, "reason": "NO_M1_TRIGGER_OR_INVALID_RISK"}
    return {"eligible": True, "reason": "QUALIFIED", "direction": direction, "entry_reference": entry, "structure_level": level, "stop": stop, "target": target, "risk_distance": risk, "target_r": 1.5}


class WeekendMarketEngine:
    def __init__(self, broker: Any) -> None:
        self.broker = broker
        self.enabled = str(os.getenv("WEEKEND_AUTOTRADE", "true")).lower() in {"1","true","yes","on"}
        self.poll_seconds = max(60, int(os.getenv("WEEKEND_SCAN_SECONDS", "180")))
        self.max_open = max(1, min(2, int(os.getenv("WEEKEND_MAX_OPEN_POSITIONS", "2"))))
        self.max_spread_bps = max(5.0, float(os.getenv("WEEKEND_MAX_SPREAD_BPS", "100")))
        self.default_size = max(0.0001, float(os.getenv("WEEKEND_DEFAULT_SIZE", "0.5")))
        self.friday_start_utc = max(0, min(23, int(os.getenv("WEEKEND_FRIDAY_START_UTC", "18"))))
        self._stop = threading.Event(); self._thread: Optional[threading.Thread] = None
        self._state = {"version": VERSION, "strategy_id": STRATEGY_ID, "enabled": self.enabled, "last_scan_at": None, "last_error": None, "markets": [], "signals": [], "market_discovery": [], "opens": 0}

    def _scan_window(self) -> bool:
        if str(os.getenv("WEEKEND_FORCE_SCAN", "false")).lower() in {"1","true","yes","on"}: return True
        now = datetime.now(timezone.utc); wd = now.weekday()
        return wd >= 5 or (wd == 4 and now.hour >= self.friday_start_utc)

    def _owned_open(self) -> List[Dict[str, Any]]:
        return [item for item in (self.broker.positions() or {}).get("positions", []) or [] if str((item.get("position") or {}).get("dealReference") or "").upper().startswith("JSWKND_")]

    def _resolve(self, seed: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        diag: Dict[str, Any] = {"symbol": seed["symbol"], "terms": seed["terms"], "eligible": False, "stage": "SEARCH"}
        try:
            market = self.broker.resolve_global_market(search_terms=list(seed["terms"]), name_tokens=list(seed["tokens"]), require_tradeable=True, cache_key="WKND_" + seed["symbol"])
            diag.update({"stage": "DETAILS", "epic": market.get("epic"), "resolved_name": market.get("name"), "search_market_status": market.get("market_status"), "search_instrument_type": market.get("instrument_type")})
            details = self.broker.market_details(str(market["epic"]), require_quote=True)
            instrument, snap = details.get("instrument") or {}, details.get("snapshot") or {}
            quote = self.broker.extract_snapshot_quote(details)
            snapshot = {"epic": market["epic"], "instrumentName": instrument.get("name") or market.get("name"), "instrumentType": instrument.get("type") or market.get("instrument_type"), "category": "CRYPTO", "marketStatus": snap.get("marketStatus") or market.get("market_status"), "bid": quote.get("bid"), "offer": quote.get("offer"), "symbol": seed["symbol"], "expiry": instrument.get("expiry") or market.get("expiry") or "-"}
            verdict = assess_market(snapshot)
            diag.update({"stage": "POLICY", "market_status": snapshot.get("marketStatus"), "instrument_type": snapshot.get("instrumentType"), "bid": snapshot.get("bid"), "offer": snapshot.get("offer"), "eligible": bool(verdict.get("eligible")), "reason": verdict.get("reason")})
            return (snapshot if verdict.get("eligible") else None), diag
        except Exception as exc:
            diag.update({"eligible": False, "reason": "DISCOVERY_EXCEPTION", "error_type": type(exc).__name__, "error": str(exc)[:500]})
            return None, diag

    def _signal(self, market: Dict[str, Any]) -> Dict[str, Any]:
        bid, offer = float(market["bid"]), float(market["offer"]); spread = offer - bid; mid = (offer + bid) / 2.0
        spread_bps = spread / mid * 10000.0 if mid > 0 else 999999.0
        guard = execution_guard(market, max_spread=mid * self.max_spread_bps / 10000.0)
        if not guard.get("eligible"): return {"symbol": market["symbol"], **guard}
        m5 = _candles(self.broker.historical_prices_epic(market["epic"], resolution="MINUTE_5", num_points=40)); m1 = _candles(self.broker.historical_prices_epic(market["epic"], resolution="MINUTE", num_points=20))
        return {"symbol": market["symbol"], "epic": market["epic"], "spread_bps": spread_bps, **structure_signal(m5, m1, spread)}

    def _execute(self, sig: Dict[str, Any]) -> Dict[str, Any]:
        details = self.broker.market_details(sig["epic"], require_quote=True); snap = details.get("snapshot") or {}; quote = self.broker.extract_snapshot_quote(details)
        live = {"epic": sig["epic"], "category": "CRYPTO", "instrumentType": (details.get("instrument") or {}).get("type"), "marketStatus": snap.get("marketStatus"), "bid": quote.get("bid"), "offer": quote.get("offer")}
        if not execution_guard(live).get("eligible"): raise RuntimeError("PRE_ORDER_MARKET_GUARD_FAILED")
        instrument = details.get("instrument") or {}; size = self.broker._normalise_deal_size(self.default_size, minimum_size=self.broker._min_deal_size(details), increment=self.broker._deal_size_increment(details))
        payload = {"currencyCode": self.broker._default_currency(instrument), "dealReference": ("JSWKND_" + uuid.uuid4().hex[:20])[:30], "direction": sig["direction"], "epic": sig["epic"], "expiry": str(instrument.get("expiry") or "-"), "forceOpen": True, "guaranteedStop": False, "orderType": "MARKET", "size": round(size,12), "stopLevel": round(float(sig["stop"]),10), "limitLevel": round(float(sig["target"]),10)}
        ack = self.broker._request("POST", "/positions/otc", version=2, payload=payload); ref = str(ack.get("dealReference") or payload["dealReference"]); confirmation = self.broker.confirm(ref)
        if str(confirmation.get("dealStatus") or "").upper() == "REJECTED": raise RuntimeError("IG_REJECTED: " + str(confirmation.get("reason") or confirmation))
        self._state["opens"] = int(self._state.get("opens") or 0) + 1
        return {"dealReference": ref, "dealId": confirmation.get("dealId"), "dealStatus": confirmation.get("dealStatus"), "direction": sig["direction"], "stop": sig["stop"], "target": sig["target"], "target_r": 1.5}

    def tick(self) -> Dict[str, Any]:
        self._state["last_scan_at"] = time.time()
        if not self.enabled or not self._scan_window():
            self._state.update({"markets": [], "signals": [], "market_discovery": []}); return self.status()
        try:
            open_rows = self._owned_open()
            if len(open_rows) >= self.max_open: return self.status()
            resolved = [self._resolve(seed) for seed in CRYPTO_SEEDS]
            markets = [m for m, _ in resolved if m]; discovery = [d for _, d in resolved]
            signals = [self._signal(m) for m in markets]; qualified = sorted([s for s in signals if s.get("eligible")], key=lambda x: (float(x.get("spread_bps") or 999999.0), str(x.get("symbol") or "")))
            executions = [{"symbol": sig["symbol"], **self._execute(sig)} for sig in qualified[:self.max_open-len(open_rows)]]
            self._state.update({"markets": markets, "market_discovery": discovery, "signals": signals, "last_executions": executions, "last_error": None})
        except Exception as exc: self._state["last_error"] = f"{type(exc).__name__}: {exc}"
        return self.status()

    def status(self) -> Dict[str, Any]:
        active = self._scan_window()
        return {**self._state, "weekend_window": active, "scan_window": active, "friday_start_utc": self.friday_start_utc, "broker_status_is_final_authority": True, "demo_only": True, "live_money_execution": False, "max_open_positions": self.max_open, "target_r": 1.5}

    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive(): return
        def loop() -> None:
            while not self._stop.is_set(): self.tick(); self._stop.wait(self.poll_seconds)
        self._thread = threading.Thread(target=loop, name="weekend-market-v613", daemon=True); self._thread.start()
