from __future__ import annotations

import json
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
                "last_error": self._last_error,
                "live_money_execution": False,
                "demo_execution": True,
            }

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
            "User-Agent": "Jasong-AI-Trader/6.6.IG-DEMO",
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

        req = urlrequest.Request(
            url=url,
            data=self._json_bytes(payload),
            headers=headers,
            method=method.upper(),
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

        search_terms = [
            f"{base}/{quote}",
            clean,
            f"{base} {quote}",
        ]

        rows = []
        seen_epics = set()
        for term in search_terms:
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

        if not rows:
            raise IGDemoError(
                f"IG DEMO has no market matching {base}/{quote}"
            )

        def score(row: Dict[str, Any]) -> tuple:
            name = self._market_name(row)
            market_type = self._market_type(row)
            status = str(row.get("marketStatus") or "").upper()
            exact_pair = (
                f"{base}/{quote}" in name
                or f"{base}{quote}" in name.replace(" ", "")
                or (
                    base in name
                    and quote in name
                )
            )
            is_currency = (
                "CURRENC" in market_type
                or "FOREX" in market_type
                or "FX" in market_type
            )
            is_tradeable = status == "TRADEABLE"
            is_dfb = str(row.get("expiry") or "").upper() in {"-", "DFB"}
            return (
                int(is_tradeable),
                int(is_currency),
                int(exact_pair),
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

            resolved = {
                "symbol": f"{base}/{quote}",
                "epic": epic,
                "expiry": (
                    instrument.get("expiry")
                    or candidate.get("expiry")
                    or "-"
                ),
                "name": (
                    instrument.get("name")
                    or candidate.get("instrumentName")
                    or candidate.get("name")
                ),
                "instrument_type": instrument_type,
                "market_status": status,
                "details": details,
            }
            self._market_cache[cache_key] = resolved
            self._market_cache_at[cache_key] = now
            return dict(resolved)

        raise IGDemoError(
            f"IG DEMO market {base}/{quote} is not currently tradeable"
        )

    def market_details(self, epic: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/markets/{urlparse.quote(epic, safe='')}",
            version=4,
        )

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

    def close_position(
        self,
        deal_id: str,
    ) -> Dict[str, Any]:
        item = self._find_open_position(deal_id)
        if item is None:
            return {
                "broker": "IG",
                "environment": "DEMO",
                "dealId": deal_id,
                "status": "ALREADY_CLOSED_OR_NOT_FOUND",
                "live_money_execution": False,
                "demo_execution": True,
            }

        position = item.get("position") or {}
        original_direction = str(
            position.get("direction") or ""
        ).upper()
        close_direction = (
            "SELL"
            if original_direction == "BUY"
            else "BUY"
        )
        size = float(position.get("dealSize") or 0.0)
        if size <= 0:
            raise IGDemoError(
                f"Invalid IG DEMO open size for deal {deal_id}"
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
        confirmation = (
            self.confirm(ref)
            if ref
            else {}
        )

        return {
            "broker": "IG",
            "environment": "DEMO",
            "dealId": deal_id,
            "dealReference": ref or None,
            "dealStatus": confirmation.get("dealStatus"),
            "status": confirmation.get("status") or "CLOSE_SUBMITTED",
            "level": confirmation.get("level"),
            "reason": confirmation.get("reason"),
            "live_money_execution": False,
            "demo_execution": True,
        }
