from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional


# V6.7.3 curated liquid multi-asset starter universe.
# Analysis symbols are public-data symbols; IG execution is resolved independently
# through IG's own market search and exact instrument metadata.
GLOBAL_MARKET_SEEDS: List[Dict[str, Any]] = [
    # ------------------------- INDICES -------------------------
    {"key":"US500","name":"US 500","asset_class":"INDEX","analysis_symbol":"^GSPC","ig_search_terms":["US 500"],"expected_types":["INDICES"],"name_tokens":["US","500"],"exposure_tags":["US_EQUITY","GLOBAL_EQUITY"]},
    {"key":"USTECH100","name":"US Tech 100","asset_class":"INDEX","analysis_symbol":"^NDX","ig_search_terms":["US Tech 100","Nasdaq 100"],"expected_types":["INDICES"],"name_tokens":["100"],"exposure_tags":["US_EQUITY","US_TECH","GLOBAL_EQUITY"]},
    {"key":"WALLSTREET","name":"Wall Street","asset_class":"INDEX","analysis_symbol":"^DJI","ig_search_terms":["Wall Street"],"expected_types":["INDICES"],"name_tokens":["WALL","STREET"],"exposure_tags":["US_EQUITY","GLOBAL_EQUITY"]},
    {"key":"GERMANY40","name":"Germany 40","asset_class":"INDEX","analysis_symbol":"^GDAXI","ig_search_terms":["Germany 40"],"expected_types":["INDICES"],"name_tokens":["GERMANY","40"],"exposure_tags":["EU_EQUITY","GLOBAL_EQUITY"]},
    {"key":"FTSE100","name":"FTSE 100","asset_class":"INDEX","analysis_symbol":"^FTSE","ig_search_terms":["FTSE 100"],"expected_types":["INDICES"],"name_tokens":["FTSE","100"],"exposure_tags":["UK_EQUITY","GLOBAL_EQUITY"]},
    {"key":"FRANCE40","name":"France 40","asset_class":"INDEX","analysis_symbol":"^FCHI","ig_search_terms":["France 40"],"expected_types":["INDICES"],"name_tokens":["FRANCE","40"],"exposure_tags":["EU_EQUITY","GLOBAL_EQUITY"]},
    {"key":"EURO50","name":"EU Stocks 50","asset_class":"INDEX","analysis_symbol":"^STOXX50E","ig_search_terms":["EU Stocks 50","Euro Stoxx 50"],"expected_types":["INDICES"],"name_tokens":["50"],"exposure_tags":["EU_EQUITY","GLOBAL_EQUITY"]},
    {"key":"JAPAN225","name":"Japan 225","asset_class":"INDEX","analysis_symbol":"^N225","ig_search_terms":["Japan 225"],"expected_types":["INDICES"],"name_tokens":["JAPAN","225"],"exposure_tags":["JP_EQUITY","GLOBAL_EQUITY"]},
    {"key":"AUSTRALIA200","name":"Australia 200","asset_class":"INDEX","analysis_symbol":"^AXJO","ig_search_terms":["Australia 200"],"expected_types":["INDICES"],"name_tokens":["AUSTRALIA","200"],"exposure_tags":["AU_EQUITY","GLOBAL_EQUITY"]},
    {"key":"SA40","name":"South Africa 40","asset_class":"INDEX","analysis_symbol":"^J200.JO","ig_search_terms":["South Africa 40","SA 40"],"expected_types":["INDICES"],"name_tokens":["40"],"exposure_tags":["ZA_EQUITY","GLOBAL_EQUITY"]},

    # ----------------------- COMMODITIES -----------------------
    {"key":"GOLD","name":"Gold","asset_class":"COMMODITY","analysis_symbol":"GC=F","ig_search_terms":["Spot Gold","Gold"],"expected_types":["COMMODITIES"],"name_tokens":["GOLD"],"exposure_tags":["PRECIOUS_METALS","COMMODITIES"]},
    {"key":"SILVER","name":"Silver","asset_class":"COMMODITY","analysis_symbol":"SI=F","ig_search_terms":["Spot Silver","Silver"],"expected_types":["COMMODITIES"],"name_tokens":["SILVER"],"exposure_tags":["PRECIOUS_METALS","COMMODITIES"]},
    {"key":"USCRUDE","name":"US Crude","asset_class":"COMMODITY","analysis_symbol":"CL=F","ig_search_terms":["US Crude","Oil - US Crude"],"expected_types":["COMMODITIES"],"name_tokens":["CRUDE"],"exposure_tags":["ENERGY","COMMODITIES"]},
    {"key":"BRENT","name":"Brent Crude","asset_class":"COMMODITY","analysis_symbol":"BZ=F","ig_search_terms":["Brent Crude","Oil - Brent Crude"],"expected_types":["COMMODITIES"],"name_tokens":["BRENT"],"exposure_tags":["ENERGY","COMMODITIES"]},
    {"key":"COPPER","name":"Copper","asset_class":"COMMODITY","analysis_symbol":"HG=F","ig_search_terms":["Copper"],"expected_types":["COMMODITIES"],"name_tokens":["COPPER"],"exposure_tags":["INDUSTRIAL_METALS","COMMODITIES"]},
    {"key":"NATGAS","name":"Natural Gas","asset_class":"COMMODITY","analysis_symbol":"NG=F","ig_search_terms":["Natural Gas"],"expected_types":["COMMODITIES"],"name_tokens":["NATURAL","GAS"],"exposure_tags":["ENERGY","COMMODITIES"]},

    # -------------------------- CRYPTO -------------------------
    {"key":"BITCOIN","name":"Bitcoin","asset_class":"CRYPTO","analysis_symbol":"BTC-USD","ig_search_terms":["Bitcoin"],"expected_types":[],"name_tokens":["BITCOIN"],"exposure_tags":["CRYPTO","CRYPTO_LARGE_CAP"]},
    {"key":"ETHER","name":"Ether","asset_class":"CRYPTO","analysis_symbol":"ETH-USD","ig_search_terms":["Ether","Ethereum"],"expected_types":[],"name_tokens":["ETHER"],"exposure_tags":["CRYPTO","CRYPTO_LARGE_CAP"]},
    {"key":"SOLANA","name":"Solana","asset_class":"CRYPTO","analysis_symbol":"SOL-USD","ig_search_terms":["Solana"],"expected_types":[],"name_tokens":["SOLANA"],"exposure_tags":["CRYPTO","CRYPTO_ALT"]},
    {"key":"XRP","name":"XRP","asset_class":"CRYPTO","analysis_symbol":"XRP-USD","ig_search_terms":["XRP","Ripple"],"expected_types":[],"name_tokens":["XRP"],"exposure_tags":["CRYPTO","CRYPTO_ALT"]},
    {"key":"LITECOIN","name":"Litecoin","asset_class":"CRYPTO","analysis_symbol":"LTC-USD","ig_search_terms":["Litecoin"],"expected_types":[],"name_tokens":["LITECOIN"],"exposure_tags":["CRYPTO","CRYPTO_ALT"]},

    # -------------------------- SHARES -------------------------
    {"key":"AAPL","name":"Apple","asset_class":"SHARE","analysis_symbol":"AAPL","ig_search_terms":["Apple"],"expected_types":["SHARES"],"name_tokens":["APPLE"],"exposure_tags":["US_EQUITY","US_TECH","MEGA_CAP"]},
    {"key":"MSFT","name":"Microsoft","asset_class":"SHARE","analysis_symbol":"MSFT","ig_search_terms":["Microsoft"],"expected_types":["SHARES"],"name_tokens":["MICROSOFT"],"exposure_tags":["US_EQUITY","US_TECH","MEGA_CAP"]},
    {"key":"NVDA","name":"NVIDIA","asset_class":"SHARE","analysis_symbol":"NVDA","ig_search_terms":["NVIDIA"],"expected_types":["SHARES"],"name_tokens":["NVIDIA"],"exposure_tags":["US_EQUITY","US_TECH","SEMICONDUCTORS"]},
    {"key":"AMZN","name":"Amazon","asset_class":"SHARE","analysis_symbol":"AMZN","ig_search_terms":["Amazon"],"expected_types":["SHARES"],"name_tokens":["AMAZON"],"exposure_tags":["US_EQUITY","MEGA_CAP","CONSUMER_TECH"]},
    {"key":"GOOGL","name":"Alphabet","asset_class":"SHARE","analysis_symbol":"GOOGL","ig_search_terms":["Alphabet","Google"],"expected_types":["SHARES"],"name_tokens":["ALPHABET"],"exposure_tags":["US_EQUITY","US_TECH","MEGA_CAP"]},
    {"key":"META","name":"Meta Platforms","asset_class":"SHARE","analysis_symbol":"META","ig_search_terms":["Meta Platforms","Meta"],"expected_types":["SHARES"],"name_tokens":["META"],"exposure_tags":["US_EQUITY","US_TECH","MEGA_CAP"]},
    {"key":"TSLA","name":"Tesla","asset_class":"SHARE","analysis_symbol":"TSLA","ig_search_terms":["Tesla"],"expected_types":["SHARES"],"name_tokens":["TESLA"],"exposure_tags":["US_EQUITY","EV","GROWTH"]},
    {"key":"JPM","name":"JPMorgan Chase","asset_class":"SHARE","analysis_symbol":"JPM","ig_search_terms":["JPMorgan Chase","JP Morgan"],"expected_types":["SHARES"],"name_tokens":["JPMORGAN"],"exposure_tags":["US_EQUITY","US_FINANCIALS"]},
    {"key":"BAC","name":"Bank of America","asset_class":"SHARE","analysis_symbol":"BAC","ig_search_terms":["Bank of America"],"expected_types":["SHARES"],"name_tokens":["BANK","AMERICA"],"exposure_tags":["US_EQUITY","US_FINANCIALS"]},
    {"key":"XOM","name":"Exxon Mobil","asset_class":"SHARE","analysis_symbol":"XOM","ig_search_terms":["Exxon Mobil"],"expected_types":["SHARES"],"name_tokens":["EXXON"],"exposure_tags":["US_EQUITY","ENERGY"]},
    {"key":"CVX","name":"Chevron","asset_class":"SHARE","analysis_symbol":"CVX","ig_search_terms":["Chevron"],"expected_types":["SHARES"],"name_tokens":["CHEVRON"],"exposure_tags":["US_EQUITY","ENERGY"]},
    {"key":"NFLX","name":"Netflix","asset_class":"SHARE","analysis_symbol":"NFLX","ig_search_terms":["Netflix"],"expected_types":["SHARES"],"name_tokens":["NETFLIX"],"exposure_tags":["US_EQUITY","MEDIA","GROWTH"]},
    {"key":"AMD","name":"AMD","asset_class":"SHARE","analysis_symbol":"AMD","ig_search_terms":["Advanced Micro Devices","AMD"],"expected_types":["SHARES"],"name_tokens":["MICRO","DEVICES"],"exposure_tags":["US_EQUITY","US_TECH","SEMICONDUCTORS"]},
    {"key":"AVGO","name":"Broadcom","asset_class":"SHARE","analysis_symbol":"AVGO","ig_search_terms":["Broadcom"],"expected_types":["SHARES"],"name_tokens":["BROADCOM"],"exposure_tags":["US_EQUITY","US_TECH","SEMICONDUCTORS"]},
    {"key":"ORCL","name":"Oracle","asset_class":"SHARE","analysis_symbol":"ORCL","ig_search_terms":["Oracle"],"expected_types":["SHARES"],"name_tokens":["ORACLE"],"exposure_tags":["US_EQUITY","US_TECH"]},
    {"key":"KO","name":"Coca-Cola","asset_class":"SHARE","analysis_symbol":"KO","ig_search_terms":["Coca-Cola","Coca Cola"],"expected_types":["SHARES"],"name_tokens":["COCA"],"exposure_tags":["US_EQUITY","CONSUMER_DEFENSIVE"]},
    {"key":"WMT","name":"Walmart","asset_class":"SHARE","analysis_symbol":"WMT","ig_search_terms":["Walmart"],"expected_types":["SHARES"],"name_tokens":["WALMART"],"exposure_tags":["US_EQUITY","CONSUMER_DEFENSIVE"]},
    {"key":"NKE","name":"Nike","asset_class":"SHARE","analysis_symbol":"NKE","ig_search_terms":["Nike"],"expected_types":["SHARES"],"name_tokens":["NIKE"],"exposure_tags":["US_EQUITY","CONSUMER"]},
    {"key":"BABA","name":"Alibaba","asset_class":"SHARE","analysis_symbol":"BABA","ig_search_terms":["Alibaba"],"expected_types":["SHARES"],"name_tokens":["ALIBABA"],"exposure_tags":["CHINA_EQUITY","ECOMMERCE"]},
    {"key":"NPN","name":"Naspers","asset_class":"SHARE","analysis_symbol":"NPN.JO","ig_search_terms":["Naspers"],"expected_types":["SHARES"],"name_tokens":["NASPERS"],"exposure_tags":["ZA_EQUITY","INTERNET"]},

    # ---------------------------- ETFs -------------------------
    {"key":"SPY","name":"SPDR S&P 500 ETF","asset_class":"ETF","analysis_symbol":"SPY","ig_search_terms":["SPDR S&P 500","SPY"],"expected_types":["SHARES"],"name_tokens":["SPDR"],"exposure_tags":["US_EQUITY","GLOBAL_EQUITY","ETF"]},
    {"key":"QQQ","name":"Invesco QQQ","asset_class":"ETF","analysis_symbol":"QQQ","ig_search_terms":["Invesco QQQ","QQQ"],"expected_types":["SHARES"],"name_tokens":["QQQ"],"exposure_tags":["US_EQUITY","US_TECH","ETF"]},
    {"key":"IWM","name":"iShares Russell 2000 ETF","asset_class":"ETF","analysis_symbol":"IWM","ig_search_terms":["Russell 2000 ETF","IWM"],"expected_types":["SHARES"],"name_tokens":["RUSSELL"],"exposure_tags":["US_EQUITY","US_SMALL_CAP","ETF"]},

    # ----------------------- RATES / BONDS ---------------------
    {"key":"US10Y","name":"US 10-Year Treasury","asset_class":"RATE","analysis_symbol":"^TNX","ig_search_terms":["US 10-Year Treasury","US 10-Year T-Note"],"expected_types":["RATES"],"name_tokens":["10"],"exposure_tags":["USD_RATES","RATES"]},
]


