from __future__ import annotations

import json
from collections import deque
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


class IGDemoError(RuntimeError):
    """Safe IG DEMO integration error."""


@dataclass
class _Session:
    cst: str
    x_security_token: str
    account_id: str
    client_id: str
    connected_at: float
    last_used_at: float


class IGDemoBroker:
    """
    Strict IG DEMO REST broker.

    Safety design:
    - Base URL is hard-coded to IG DEMO.
    - No production/live IG base URL is accepted.
    - Credentials are read only from environment variables.
    - Session tokens are kept in memory and are never returned by public status().
    - Market resolution is cached to conserve IG REST allowances.
    """

    BASE_URL = "https://demo-api.ig.com/gateway/deal"

    def __init__(self) -> None:
        self.api_key = os.getenv("IG_DEMO_API_KEY", "").strip()
        self.identifier = os.getenv("IG_DEMO_IDENTIFIER", "").strip()
        self.password = os.getenv("IG_DEMO_PASSWORD", "")
        self.preferred_account_id = os.getenv(
            "IG_DEMO_ACCOUNT_ID", ""
        ).strip()

        self.default_size = self._float_env(
            "IG_DEMO_DEFAULT_SIZE",
            0.5,
            minimum=0.000001,
        )
        self.timeout_seconds = self._float_env(
            "IG_DEMO_HTTP_TIMEOUT_SECONDS",
            20.0,
            minimum=3.0,
        )

        # IG documents a per-account non-trading REST allowance. Keep our
        # own ceiling below IG's default so scans + market lookups cannot
        # starve broker execution with HTTP 403 account-allowance errors.
        self.nontrading_rpm = int(
            max(
                5,
                min(
                    25,
                    float(
                        os.getenv(
                            "IG_DEMO_NONTRADING_RPM",
                            "20",
                        )
                    ),
                ),
            )
        )
        self._nontrading_times = deque()
        self._rate_lock = threading.RLock()

        self._lock = threading.RLock()
        self._session: Optional[_Session] = None
        self._market_cache: Dict[str, Dict[str, Any]] = {}
        self._market_cache_at: Dict[str, float] = {}
        self._market_cache_ttl = 6 * 3600.0
        self._last_error: Optional[str] = None

    @staticmethod
    def _float_env(
        name: str,
        default: float,
        *,
        minimum: float,
    ) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(minimum, value)

    @staticmethod
    def _json_bytes(payload: Optional[Dict[str, Any]]) -> Optional[bytes]:
        if payload is None:
            return None
        return json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

    def configured(self) -> bool:
        return bool(
            self.api_key
            and self.identifier
            and self.password
        )

    def status(self) -> Dict[str, Any]:
        with self._lock:
            session = self._session
            return {
                "broker": "IG",
                "environment": "DEMO",
                "base_url": self.BASE_URL,
                "configured": self.configured(),
                "connected": session is not None,
                "account_id": (
                    session.account_id
                    if session is not None
                    else self.preferred_account_id or None
                ),
                "client_id": (
                    session.client_id
                    if session is not None
                    else None
                ),
                "default_size": self.default_size,
                "nontrading_rpm_guard": self.nontrading_rpm,
                "last_error": self._last_error,
                "live_money_execution": False,
                "demo_execution": True,
            }

    def _wait_for_nontrading_slot(self) -> None:
        """Throttle authenticated GET traffic below IG's account allowance."""
        while True:
            now = time.time()
            wait_for = 0.0

            with self._rate_lock:
                while (
                    self._nontrading_times
                    and now - self._nontrading_times[0] >= 60.0
                ):
                    self._nontrading_times.popleft()

                if len(self._nontrading_times) < self.nontrading_rpm:
                    self._nontrading_times.append(now)
                    return

                wait_for = max(
                    0.25,
                    60.0 - (
                        now - self._nontrading_times[0]
                    ) + 0.15,
                )

            time.sleep(wait_for)

    def _base_headers(
        self,
        *,
        version: int,
        authenticated: bool,
    ) -> Dict[str, str]:
        headers = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
            "Version": str(version),
            "User-Agent": "Jasong-AI-Trader/6.7.3-GLOBAL-MULTI-MARKET",
        }

        if authenticated:
            session = self._session
            if session is None:
                raise IGDemoError("IG DEMO session is not connected")
            headers["CST"] = session.cst
            headers["X-SECURITY-TOKEN"] = session.x_security_token

        return headers

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        version: int = 1,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> tuple[Dict[str, Any], Any]:
        if not self.configured():
            raise IGDemoError(
                "IG DEMO is not configured. Add IG_DEMO_API_KEY, "
                "IG_DEMO_IDENTIFIER and IG_DEMO_PASSWORD."
            )

        if not path.startswith("/"):
            path = "/" + path

        url = self.BASE_URL + path
        if query:
            clean_query = {
                key: value
                for key, value in query.items()
                if value is not None
            }
            url += "?" + urlparse.urlencode(clean_query)

        headers = self._base_headers(
            version=version,
            authenticated=authenticated,
        )
        if extra_headers:
            headers.update(extra_headers)

        clean_method = method.upper()

        if authenticated and clean_method == "GET":
            self._wait_for_nontrading_slot()

        req = urlrequest.Request(
            url=url,
            data=self._json_bytes(payload),
            headers=headers,
            method=clean_method,
        )

        try:
            with urlrequest.urlopen(
                req,
                timeout=self.timeout_seconds,
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data: Dict[str, Any]
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                        data = (
                            parsed
                            if isinstance(parsed, dict)
                            else {"data": parsed}
                        )
                    except Exception:
                        data = {"raw": raw}
                else:
                    data = {}

                return data, response.headers

        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            detail = body
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    detail = (
                        parsed.get("errorCode")
                        or parsed.get("error")
                        or parsed.get("message")
                        or body
                    )
            except Exception:
                pass

            raise IGDemoError(
                f"IG DEMO HTTP {exc.code}: {detail}"
            ) from exc

        except urlerror.URLError as exc:
            raise IGDemoError(
                f"IG DEMO network error: {exc.reason}"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        version: int = 1,
        payload: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        if authenticated and self._session is None:
            self.connect()

        try:
            data, _ = self._raw_request(
                method,
                path,
                version=version,
                payload=payload,
                query=query,
                authenticated=authenticated,
                extra_headers=extra_headers,
            )
            if authenticated and self._session is not None:
                self._session.last_used_at = time.time()
            self._last_error = None
            return data

        except IGDemoError as exc:
            message = str(exc)
            self._last_error = message

            auth_problem = (
                "HTTP 401" in message
                or "client-token" in message
                or "account-token" in message
                or "oauth-token" in message
            )
            if (
                authenticated
                and retry_auth
                and auth_problem
            ):
                with self._lock:
                    self._session = None
                self.connect(force=True)
                return self._request(
                    method,
                    path,
                    version=version,
                    payload=payload,
                    query=query,
                    authenticated=True,
                    extra_headers=extra_headers,
                    retry_auth=False,
                )
            raise

    def connect(self, *, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._session is not None and not force:
                return self.status()

            if not self.configured():
                raise IGDemoError(
                    "IG DEMO credentials are missing from environment."
                )

            data, headers = self._raw_request(
                "POST",
                "/session",
                version=2,
                payload={
                    "identifier": self.identifier,
                    "password": self.password,
                    "encryptedPassword": False,
                },
                authenticated=False,
            )

            cst = str(headers.get("CST") or "").strip()
            xst = str(
                headers.get("X-SECURITY-TOKEN") or ""
            ).strip()

            current_account_id = str(
                data.get("currentAccountId")
                or data.get("accountId")
                or ""
            ).strip()

            if not cst or not xst or not current_account_id:
                raise IGDemoError(
                    "IG DEMO login succeeded without the required "
                    "session/account tokens."
                )

            self._session = _Session(
                cst=cst,
                x_security_token=xst,
                account_id=current_account_id,
                client_id=str(data.get("clientId") or ""),
                connected_at=time.time(),
                last_used_at=time.time(),
            )

            rerouting = str(
                data.get("reroutingEnvironment") or "DEMO"
            ).upper()
            if rerouting not in {"DEMO", ""}:
                self._session = None
                raise IGDemoError(
                    f"Refusing non-DEMO IG environment: {rerouting}"
                )

            if (
                self.preferred_account_id
                and self.preferred_account_id != current_account_id
            ):
                switched, switched_headers = self._raw_request(
                    "PUT",
                    "/session",
                    version=1,
                    payload={
                        "accountId": self.preferred_account_id,
                        "defaultAccount": False,
                    },
                    authenticated=True,
                )

                new_xst = str(
                    switched_headers.get("X-SECURITY-TOKEN") or ""
                ).strip()
                if new_xst:
                    self._session.x_security_token = new_xst

                self._session.account_id = (
                    self.preferred_account_id
                )

                if switched.get("dealingEnabled") is False:
                    raise IGDemoError(
                        "Selected IG DEMO account is not dealing-enabled."
                    )

            self._last_error = None
            return {
                **self.status(),
                "dealing_enabled": data.get("dealingEnabled"),
                "account_currency": data.get("currencyIsoCode"),
            }

    def logout(self) -> Dict[str, Any]:
        with self._lock:
            if self._session is None:
                return self.status()
            try:
                self._request(
                    "DELETE",
                    "/session",
                    version=1,
                    retry_auth=False,
                )
            finally:
                self._session = None
        return self.status()

    def accounts(self) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/accounts",
            version=1,
        )

    def positions(self) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/positions",
            version=2,
        )

    @staticmethod
    def _normalise_symbol(symbol: str) -> tuple[str, str, str]:
        clean = (
            str(symbol or "")
            .upper()
            .strip()
            .replace("=X", "")
            .replace(" ", "")
        )
        clean = clean.replace("/", "")
        if len(clean) != 6 or not clean.isalpha():
            raise IGDemoError(
                f"Unsupported FX symbol format for IG DEMO: {symbol}"
            )
        base = clean[:3]
        quote = clean[3:]
        return clean, base, quote

    @staticmethod
    def _market_name(row: Dict[str, Any]) -> str:
        return str(
            row.get("instrumentName")
            or row.get("name")
            or ""
        ).upper()

    @staticmethod
    def _market_type(row: Dict[str, Any]) -> str:
        return str(
            row.get("instrumentType")
            or row.get("type")
            or ""
        ).upper()

    def resolve_market(
        self,
        symbol: str,
        *,
        require_tradeable: bool = True,
    ) -> Dict[str, Any]:
        clean, base, quote = self._normalise_symbol(symbol)
        cache_key = f"{clean}:{int(require_tradeable)}"
        now = time.time()

        cached = self._market_cache.get(cache_key)
        cached_at = self._market_cache_at.get(cache_key, 0.0)
        if cached and now - cached_at < self._market_cache_ttl:
            return dict(cached)

        # Start with the exact human FX pair. IG market search is fuzzy, and
        # querying three variants for every scan caused unnecessary REST bursts.
        # Only fall back to compact/spaced forms when the exact query returns
        # nothing usable.
        search_terms = [
            f"{base}/{quote}",
            clean,
            f"{base} {quote}",
        ]

        rows = []
        seen_epics = set()
        for term_index, term in enumerate(search_terms):
            response = self._request(
                "GET",
                "/markets",
                version=1,
                query={"searchTerm": term},
            )
            for row in response.get("markets", []) or []:
                if not isinstance(row, dict):
                    continue
                epic = str(row.get("epic") or "")
                if epic and epic not in seen_epics:
                    rows.append(dict(row))
                    seen_epics.add(epic)

            # The exact slash query normally returns the correct FX family.
            # Avoid two more account-allowance-consuming calls when it does.
            if rows and term_index == 0:
                break

        if not rows:
            raise IGDemoError(
                f"IG DEMO has no market matching {base}/{quote}"
            )

        def _letters(value: Any) -> str:
            return "".join(
                ch for ch in str(value or "").upper()
                if ch.isalpha()
            )

        def score(row: Dict[str, Any]) -> tuple:
            name = self._market_name(row)
            market_type = self._market_type(row)
            status = str(row.get("marketStatus") or "").upper()
            epic = str(row.get("epic") or "").upper()
            name_letters = _letters(name)
            epic_letters = _letters(epic)
            exact_pair = (
                name_letters.startswith(clean)
                or clean in epic_letters
            )
            is_currency = (
                "CURRENC" in market_type
                or "FOREX" in market_type
                or "FX" in market_type
            )
            is_tradeable = status == "TRADEABLE"
            is_dfb = str(row.get("expiry") or "").upper() in {"-", "DFB"}
            return (
                int(exact_pair),
                int(is_tradeable),
                int(is_currency),
                int(is_dfb),
            )

        rows.sort(key=score, reverse=True)

        for candidate in rows:
            epic = str(candidate.get("epic") or "").strip()
            if not epic:
                continue

            details = self.market_details(epic)
            instrument = details.get("instrument") or {}
            snapshot = details.get("snapshot") or {}

            status = str(
                snapshot.get("marketStatus")
                or candidate.get("marketStatus")
                or ""
            ).upper()

            if (
                require_tradeable
                and status != "TRADEABLE"
            ):
                continue

            instrument_type = str(
                instrument.get("type")
                or candidate.get("instrumentType")
                or ""
            ).upper()
            if instrument_type and "CURRENC" not in instrument_type:
                continue

            # CRITICAL SAFETY CHECK:
            # IG /markets search is fuzzy. Never execute a returned market
            # unless its actual instrument metadata matches the requested FX pair.
            instrument_name = str(
                instrument.get("name")
                or candidate.get("instrumentName")
                or candidate.get("name")
                or ""
            )
            market_id = str(instrument.get("marketId") or "")
            chart_code = str(instrument.get("chartCode") or "")

            name_letters = _letters(instrument_name)
            market_id_letters = _letters(market_id)
            chart_code_letters = _letters(chart_code)
            epic_letters = _letters(epic)

            exact_instrument_match = (
                name_letters.startswith(clean)
                or market_id_letters == clean
                or chart_code_letters == clean
                or clean in epic_letters
            )
            if not exact_instrument_match:
                continue

            resolved = {
                "symbol": f"{base}/{quote}",
                "epic": epic,
                "expiry": (
                    instrument.get("expiry")
                    or candidate.get("expiry")
                    or "-"
                ),
                "name": instrument_name,
                "instrument_type": instrument_type,
                "market_status": status,
                "ig_market_id": market_id or None,
                "ig_chart_code": chart_code or None,
                "exact_pair_match": True,
                "details": details,
            }
            self._market_cache[cache_key] = resolved
            self._market_cache_at[cache_key] = now
            return dict(resolved)

        raise IGDemoError(
            f"IG DEMO has no exact tradeable market for {base}/{quote}; "
            "fuzzy market matches were rejected"
        )

    @staticmethod
    def extract_snapshot_quote(details: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """Extract bid/offer from IG v3 or v4 market snapshots."""
        snapshot = (details or {}).get("snapshot") or {}

        def _num(value: Any) -> Optional[float]:
            try:
                if value is None or value == "":
                    return None
                number = float(value)
                return number if number == number else None
            except Exception:
                return None

        bid = _num(snapshot.get("bid"))
        offer = _num(
            snapshot.get("offer")
            if snapshot.get("offer") is not None
            else snapshot.get("ask")
        )

        if bid is None or offer is None:
            ladder = snapshot.get("priceLadder") or []
            if isinstance(ladder, list) and ladder:
                first = ladder[0] if isinstance(ladder[0], dict) else {}
                if bid is None:
                    bid = _num(first.get("bid"))
                if offer is None:
                    offer = _num(
                        first.get("ask")
                        if first.get("ask") is not None
                        else first.get("offer")
                    )

        return {"bid": bid, "offer": offer}

    def market_details(
        self,
        epic: str,
        *,
        require_quote: bool = False,
    ) -> Dict[str, Any]:
        path = f"/markets/{urlparse.quote(epic, safe='')}"
        details = self._request("GET", path, version=4)

        if require_quote:
            quote = self.extract_snapshot_quote(details)
            if quote.get("bid") is None or quote.get("offer") is None:
                try:
                    v3 = self._request("GET", path, version=3)
                    q3 = self.extract_snapshot_quote(v3)
                    if q3.get("bid") is not None and q3.get("offer") is not None:
                        merged = dict(details)
                        merged["_quote_fallback_v3"] = True
                        merged["_quote_snapshot"] = {
                            "bid": q3["bid"],
                            "offer": q3["offer"],
                        }
                        snap = dict(merged.get("snapshot") or {})
                        v3snap = v3.get("snapshot") or {}
                        if not snap.get("marketStatus") and v3snap.get("marketStatus"):
                            snap["marketStatus"] = v3snap.get("marketStatus")
                        merged["snapshot"] = snap
                        return merged
                except Exception:
                    pass

        return details


    def search_markets(
        self,
        search_term: str,
    ) -> Dict[str, Any]:
        """Search IG DEMO markets using IG's own market search endpoint."""
        term = str(search_term or "").strip()
        if not term:
            raise IGDemoError("IG DEMO market search term is empty")
        return self._request(
            "GET",
            "/markets",
            version=1,
            query={"searchTerm": term},
        )

    @staticmethod
    def _letters_words(value: Any) -> tuple[str, set[str]]:
        text = str(value or "").upper()
        letters = "".join(ch for ch in text if ch.isalnum())
        words = {
            "".join(ch for ch in word if ch.isalnum())
            for word in text.replace("/", " ").replace("-", " ").split()
        }
        words.discard("")
        return letters, words

    @staticmethod
    def _instrument_type_family(value: Any) -> str:
        """Normalise IG instrument type variants into broad safe families."""
        raw = str(value or "").upper().strip()
        if "CURRENC" in raw or "FOREX" in raw or raw == "FX":
            return "FX"
        if "SHARE" in raw or "STOCK" in raw or "EQUITY" in raw:
            return "SHARE"
        if "INDICE" in raw or raw == "INDEX":
            return "INDEX"
        if "COMMOD" in raw:
            return "COMMODITY"
        if "CRYPTO" in raw or "BITCOIN" in raw or "ETHER" in raw:
            return "CRYPTO"
        if "ETF" in raw or "FUND" in raw:
            return "ETF"
        return raw

    @classmethod
    def _instrument_type_allowed(
        cls,
        actual: Any,
        expected_types: Optional[list[str]],
    ) -> bool:
        if not expected_types:
            return True
        actual_family = cls._instrument_type_family(actual)
        expected_families = {
            cls._instrument_type_family(x)
            for x in (expected_types or [])
            if str(x or "").strip()
        }
        return actual_family in expected_families

    def resolve_global_market(
        self,
        *,
        search_terms: list[str],
        expected_types: Optional[list[str]] = None,
        name_tokens: Optional[list[str]] = None,
        require_tradeable: bool = True,
        cache_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve a non-FX IG market safely from one or more search terms.

        Unlike resolve_market(), this method is not limited to six-letter FX
        pairs.  It still refuses obviously unrelated fuzzy search hits by
        scoring the actual instrument name/type returned by /markets/{epic}.
        """
        terms = [str(x or "").strip() for x in (search_terms or []) if str(x or "").strip()]
        if not terms:
            raise IGDemoError("No IG DEMO global market search terms supplied")

        allowed_types = {str(x or "").upper().strip() for x in (expected_types or []) if str(x or "").strip()}
        tokens = [str(x or "").upper().strip() for x in (name_tokens or []) if str(x or "").strip()]
        ck = str(cache_key or "|".join(terms)).upper().strip()
        cache_id = f"GLOBAL:{ck}:{int(require_tradeable)}"
        now = time.time()
        cached = self._market_cache.get(cache_id)
        cached_at = self._market_cache_at.get(cache_id, 0.0)
        if cached and now - cached_at < self._market_cache_ttl:
            return dict(cached)

        rows: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for term in terms[:3]:
            response = self.search_markets(term)
            for raw in response.get("markets", []) or []:
                if not isinstance(raw, dict):
                    continue
                epic = str(raw.get("epic") or "").strip()
                if not epic or epic in seen:
                    continue
                seen.add(epic)
                rows.append(dict(raw))
            if rows:
                # Search is account-rate-limited.  Prefer one good query and
                # only consume fallbacks when the first query found nothing.
                break

        if not rows:
            raise IGDemoError(f"IG DEMO has no market matching {terms[0]}")

        def row_score(row: Dict[str, Any]) -> tuple:
            name = str(row.get("instrumentName") or row.get("name") or "").upper()
            market_type = str(row.get("instrumentType") or row.get("type") or "").upper()
            status = str(row.get("marketStatus") or "").upper()
            expiry = str(row.get("expiry") or "").upper()
            name_letters, name_words = self._letters_words(name)
            token_hits = 0
            for token in tokens:
                token_letters = "".join(ch for ch in token if ch.isalnum())
                if token_letters and (token_letters in name_letters or token_letters in name_words):
                    token_hits += 1
            type_ok = self._instrument_type_allowed(
                market_type,
                list(allowed_types),
            )
            banned = any(x in market_type for x in ("OPT_", "BINARY", "SPRINT", "KNOCKOUT", "BUNGEE"))
            cash_like = expiry in {"", "-", "DFB"} or "CASH" in name
            return (
                token_hits,
                int(type_ok),
                int(status == "TRADEABLE"),
                int(cash_like),
                -int(banned),
            )

        rows.sort(key=row_score, reverse=True)

        for candidate in rows[:12]:
            epic = str(candidate.get("epic") or "").strip()
            if not epic:
                continue
            try:
                details = self.market_details(epic)
            except Exception:
                continue
            instrument = details.get("instrument") or {}
            snapshot = details.get("snapshot") or {}
            status = str(snapshot.get("marketStatus") or candidate.get("marketStatus") or "").upper()
            instrument_type = str(instrument.get("type") or candidate.get("instrumentType") or "").upper()
            if not self._instrument_type_allowed(
                instrument_type,
                list(allowed_types),
            ):
                continue
            if any(x in instrument_type for x in ("OPT_", "BINARY", "SPRINT", "KNOCKOUT", "BUNGEE")):
                continue
            if require_tradeable and status != "TRADEABLE":
                continue

            instrument_name = str(instrument.get("name") or candidate.get("instrumentName") or candidate.get("name") or "")
            name_letters, _ = self._letters_words(instrument_name)
            # At least one explicit token must match when tokens were supplied.
            if tokens:
                matched = False
                for token in tokens:
                    token_letters = "".join(ch for ch in token if ch.isalnum())
                    if token_letters and token_letters in name_letters:
                        matched = True
                        break
                if not matched:
                    continue

            expiry = instrument.get("expiry") or candidate.get("expiry") or "-"
            resolved = {
                "symbol": ck,
                "epic": epic,
                "expiry": expiry,
                "name": instrument_name,
                "instrument_type": instrument_type,
                "instrument_family": self._instrument_type_family(instrument_type),
                "market_status": status,
                "details": details,
                "min_deal_size": self._min_deal_size(details),
                "streaming_prices_available": instrument.get("streamingPricesAvailable"),
                "unit": instrument.get("unit"),
                "value_of_one_pip": instrument.get("valueOfOnePip"),
            }
            self._market_cache[cache_id] = resolved
            self._market_cache_at[cache_id] = now
            return dict(resolved)

        raise IGDemoError(
            f"IG DEMO search results for {terms[0]} did not contain a safe matching instrument"
        )

    def historical_prices_epic(
        self,
        epic: str,
        *,
        resolution: str = "MINUTE_15",
        num_points: int = 160,
    ) -> Dict[str, Any]:
        clean_epic = str(epic or "").strip()
        if not clean_epic:
            raise IGDemoError("IG DEMO historical price EPIC is empty")
        points = max(1, min(int(num_points), 500))
        response = self._request(
            "GET",
            f"/prices/{urlparse.quote(clean_epic, safe='')}/{urlparse.quote(str(resolution), safe='')}/{points}",
            version=2,
        )
        return {
            "broker": "IG",
            "environment": "DEMO",
            "epic": clean_epic,
            "resolution": str(resolution),
            "requested_points": points,
            "prices": response.get("prices") or [],
            "metadata": response.get("metadata") or {},
            "live_money_execution": False,
        }

    def open_epic_position(
        self,
        *,
        epic: str,
        direction: str,
        size: Optional[float] = None,
        deal_reference: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open an IG DEMO position on an already-resolved EPIC."""
        direction = str(direction or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            raise IGDemoError("Direction must be BUY or SELL")
        clean_epic = str(epic or "").strip()
        if not clean_epic:
            raise IGDemoError("IG DEMO EPIC is empty")

        details = self.market_details(clean_epic)
        instrument = details.get("instrument") or {}
        snapshot = details.get("snapshot") or {}
        market_status = str(snapshot.get("marketStatus") or "").upper()
        if market_status != "TRADEABLE":
            raise IGDemoError(f"IG DEMO market is not tradeable: {market_status or 'UNKNOWN'}")

        instrument_type = str(instrument.get("type") or "").upper()
        if any(x in instrument_type for x in ("OPT_", "BINARY", "SPRINT", "KNOCKOUT", "BUNGEE")):
            raise IGDemoError(f"Unsupported IG DEMO instrument type for autonomous execution: {instrument_type}")

        requested_size = self.default_size if size is None else float(size)
        min_size = self._min_deal_size(details)
        final_size = max(requested_size, min_size)
        payload: Dict[str, Any] = {
            "currencyCode": self._default_currency(instrument),
            "dealReference": (deal_reference or f"JSCMP_{uuid.uuid4().hex[:20]}")[:30],
            "direction": direction,
            "epic": clean_epic,
            "expiry": str(instrument.get("expiry") or "-"),
            "forceOpen": True,
            "guaranteedStop": False,
            "orderType": "MARKET",
            "size": round(final_size, 12),
        }
        acknowledgement = self._request("POST", "/positions/otc", version=2, payload=payload)
        ref = str(acknowledgement.get("dealReference") or payload["dealReference"])
        confirmation = self.confirm(ref)
        if str(confirmation.get("dealStatus") or "").upper() == "REJECTED":
            raise IGDemoError(
                "IG DEMO rejected order: " + str(confirmation.get("reason") or confirmation)
            )
        return {
            "broker": "IG",
            "environment": "DEMO",
            "symbol": str(instrument.get("name") or clean_epic),
            "epic": clean_epic,
            "instrument_type": instrument_type,
            "market_status": market_status,
            "direction": direction,
            "requestedSize": requested_size,
            "minimumSize": min_size,
            "size": final_size,
            "dealReference": ref,
            "dealId": confirmation.get("dealId"),
            "level": confirmation.get("level"),
            "dealStatus": confirmation.get("dealStatus"),
            "reason": confirmation.get("reason"),
            "live_money_execution": False,
        }

    def historical_prices(
        self,
        symbol: str,
        *,
        resolution: str = "MINUTE_15",
        num_points: int = 160,
    ) -> Dict[str, Any]:
        """Return IG DEMO historical candles for an exact FX pair.

        Uses IG's DEMO-only /prices endpoint. Market resolution still goes
        through the exact-pair safety check before any price request is made.
        """
        market = self.resolve_market(
            symbol,
            require_tradeable=False,
        )

        points = max(
            1,
            min(
                int(num_points),
                500,
            ),
        )

        epic = str(
            market.get("epic")
            or ""
        ).strip()

        if not epic:
            raise IGDemoError(
                f"IG DEMO market has no EPIC for {symbol}"
            )

        response = self._request(
            "GET",
            (
                f"/prices/"
                f"{urlparse.quote(epic, safe='')}/"
                f"{urlparse.quote(str(resolution), safe='')}/"
                f"{points}"
            ),
            version=2,
        )

        return {
            "broker": "IG",
            "environment": "DEMO",
            "symbol": market.get("symbol"),
            "epic": epic,
            "resolution": str(resolution),
            "requested_points": points,
            "prices": response.get("prices") or [],
            "metadata": response.get("metadata") or {},
            "live_money_execution": False,
        }

    @staticmethod
    def _default_currency(instrument: Dict[str, Any]) -> str:
        currencies = instrument.get("currencies") or []
        for item in currencies:
            if isinstance(item, dict) and item.get("isDefault"):
                code = str(item.get("code") or "").upper()
                if len(code) == 3:
                    return code
        for item in currencies:
            if isinstance(item, dict):
                code = str(item.get("code") or "").upper()
                if len(code) == 3:
                    return code
        raise IGDemoError(
            "IG DEMO market has no valid order currency"
        )

    @staticmethod
    def _min_deal_size(details: Dict[str, Any]) -> float:
        rules = details.get("dealingRules") or {}
        min_size = rules.get("minDealSize") or {}
        try:
            return max(0.0, float(min_size.get("value") or 0.0))
        except Exception:
            return 0.0

    def confirm(
        self,
        deal_reference: str,
        *,
        timeout_seconds: float = 12.0,
    ) -> Dict[str, Any]:
        deadline = time.time() + max(1.0, timeout_seconds)
        last: Dict[str, Any] = {}

        while time.time() < deadline:
            try:
                last = self._request(
                    "GET",
                    f"/confirms/{urlparse.quote(deal_reference, safe='')}",
                    version=1,
                )
            except IGDemoError as exc:
                # Confirm may briefly be unavailable after acknowledgement.
                if "404" not in str(exc):
                    raise
                time.sleep(0.5)
                continue

            if last:
                return last
            time.sleep(0.5)

        return {
            "dealReference": deal_reference,
            "dealStatus": "PENDING_CONFIRMATION",
        }

    def open_market_position(
        self,
        *,
        symbol: str,
        direction: str,
        size: Optional[float] = None,
        stop_distance: Optional[float] = None,
        limit_distance: Optional[float] = None,
        deal_reference: Optional[str] = None,
    ) -> Dict[str, Any]:
        direction = str(direction or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            raise IGDemoError("Direction must be BUY or SELL")

        market = self.resolve_market(
            symbol,
            require_tradeable=True,
        )
        details = market["details"]
        instrument = details.get("instrument") or {}

        requested_size = (
            self.default_size
            if size is None
            else float(size)
        )
        min_size = self._min_deal_size(details)
        final_size = max(requested_size, min_size)

        payload: Dict[str, Any] = {
            "currencyCode": self._default_currency(instrument),
            "dealReference": (
                deal_reference
                or f"JASONG_{uuid.uuid4().hex[:20]}"
            )[:30],
            "direction": direction,
            "epic": market["epic"],
            "expiry": str(market["expiry"] or "-"),
            "forceOpen": True,
            "guaranteedStop": False,
            "orderType": "MARKET",
            "size": round(final_size, 12),
        }

        if stop_distance is not None:
            payload["stopDistance"] = float(stop_distance)
        if limit_distance is not None:
            payload["limitDistance"] = float(limit_distance)

        acknowledgement = self._request(
            "POST",
            "/positions/otc",
            version=2,
            payload=payload,
        )
        ref = str(
            acknowledgement.get("dealReference")
            or payload["dealReference"]
        )

        confirmation = self.confirm(ref)
        deal_status = str(
            confirmation.get("dealStatus")
            or ""
        ).upper()

        if deal_status == "REJECTED":
            raise IGDemoError(
                "IG DEMO rejected order: "
                f"{confirmation.get('reason') or confirmation}"
            )

        return {
            "broker": "IG",
            "environment": "DEMO",
            "symbol": market["symbol"],
            "epic": market["epic"],
            "direction": direction,
            "size": payload["size"],
            "currencyCode": payload["currencyCode"],
            "dealReference": ref,
            "dealId": confirmation.get("dealId"),
            "dealStatus": (
                deal_status or "PENDING_CONFIRMATION"
            ),
            "status": confirmation.get("status"),
            "level": confirmation.get("level"),
            "reason": confirmation.get("reason"),
            "live_money_execution": False,
            "demo_execution": True,
        }

    def _find_open_position(
        self,
        deal_id: str,
    ) -> Optional[Dict[str, Any]]:
        response = self.positions()
        for item in response.get("positions", []) or []:
            if not isinstance(item, dict):
                continue
            position = item.get("position") or {}
            if str(position.get("dealId") or "") == deal_id:
                return item
        return None

    @staticmethod
    def _open_position_size(
        position: Dict[str, Any],
    ) -> float:
        """Return the current REST open size across IG position API versions.

        IG /positions v2 uses `position.size`; v1 used `position.dealSize`.
        The broker reads /positions with version=2, so `size` must be the
        primary field. Keeping the fallback preserves compatibility with old
        payloads and persisted test fixtures.
        """
        raw = (
            position.get("size")
            if position.get("size") is not None
            else position.get("dealSize")
        )
        try:
            value = float(raw or 0.0)
        except Exception:
            value = 0.0
        return value if value > 0 else 0.0

    @staticmethod
    def _market_status_from_position_item(
        item: Dict[str, Any],
    ) -> tuple[str, str]:
        """Return (market_status, epic) from an IG /positions row."""
        market = item.get("market") or {}
        position = item.get("position") or {}
        if not isinstance(market, dict):
            market = {}
        if not isinstance(position, dict):
            position = {}

        status = str(
            market.get("marketStatus")
            or ""
        ).upper().strip()
        epic = str(
            market.get("epic")
            or position.get("epic")
            or ""
        ).strip()
        return status, epic

    @staticmethod
    def _close_allowed_market_status(
        market_status: str,
    ) -> bool:
        """Only submit a market close when IG says closings are possible."""
        return str(
            market_status
            or ""
        ).upper().strip() in {
            "TRADEABLE",
            "CLOSINGS_ONLY",
        }

    def close_position(
        self,
        deal_id: str,
    ) -> Dict[str, Any]:
        """Close exactly the broker-reported remaining IG DEMO position size.

        Safety / integrity behaviour:
        - fetches authoritative /positions immediately before closing;
        - uses v2 `position.size` (with v1 `dealSize` fallback);
        - submits one close request for that exact remaining size;
        - treats an explicit IG rejection as an error;
        - re-reads /positions to verify whether the deal disappeared;
        - never reports a verified close merely because the DELETE request
          was accepted.

        If IG has accepted the close but the position is still visible during
        the short verification window, the caller receives CLOSE_PENDING and
        should reconcile on its next normal broker-sync tick instead of
        blindly submitting duplicate close requests in a tight loop.
        """
        deal_id = str(deal_id or "").strip()
        if not deal_id:
            raise IGDemoError("IG DEMO close_position requires deal_id")

        item = self._find_open_position(deal_id)
        if item is None:
            return {
                "broker": "IG",
                "environment": "DEMO",
                "dealId": deal_id,
                "status": "ALREADY_CLOSED_OR_NOT_FOUND",
                "dealStatus": "ACCEPTED",
                "requestedCloseSize": 0.0,
                "remainingSize": 0.0,
                "closeVerified": True,
                "live_money_execution": False,
                "demo_execution": True,
            }

        market_status, epic = (
            self._market_status_from_position_item(
                item
            )
        )

        # V6.7.2a: a due close is not an execution failure when the underlying
        # weekday market is unavailable. Avoid sending DELETE instructions
        # that IG will reject with MARKET_CLOSED_WITH_EDITS. The normal broker
        # reconciliation loop will re-read /positions; once marketStatus becomes
        # TRADEABLE or CLOSINGS_ONLY, the close is submitted automatically.
        if (
            market_status
            and not self._close_allowed_market_status(
                market_status
            )
        ):
            return {
                "broker": "IG",
                "environment": "DEMO",
                "dealId": deal_id,
                "epic": epic or None,
                "marketStatus": market_status,
                "status": "CLOSE_DEFERRED_MARKET_CLOSED",
                "dealStatus": "DEFERRED",
                "requestedCloseSize": 0.0,
                "remainingSize": self._open_position_size(
                    item.get("position") or {}
                ),
                "closeVerified": False,
                "closeDeferred": True,
                "deferredReason": (
                    "IG market is not currently open for position closing"
                ),
                "live_money_execution": False,
                "demo_execution": True,
            }

        position = item.get("position") or {}
        original_direction = str(
            position.get("direction") or ""
        ).upper()
        if original_direction not in {"BUY", "SELL"}:
            raise IGDemoError(
                f"Invalid IG DEMO direction for deal {deal_id}: "
                f"{original_direction or '-'}"
            )

        close_direction = (
            "SELL"
            if original_direction == "BUY"
            else "BUY"
        )
        size = self._open_position_size(position)
        if size <= 0:
            # Include both supported field names in the diagnostic without
            # exposing credentials/tokens.
            raise IGDemoError(
                "Invalid IG DEMO open size for deal "
                f"{deal_id}; v2 size={position.get('size')!r}, "
                f"v1 dealSize={position.get('dealSize')!r}"
            )

        acknowledgement = self._request(
            "POST",
            "/positions/otc",
            version=1,
            payload={
                "dealId": deal_id,
                "direction": close_direction,
                "orderType": "MARKET",
                "size": size,
            },
            extra_headers={"_method": "DELETE"},
        )

        ref = str(
            acknowledgement.get("dealReference")
            or ""
        )

        confirmation: Dict[str, Any] = {}
        confirmation_error: Optional[str] = None
        if ref:
            try:
                confirmation = self.confirm(ref)
            except Exception as exc:
                # A close acknowledgement can still be valid even when the
                # confirm endpoint is temporarily unavailable/rate-limited.
                # Broker reconciliation below remains the source of truth.
                confirmation_error = (
                    f"{type(exc).__name__}: {exc}"
                )

        deal_status = str(
            confirmation.get("dealStatus")
            or ""
        ).upper()
        if deal_status == "REJECTED":
            raise IGDemoError(
                "IG DEMO rejected close: "
                f"{confirmation.get('reason') or confirmation}"
            )

        close_verified = False
        remaining_size: Optional[float] = None
        verification_checks = 0

        # Short read-after-write verification only. If IG is eventually
        # consistent, the normal 15-second broker reconciliation will finish
        # the job without generating duplicate close instructions.
        for delay in (0.20, 0.45, 0.80):
            time.sleep(delay)
            verification_checks += 1
            current = self._find_open_position(deal_id)
            if current is None:
                remaining_size = 0.0
                close_verified = True
                break
            current_position = current.get("position") or {}
            remaining_size = self._open_position_size(
                current_position
            )

        if remaining_size is None:
            remaining_size = size

        status = (
            "CLOSED_VERIFIED"
            if close_verified
            else "CLOSE_PENDING"
        )

        return {
            "broker": "IG",
            "environment": "DEMO",
            "dealId": deal_id,
            "dealReference": ref or None,
            "dealStatus": (
                confirmation.get("dealStatus")
                or (
                    "ACCEPTED"
                    if ref
                    else None
                )
            ),
            "status": status,
            "confirmationStatus": confirmation.get("status"),
            "level": confirmation.get("level"),
            "reason": confirmation.get("reason"),
            "requestedCloseSize": size,
            "openSizeBefore": size,
            "remainingSize": remaining_size,
            "closeVerified": close_verified,
            "verificationChecks": verification_checks,
            "confirmationError": confirmation_error,
            "positionSizeSource": (
                "size"
                if position.get("size") is not None
                else "dealSize"
            ),
            "live_money_execution": False,
            "demo_execution": True,
        }