class GlobalMarketEngine:
    VERSION = "6.8.19"

    def __init__(
        self,
        *,
        broker: Any,
        analysis_func: Callable[[Dict[str, Any]], Dict[str, Any]],
        state_path: str,
        scan_interval_seconds: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.broker = broker
        self.analysis_func = analysis_func
        self.state_path = state_path
        # Heavy analysis refresh remains slower than execution eligibility.
        # The 15-second opportunity loop below re-ranks cached evidence without
        # repeatedly downloading deep history from IG/public providers.
        self.scan_interval_seconds = max(
            30,
            int(
                scan_interval_seconds
                or os.getenv(
                    "GLOBAL_SCAN_INTERVAL_SECONDS",
                    "60",
                )
            ),
        )
        self.eligibility_refresh_seconds = max(
            10,
            int(
                os.getenv(
                    "GLOBAL_ELIGIBILITY_REFRESH_SECONDS",
                    "15",
                )
            ),
        )
        self.batch_size = max(
            2,
            min(
                12,
                int(
                    batch_size
                    or os.getenv(
                        "GLOBAL_SCAN_BATCH_SIZE",
                        "4",
                    )
                ),
            ),
        )
        self.candidate_ttl_seconds = max(
            300,
            int(os.getenv("GLOBAL_CANDIDATE_TTL_SECONDS", "1800")),
        )
        self.opportunity_board_limit = max(
            20,
            min(
                200,
                int(
                    os.getenv(
                        "GLOBAL_OPPORTUNITY_BOARD_LIMIT",
                        "100",
                    )
                ),
            ),
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = self._load_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "enabled": True,
            "offset": 0,
            "runs": 0,
            "last_run_at": None,
            "last_error": None,
            "evaluations": {},
            "last_batch_keys": [],
            "opportunity_board": [],
            "last_eligibility_refresh_at": None,
            "eligibility_refresh_runs": 0,
        }

    def _load_state(self) -> Dict[str, Any]:
        state = self._default_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    state.update(raw)
        except Exception:
            pass
        state["version"] = self.VERSION
        return state

    def _persist(self) -> None:
        try:
            directory = os.path.dirname(self.state_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, separators=(",", ":"), default=str)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self._state["last_error"] = f"persist: {type(exc).__name__}: {exc}"

    def universe(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in GLOBAL_MARKET_SEEDS]

    def _next_batch(self) -> List[Dict[str, Any]]:
        with self._lock:
            total = len(GLOBAL_MARKET_SEEDS)
            offset = int(self._state.get("offset") or 0) % max(1, total)
            rows = [
                GLOBAL_MARKET_SEEDS[(offset + i) % total]
                for i in range(min(self.batch_size, total))
            ]
            self._state["offset"] = (offset + len(rows)) % max(1, total)
            self._state["last_batch_keys"] = [r["key"] for r in rows]
            return [dict(r) for r in rows]

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            if number != number:
                return default
            return number
        except Exception:
            return default

    def _resolve_execution_market(self, seed: Dict[str, Any]) -> Dict[str, Any]:
        return self.broker.resolve_global_market(
            search_terms=list(seed.get("ig_search_terms") or [seed.get("name")]),
            expected_types=list(seed.get("expected_types") or []),
            name_tokens=list(seed.get("name_tokens") or []),
            require_tradeable=False,
            cache_key=str(seed.get("key") or seed.get("name") or ""),
        )

    def _evaluate_seed(self, seed: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        row: Dict[str, Any] = {
            **seed,
            "market": seed.get("name"),
            "symbol": seed.get("key"),
            "asset_class": seed.get("asset_class"),
            "evaluated_at": now,
            "intelligence_source": "GLOBAL_MULTI_MARKET",
            "direction": "WAIT",
            "live_direction": "WAIT",
            "direction_match": False,
            "quant_confidence": 0.0,
            "model_ai_confidence": 0.0,
            "smart_fast_score": 0.0,
            "quality_tier": "",
            "deep_status": "GLOBAL_REJECT",
            "ig_tradeable": False,
            "ig_epic": None,
            "rejection_reasons": [],
        }

        try:
            analysis = self.analysis_func(seed) or {}
            if not isinstance(analysis, dict):
                raise TypeError("analysis callback did not return an object")
            row.update({k: v for k, v in analysis.items() if k != "recent_returns"})
            recent_returns = analysis.get("recent_returns") or []
            if isinstance(recent_returns, list):
                row["recent_returns"] = [self._safe_float(x) for x in recent_returns[-120:]]
        except Exception as exc:
            row["analysis_error"] = f"{type(exc).__name__}: {exc}"
            row["reason"] = row["analysis_error"]
            return row

        direction = str(row.get("direction") or row.get("live_direction") or "WAIT").upper()
        row["direction"] = direction
        row["live_direction"] = direction
        row["direction_match"] = direction in {"BUY", "SELL"}

        # Conserve IG non-trading allowance: only resolve an execution market
        # after the public-data model has found a plausibly useful setup.
        promising = (
            direction in {"BUY", "SELL"}
            and self._safe_float(row.get("model_ai_confidence")) >= 0.35
            and self._safe_float(row.get("smart_fast_score")) >= 75.0
        )
        if promising:
            try:
                market = self._resolve_execution_market(seed)
                row["ig_epic"] = market.get("epic")
                row["ig_market_name"] = market.get("name")
                row["ig_instrument_type"] = market.get("instrument_type")
                row["ig_market_status"] = market.get("market_status")
                row["ig_tradeable"] = str(market.get("market_status") or "").upper() == "TRADEABLE"
                row["ig_min_deal_size"] = market.get("min_deal_size")
                row["ig_expiry"] = market.get("expiry")
            except Exception as exc:
                row["ig_preflight_error"] = f"{type(exc).__name__}: {exc}"

        return row

    def run_now(self) -> Dict[str, Any]:
        batch = self._next_batch()
        evaluations: Dict[str, Any] = {}
        error: Optional[str] = None
        for seed in batch:
            key = str(seed.get("key") or seed.get("name"))
            try:
                evaluations[key] = self._evaluate_seed(seed)
            except Exception as exc:
                evaluations[key] = {
                    **seed,
                    "market": seed.get("name"),
                    "symbol": key,
                    "evaluated_at": time.time(),
                    "deep_status": "GLOBAL_REJECT",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
                error = evaluations[key]["reason"]

        with self._lock:
            existing = self._state.setdefault("evaluations", {})
            existing.update(evaluations)
            self._state["runs"] = int(self._state.get("runs") or 0) + 1
            self._state["last_run_at"] = time.time()
            self._state["last_error"] = error
            self._persist()

        try:
            self.refresh_opportunity_board()
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = (
                    f"eligibility: {type(exc).__name__}: {exc}"
                )
                self._persist()

        return self.status()

    def _raw_candidates(
        self,
    ) -> List[Dict[str, Any]]:
        """Return persisted fresh evaluations before opportunity re-ranking."""
        now = time.time()
        with self._lock:
            rows = [
                dict(value)
                for value in (
                    self._state.get("evaluations")
                    or {}
                ).values()
                if isinstance(value, dict)
            ]

        fresh = [
            row
            for row in rows
            if (
                now
                - self._safe_float(
                    row.get("evaluated_at"),
                    0.0,
                )
                <= self.candidate_ttl_seconds
            )
        ]
        return fresh

    def refresh_opportunity_board(
        self,
    ) -> Dict[str, Any]:
        """Reassess cached market eligibility every ~15 seconds.

        This deliberately does NOT perform a full historical retrain every
        15 seconds. It continuously re-ranks the latest valid evidence and lets
        Compound perform exact IG quote/spread/tradeability preflight on the
        candidates it is actually considering for execution.
        """
        now = time.time()
        rows = self._raw_candidates()
        board: List[Dict[str, Any]] = []

        for raw in rows:
            row = dict(raw)
            age = max(
                0.0,
                now
                - self._safe_float(
                    row.get("evaluated_at"),
                    0.0,
                ),
            )
            freshness = max(
                0.0,
                min(
                    1.0,
                    1.0
                    - (
                        age
                        / max(
                            float(
                                self.candidate_ttl_seconds
                            ),
                            1.0,
                        )
                    ),
                ),
            )

            ai = max(
                0.0,
                min(
                    1.0,
                    self._safe_float(
                        row.get(
                            "model_ai_confidence"
                        ),
                        0.0,
                    ),
                ),
            )
            quant = max(
                0.0,
                min(
                    1.0,
                    self._safe_float(
                        row.get(
                            "quant_confidence"
                        ),
                        0.0,
                    ),
                ),
            )
            fast = max(
                0.0,
                min(
                    1.0,
                    self._safe_float(
                        row.get(
                            "smart_fast_score"
                        ),
                        0.0,
                    )
                    / 100.0,
                ),
            )

            historical_wr = self._safe_float(
                row.get("historical_win_rate"),
                0.50,
            )
            historical_wr = max(
                0.0,
                min(1.0, historical_wr),
            )

            pf = self._safe_float(
                row.get(
                    "historical_profit_factor"
                ),
                1.0,
            )
            pf_score = max(
                0.0,
                min(
                    1.0,
                    pf / 2.0,
                ),
            )

            quality = str(
                row.get("quality_tier") or ""
            ).upper()
            quality_score = (
                1.0
                if quality == "A+"
                else 0.92
                if quality == "A"
                else 0.35
                if quality in {"B", "B+"}
                else 0.20
            )
            prime_history_score = (
                1.0
                if (
                    historical_wr >= 0.55
                    and pf >= 1.20
                )
                else 0.20
                if (
                    historical_wr < 0.50
                    or pf < 1.0
                )
                else 0.55
            )

            direction_ok = (
                str(
                    row.get("direction") or ""
                ).upper()
                in {"BUY", "SELL"}
            )

            # Opportunity score ranks what should be checked first.
            # It does not override Compound's hard confidence/spread/correlation
            # gates.
            score = 100.0 * (
                0.22 * ai
                + 0.18 * quant
                + 0.18 * fast
                + 0.10 * historical_wr
                + 0.08 * pf_score
                + 0.10 * quality_score
                + 0.07 * prime_history_score
                + 0.07 * freshness
            )

            row["opportunity_score"] = round(
                score,
                2,
            )
            row["opportunity_freshness_pct"] = round(
                freshness * 100.0,
                2,
            )
            row["opportunity_age_seconds"] = round(
                age,
                2,
            )
            row["opportunity_direction_valid"] = (
                direction_ok
            )
            row["prime_rank_hint"] = bool(
                quality in {"A", "A+"}
                and historical_wr >= 0.55
                and pf >= 1.20
            )
            row["opportunity_last_checked_at"] = (
                now
            )
            board.append(row)

        board.sort(
            key=lambda row: (
                bool(
                    row.get(
                        "opportunity_direction_valid"
                    )
                ),
                self._safe_float(
                    row.get("opportunity_score"),
                    0.0,
                ),
                self._safe_float(
                    row.get(
                        "smart_fast_score"
                    ),
                    0.0,
                ),
                self._safe_float(
                    row.get(
                        "model_ai_confidence"
                    ),
                    0.0,
                ),
            ),
            reverse=True,
        )

        board = board[
            :self.opportunity_board_limit
        ]

        with self._lock:
            self._state[
                "opportunity_board"
            ] = board
            self._state[
                "last_eligibility_refresh_at"
            ] = now
            self._state[
                "eligibility_refresh_runs"
            ] = int(
                self._state.get(
                    "eligibility_refresh_runs"
                )
                or 0
            ) + 1
            self._persist()

        return {
            "version": self.VERSION,
            "count": len(board),
            "refreshed_at": now,
            "refresh_seconds":
                self.eligibility_refresh_seconds,
            "heavy_scan_seconds":
                self.scan_interval_seconds,
            "opportunities": board,
            "environment":
                "IG DEMO + PUBLIC ANALYSIS DATA",
            "live_money_execution": False,
        }

    def opportunity_board(
        self,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row)
                for row in (
                    self._state.get(
                        "opportunity_board"
                    )
                    or []
                )
                if isinstance(row, dict)
            ]

        if not rows:
            try:
                self.refresh_opportunity_board()
            except Exception:
                pass
            with self._lock:
                rows = [
                    dict(row)
                    for row in (
                        self._state.get(
                            "opportunity_board"
                        )
                        or []
                    )
                    if isinstance(row, dict)
                ]

        max_rows = (
            self.opportunity_board_limit
            if limit is None
            else max(
                1,
                min(
                    int(limit),
                    self.opportunity_board_limit,
                ),
            )
        )
        return rows[:max_rows]

    def candidates(self) -> List[Dict[str, Any]]:
        rows = self.opportunity_board()
        if rows:
            return rows

        # Safe bootstrap fallback before the first eligibility refresh.
        fresh = self._raw_candidates()
        fresh.sort(
            key=lambda row: (
                self._safe_float(
                    row.get("smart_fast_score")
                ),
                self._safe_float(
                    row.get("model_ai_confidence")
                ),
                self._safe_float(
                    row.get("quant_confidence")
                ),
            ),
            reverse=True,
        )
        return fresh

    def correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        rows = self.candidates()
        series: Dict[str, List[float]] = {}
        for row in rows:
            values = row.get("recent_returns") or []
            if isinstance(values, list) and len(values) >= 20:
                series[str(row.get("symbol") or row.get("key") or "").upper()] = [self._safe_float(v) for v in values]
        keys = list(series)
        matrix: Dict[str, Dict[str, float]] = {k: {} for k in keys}
        for left in keys:
            for right in keys:
                if left == right:
                    matrix[left][right] = 1.0
                    continue
                a = series[left]
                b = series[right]
                n = min(len(a), len(b))
                if n < 20:
                    matrix[left][right] = 0.0
                    continue
                a = a[-n:]
                b = b[-n:]
                ma = sum(a) / n
                mb = sum(b) / n
                cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
                va = sum((x - ma) ** 2 for x in a)
                vb = sum((y - mb) ** 2 for y in b)
                denom = (va * vb) ** 0.5
                matrix[left][right] = float(cov / denom) if denom > 0 else 0.0
        return matrix

    def status(self) -> Dict[str, Any]:
        rows = self.candidates()
        counts: Dict[str, int] = {}
        tradeable: Dict[str, int] = {}
        elite_ready = 0
        for row in rows:
            asset = str(row.get("asset_class") or "UNKNOWN").upper()
            counts[asset] = counts.get(asset, 0) + 1
            if row.get("ig_tradeable"):
                tradeable[asset] = tradeable.get(asset, 0) + 1
            if (
                str(row.get("direction") or "").upper() in {"BUY", "SELL"}
                and self._safe_float(row.get("model_ai_confidence")) >= 0.40
                and self._safe_float(row.get("quant_confidence")) >= 0.30
                and self._safe_float(row.get("smart_fast_score")) >= 90.0
                and str(row.get("quality_tier") or "").upper() in {"A", "A+"}
                and str(row.get("deep_status") or "").upper() in {"GLOBAL_VERIFIED", "GLOBAL_NEAR_VERIFIED"}
                and bool(row.get("ig_tradeable"))
            ):
                elite_ready += 1
        with self._lock:
            return {
                "version": self.VERSION,
                "name": "JASONG GLOBAL MULTI-MARKET INTELLIGENCE",
                "enabled": bool(self._state.get("enabled", True)),
                "universe_size": len(GLOBAL_MARKET_SEEDS) + 9,  # mature FX universe remains separate
                "global_non_fx_universe_size": len(GLOBAL_MARKET_SEEDS),
                "fx_universe_size": 9,
                "fresh_evaluations": len(rows),
                "evaluated_by_asset_class": counts,
                "tradeable_by_asset_class": tradeable,
                "elite_ready": elite_ready,
                "scan_interval_seconds":
                    self.scan_interval_seconds,
                "eligibility_refresh_seconds":
                    self.eligibility_refresh_seconds,
                "last_eligibility_refresh_at":
                    self._state.get(
                        "last_eligibility_refresh_at"
                    ),
                "eligibility_refresh_runs":
                    int(
                        self._state.get(
                            "eligibility_refresh_runs"
                        )
                        or 0
                    ),
                "opportunity_board_size":
                    len(
                        self._state.get(
                            "opportunity_board"
                        )
                        or []
                    ),
                "batch_size": self.batch_size,
                "runs": int(self._state.get("runs") or 0),
                "last_run_at": self._state.get("last_run_at"),
                "last_batch_keys": list(self._state.get("last_batch_keys") or []),
                "last_error": self._state.get("last_error"),
                "state_path": self.state_path,
                "environment": "IG DEMO + PUBLIC ANALYSIS DATA",
                "live_money_execution": False,
            }

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jasong-global-markets")
            self._thread.start()

    def _loop(self) -> None:
        # Short startup delay so Render can finish IG login / other engine startup.
        if self._stop.wait(12.0):
            return

        next_heavy_scan_at = 0.0

        while not self._stop.is_set():
            now = time.time()
            try:
                if self._state.get("enabled", True):
                    if now >= next_heavy_scan_at:
                        self.run_now()
                        next_heavy_scan_at = (
                            time.time()
                            + self.scan_interval_seconds
                        )
                    else:
                        # Continuous eligibility assessment uses the persistent
                        # cached evidence board; it does not retrain history.
                        self.refresh_opportunity_board()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._persist()

            self._stop.wait(
                self.eligibility_refresh_seconds
            )

    def stop_thread(self) -> None:
        self._stop.set()

