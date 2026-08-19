from __future__ import annotations

"""Authenticated read-only MCP bridge for Jasong AI Trader V6.9.3.

The bridge intentionally exposes diagnostics and trading state only. It does
not expose order-entry, order-close, strategy mutation, or broker credential
functions. OAuth 2.1-style authorization-code + PKCE and refresh-token routes
are hosted by the existing FastAPI process so ChatGPT-compatible MCP clients
can authenticate without placing broker secrets in the mobile application.

A separate static bearer token can optionally be configured for direct OpenAI
API / MCP Inspector testing. The static token is never returned by an endpoint.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations
except Exception:  # pragma: no cover - deployment dependency guard
    MCPServer = None  # type: ignore[assignment]
    TransportSecuritySettings = None  # type: ignore[assignment]
    ToolAnnotations = None  # type: ignore[assignment]


VERSION = "6.9.3"
READ_SCOPE = "jasong.read"
OFFLINE_SCOPE = "offline_access"
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
AUTH_CODE_TTL_SECONDS = 5 * 60

_RATE_LOCK = threading.RLock()
_RATE_EVENTS: Dict[str, List[float]] = {}


def _request_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", None) or "unknown")


def _rate_allowed(bucket: str, identity: str, *, limit: int, window_seconds: int) -> bool:
    now = time.time()
    key = f"{bucket}:{identity}"
    with _RATE_LOCK:
        events = [stamp for stamp in _RATE_EVENTS.get(key, []) if now - stamp < window_seconds]
        if len(events) >= limit:
            _RATE_EVENTS[key] = events
            return False
        events.append(now)
        _RATE_EVENTS[key] = events
        return True


def _state_path() -> str:
    explicit = os.getenv("JASONG_MCP_AUTH_STATE_PATH", "").strip()
    if explicit:
        return explicit
    return (
        "/var/data/jasong_mcp_auth.json"
        if os.path.isdir("/var/data")
        else "/tmp/jasong_mcp_auth.json"
    )


def _audit_path() -> str:
    explicit = os.getenv("JASONG_MCP_AUDIT_PATH", "").strip()
    if explicit:
        return explicit
    return (
        "/var/data/jasong_mcp_audit.jsonl"
        if os.path.isdir("/var/data")
        else "/tmp/jasong_mcp_audit.jsonl"
    )


def _public_base_url() -> str:
    value = os.getenv(
        "JASONG_MCP_PUBLIC_BASE_URL",
        "https://jasong-ai-trader-v2.onrender.com",
    ).strip()
    return value.rstrip("/")


def _mcp_resource_url() -> str:
    return f"{_public_base_url()}/mcp"


def _oauth_metadata_url() -> str:
    return f"{_public_base_url()}/.well-known/oauth-protected-resource/mcp"


def _sha256_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _now() -> int:
    return int(time.time())


def _clean_scope(value: str | None) -> List[str]:
    parts = [item.strip() for item in str(value or "").split() if item.strip()]
    output: List[str] = []
    for item in parts:
        if item not in output:
            output.append(item)
    return output


def _json_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _redirect_with_query(url: str, **params: str) -> str:
    parsed = urlparse(url)
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _safe_json(value: Any, depth: int = 0) -> Any:
    """Return a serialisable, secret-scrubbed representation."""
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lower = key.lower()
            if any(
                marker in lower
                for marker in (
                    "password",
                    "api_key",
                    "apikey",
                    "secret",
                    "access_token",
                    "refresh_token",
                    "authorization_header",
                    "security_token",
                    "x-security-token",
                    "cst",
                )
            ):
                continue
            # Large model input arrays add no diagnostic value to ChatGPT and
            # can dominate the response payload.
            if lower in {"recent_returns", "raw_prices", "candles", "bars"}:
                continue
            clean[key] = _safe_json(raw_value, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth + 1) for item in list(value)[:250]]
    try:
        return _safe_json(dict(value), depth + 1)
    except Exception:
        return str(value)


class MCPAuthStore:
    """Small persistent OAuth/client/token store for a single private owner."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {
            "clients": {},
            "codes": {},
            "access_tokens": {},
            "refresh_tokens": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in self._data:
                    if isinstance(raw.get(key), dict):
                        self._data[key] = raw[key]
        except Exception:
            pass
        self.prune()

    def _persist(self) -> None:
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(target)
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass

    def prune(self) -> None:
        now = _now()
        with self._lock:
            for bucket in ("codes", "access_tokens", "refresh_tokens"):
                rows = self._data.setdefault(bucket, {})
                expired = [key for key, row in rows.items() if int((row or {}).get("expires_at") or 0) <= now]
                for key in expired:
                    rows.pop(key, None)
            try:
                self._persist()
            except Exception:
                pass

    @staticmethod
    def _validate_redirect_uri(uri: str) -> bool:
        parsed = urlparse(str(uri or ""))
        if parsed.scheme == "https" and parsed.netloc:
            return True
        # Localhost HTTP is allowed for MCP Inspector/testing only.
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    def register_client(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        redirect_uris = [str(item) for item in (payload.get("redirect_uris") or [])]
        if not redirect_uris or not all(self._validate_redirect_uri(uri) for uri in redirect_uris):
            raise ValueError("redirect_uris must contain valid HTTPS URLs (localhost HTTP is allowed for testing)")
        grant_types = [str(item) for item in (payload.get("grant_types") or ["authorization_code", "refresh_token"])]
        if any(item not in {"authorization_code", "refresh_token"} for item in grant_types):
            raise ValueError("unsupported grant type")
        client_id = secrets.token_urlsafe(24)
        row = {
            "client_id": client_id,
            "client_name": str(payload.get("client_name") or "ChatGPT MCP Client")[:120],
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_id_issued_at": _now(),
        }
        with self._lock:
            self._data["clients"][client_id] = row
            self._persist()
        return dict(row)

    def client(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._data.get("clients", {}).get(str(client_id))
            return dict(row) if isinstance(row, dict) else None

    def issue_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
        resource: str,
    ) -> str:
        code = secrets.token_urlsafe(36)
        with self._lock:
            self._data["codes"][_sha256_token(code)] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "scope": scope,
                "resource": resource,
                "expires_at": _now() + AUTH_CODE_TTL_SECONDS,
            }
            self._persist()
        return code

    def _issue_tokens(self, *, client_id: str, scope: str, resource: str) -> Dict[str, Any]:
        access_token = secrets.token_urlsafe(48)
        now = _now()
        scopes = _clean_scope(scope)
        response: Dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "scope": " ".join(scopes),
        }
        with self._lock:
            self._data["access_tokens"][_sha256_token(access_token)] = {
                "client_id": client_id,
                "scope": " ".join(scopes),
                "resource": resource,
                "expires_at": now + ACCESS_TOKEN_TTL_SECONDS,
            }
            # Persistent connectivity is opt-in through the standards-based
            # offline_access scope advertised in discovery metadata.
            if OFFLINE_SCOPE in scopes:
                refresh_token = secrets.token_urlsafe(64)
                self._data["refresh_tokens"][_sha256_token(refresh_token)] = {
                    "client_id": client_id,
                    "scope": " ".join(scopes),
                    "resource": resource,
                    "expires_at": now + REFRESH_TOKEN_TTL_SECONDS,
                }
                response["refresh_token"] = refresh_token
            self._persist()
        return response

    def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> Optional[Dict[str, Any]]:
        key = _sha256_token(code)
        with self._lock:
            row = self._data["codes"].pop(key, None)
            self._persist()
        if not isinstance(row, dict) or int(row.get("expires_at") or 0) <= _now():
            return None
        if row.get("client_id") != client_id or row.get("redirect_uri") != redirect_uri:
            return None
        if not code_verifier or not hmac.compare_digest(_pkce_s256(code_verifier), str(row.get("code_challenge") or "")):
            return None
        return self._issue_tokens(
            client_id=client_id,
            scope=str(row.get("scope") or READ_SCOPE),
            resource=str(row.get("resource") or _mcp_resource_url()),
        )

    def exchange_refresh(self, *, refresh_token: str, client_id: str) -> Optional[Dict[str, Any]]:
        key = _sha256_token(refresh_token)
        with self._lock:
            row = self._data["refresh_tokens"].pop(key, None)
            self._persist()
        if not isinstance(row, dict) or int(row.get("expires_at") or 0) <= _now():
            return None
        if row.get("client_id") != client_id:
            return None
        if OFFLINE_SCOPE not in _clean_scope(str(row.get("scope") or "")):
            return None
        # Refresh-token rotation: the old token is consumed and a new pair is issued.
        return self._issue_tokens(
            client_id=client_id,
            scope=str(row.get("scope") or READ_SCOPE),
            resource=str(row.get("resource") or _mcp_resource_url()),
        )

    def verify_bearer(self, token: str) -> Optional[Dict[str, Any]]:
        direct = os.getenv("JASONG_MCP_BEARER_TOKEN", "").strip()
        if direct and len(direct) >= 32 and hmac.compare_digest(token, direct):
            return {
                "client_id": "direct-bearer",
                "scope": f"{READ_SCOPE} {OFFLINE_SCOPE}",
                "resource": _mcp_resource_url(),
                "expires_at": _now() + 300,
            }
        key = _sha256_token(token)
        with self._lock:
            row = self._data.get("access_tokens", {}).get(key)
            if not isinstance(row, dict):
                return None
            if int(row.get("expires_at") or 0) <= _now():
                return None
            scopes = _clean_scope(str(row.get("scope") or ""))
            if READ_SCOPE not in scopes:
                return None
            if str(row.get("resource") or "") != _mcp_resource_url():
                return None
            return dict(row)

    def summary(self) -> Dict[str, Any]:
        self.prune()
        with self._lock:
            return {
                "clients": len(self._data.get("clients", {})),
                "active_access_tokens": len(self._data.get("access_tokens", {})),
                "active_refresh_tokens": len(self._data.get("refresh_tokens", {})),
                "state_path": self.path,
            }


class ProtectedMCPApp:
    """ASGI bearer-auth wrapper for the mounted MCP transport."""

    def __init__(self, app: Any, auth_store: MCPAuthStore):
        self.app = app
        self.auth_store = auth_store

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        identity = self.auth_store.verify_bearer(token) if token else None
        if not identity:
            body = json.dumps({
                "error": "invalid_token",
                "error_description": "Authentication required",
            }).encode("utf-8")
            www_auth = f'Bearer resource_metadata="{_oauth_metadata_url()}"'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"www-authenticate", www_auth.encode("utf-8")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["jasong_mcp_identity"] = identity
        await self.app(scope, receive, send)


def _audit(tool: str, **details: Any) -> None:
    try:
        target = Path(_audit_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": time.time(),
            "version": VERSION,
            "tool": tool,
            "details": _safe_json(details),
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def _compact_market(row: Dict[str, Any]) -> Dict[str, Any]:
    return _safe_json({
        "key": row.get("key") or row.get("symbol"),
        "market": row.get("market") or row.get("name"),
        "category": row.get("category"),
        "asset_class": row.get("asset_class"),
        "strategy": row.get("strategy_name"),
        "regime": row.get("regime"),
        "direction": row.get("direction"),
        "quant_confidence_pct": row.get("quant_confidence_pct"),
        "model_ai_directional_confidence_pct": row.get("model_ai_directional_confidence_pct"),
        "fast_score": row.get("smart_fast_score"),
        "holdout_win_rate_pct": row.get("historical_win_rate_pct"),
        "profit_factor": row.get("historical_profit_factor"),
        "holdout_trades": row.get("historical_trades"),
        "sample_pass": row.get("historical_sample_pass"),
        "walk_forward_pass": row.get("walk_forward_pass"),
        "walk_forward_min_win_rate_pct": row.get("walk_forward_min_win_rate_pct"),
        "walk_forward_median_win_rate_pct": row.get("walk_forward_median_win_rate_pct"),
        "walk_forward_profitable_folds": row.get("walk_forward_profitable_folds"),
        "selection_stable": row.get("optimizer_selection_stable"),
        "quality_tier": row.get("quality_tier"),
        "validation_status": row.get("category_validation_status"),
        "state": "PRIME" if row.get("compound_eligible") else (
            "STRONG" if (
                float(row.get("smart_fast_score") or 0) >= 45
                and float(row.get("quant_confidence_pct") or 0) >= 28
                and float(row.get("model_ai_directional_confidence_pct") or 0) >= 40
            ) else "WATCH"
        ),
        "standard_eligible": row.get("standard_eligible"),
        "compound_eligible": row.get("compound_eligible"),
        "ig_tradeable": row.get("ig_tradeable"),
        "ig_market_status": row.get("ig_market_status"),
        "ig_epic": row.get("ig_epic"),
        "ig_spread_bps": row.get("ig_spread_bps"),
        "spread_pass": row.get("spread_pass"),
        "rejection_reasons": row.get("rejection_reasons") or [],
        "evaluated_at": row.get("evaluated_at"),
    })


def _call_first(obj: Any, names: Iterable[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                return fn(*args, **kwargs)
            except TypeError:
                try:
                    return fn()
                except Exception:
                    continue
            except Exception:
                continue
    return None


def install_chatgpt_mcp(
    app: Any,
    *,
    intelligence: Any,
    portfolio: Any,
    compound_engine: Any,
    broker: Any,
    evidence_source: Any = None,
) -> Dict[str, Any]:
    """Install the authenticated, read-only remote MCP bridge on FastAPI."""
    if getattr(app.state, "jasong_mcp_installed", False):
        return dict(getattr(app.state, "jasong_mcp_status", {}) or {})

    enabled = os.getenv("JASONG_MCP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        status = {
            "version": VERSION,
            "enabled": False,
            "installed": False,
            "reason": "JASONG_MCP_ENABLED is false",
            "live_money_execution": False,
        }
        app.state.jasong_mcp_status = status
        app.add_api_route("/chatgpt-mcp/status", lambda: status, methods=["GET"], name="jasong_mcp_status_disabled")
        return status

    if MCPServer is None or ToolAnnotations is None or TransportSecuritySettings is None:
        raise RuntimeError("mcp>=2.0.0 is not installed")

    auth_store = MCPAuthStore(_state_path())
    annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
    mcp = MCPServer("Jasong AI Trader V6.9.3 Read-Only")

    def _ranked_rows() -> List[Dict[str, Any]]:
        rankings = intelligence.category_rankings()
        return [
            dict(row)
            for category in ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES")
            for row in rankings.get(category, [])
        ]

    def _market_key(value: Any) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    def _compound_evaluation(symbol: str) -> Optional[Dict[str, Any]]:
        wanted = _market_key(symbol)
        status = _call_first(compound_engine, ("status",)) or {}
        if not isinstance(status, dict):
            return None
        candidates: List[Dict[str, Any]] = []
        for field in (
            "last_candidate_ranking",
            "pending_elite_candidates",
            "last_ranking",
            "candidates",
        ):
            rows = status.get(field) or []
            if isinstance(rows, list):
                candidates.extend(dict(row) for row in rows if isinstance(row, dict))
        for row in candidates:
            keys = {
                _market_key(row.get("key")),
                _market_key(row.get("symbol")),
                _market_key(row.get("market")),
                _market_key(row.get("ig_market_name")),
            }
            if wanted and wanted in keys:
                return row
        return None

    @mcp.tool(
        title="Get Jasong system status",
        description="Use this when you need the current Jasong AI Trader health, qualification policy, evidence coverage, portfolio capacity, compound state, or broker connectivity. Read-only.",
        annotations=annotations,
    )
    def get_system_status() -> Dict[str, Any]:
        _audit("get_system_status")
        broker_configured = bool(getattr(broker, "configured", lambda: False)())
        return _safe_json({
            "version": VERSION,
            "execution_mode": "IG_DEMO_ONLY",
            "live_money_execution": False,
            "category_system": intelligence.status(),
            "portfolio": portfolio.status(),
            "compound": _call_first(compound_engine, ("status",)),
            "forward_evidence": _call_first(evidence_source, ("execution_guard_snapshot", "status")) if evidence_source is not None else None,
            "broker": _call_first(broker, ("status",)) or {"configured": broker_configured, "environment": "DEMO"},
            "mcp_auth": auth_store.summary(),
        })

    @mcp.tool(
        title="Get market opportunities",
        description="Use this when you need the current top market opportunities, optionally filtered by category or PRIME/STRONG/WATCH state. Read-only.",
        annotations=annotations,
    )
    def get_market_opportunities(category: str = "", state: str = "", limit: int = 20) -> Dict[str, Any]:
        _audit("get_market_opportunities", category=category, state=state, limit=limit)
        wanted_category = category.upper().strip()
        wanted_state = state.upper().strip()
        rows = [_compact_market(row) for row in _ranked_rows()]
        if wanted_category:
            rows = [row for row in rows if str(row.get("category") or "").upper() == wanted_category]
        if wanted_state:
            rows = [row for row in rows if str(row.get("state") or "").upper() == wanted_state]
        limit = max(1, min(int(limit), 100))
        return {"version": VERSION, "count": len(rows[:limit]), "opportunities": rows[:limit]}

    @mcp.tool(
        title="Get market details",
        description="Use this when you need the current qualification evidence and broker blockers for one ranked market symbol, such as META or EURUSD. Read-only.",
        annotations=annotations,
    )
    def get_market_details(symbol: str) -> Dict[str, Any]:
        wanted = symbol.upper().replace("/", "").replace(" ", "").strip()
        _audit("get_market_details", symbol=wanted)
        for row in _ranked_rows():
            variants = {
                str(row.get("key") or "").upper().replace("/", "").replace(" ", ""),
                str(row.get("symbol") or "").upper().replace("/", "").replace(" ", ""),
                str(row.get("market") or "").upper().replace("/", "").replace(" ", ""),
                str(row.get("name") or "").upper().replace("/", "").replace(" ", ""),
            }
            if wanted in variants:
                return _safe_json(row)
        return {"version": VERSION, "found": False, "symbol": wanted, "message": "Market is not in the current top-five-per-category ranking surface."}

    @mcp.tool(
        title="Explain execution blockers",
        description="Use this when you need the exact current reasons a ranked market is not PRIME or not eligible for IG DEMO execution. Read-only.",
        annotations=annotations,
    )
    def get_execution_blockers(symbol: str) -> Dict[str, Any]:
        details = get_market_details(symbol)
        _audit("get_execution_blockers", symbol=symbol)
        if details.get("found") is False:
            return details
        compound_eval = _compound_evaluation(symbol)
        return _safe_json({
            "version": VERSION,
            "market": details.get("market") or details.get("name"),
            "symbol": details.get("symbol") or details.get("key"),
            "direction": details.get("direction"),
            "standard_eligible": details.get("standard_eligible"),
            "category_compound_eligible": details.get("compound_eligible"),
            "category_rejection_reasons": details.get("rejection_reasons") or [],
            "compound_engine_evaluation": compound_eval,
            "compound_engine_rejection_reasons": (compound_eval or {}).get("rejection_reasons") or [],
            "policy": {
                "quant_min_pct": 28.0,
                "ai_min_pct": 40.0,
                "fast_min": 45.0,
                "holdout_wr_min_pct": 60.0,
                "profit_factor_min": 1.20,
                "holdout_trades_min": 10,
                "walk_forward_fold_wr_min_pct": 40.0,
                "walk_forward_median_wr_min_pct": 40.0,
                "walk_forward_profitable_folds_min": 2,
            },
            "current": {
                "quant_pct": details.get("quant_confidence_pct"),
                "ai_pct": details.get("model_ai_directional_confidence_pct"),
                "fast": details.get("smart_fast_score"),
                "holdout_wr_pct": details.get("historical_win_rate_pct"),
                "profit_factor": details.get("historical_profit_factor"),
                "holdout_trades": details.get("historical_trades"),
                "wf_min_pct": details.get("walk_forward_min_win_rate_pct"),
                "wf_median_pct": details.get("walk_forward_median_win_rate_pct"),
                "wf_profitable_folds": details.get("walk_forward_profitable_folds"),
                "selection_stable": details.get("optimizer_selection_stable"),
                "ig_tradeable": details.get("ig_tradeable"),
                "spread_bps": details.get("ig_spread_bps"),
                "spread_pass": details.get("spread_pass"),
            },
        })

    @mcp.tool(
        title="Get PRIME markets",
        description="Use this when you need the current markets that have passed every category gate and are eligible as Compound PRIME candidates. Read-only.",
        annotations=annotations,
    )
    def get_prime_markets() -> Dict[str, Any]:
        _audit("get_prime_markets")
        rows = [_compact_market(row) for row in intelligence.compound_candidates()]
        return {"version": VERSION, "count": len(rows), "prime_markets": rows, "live_money_execution": False}

    @mcp.tool(
        title="Get validation status",
        description="Use this when you need optimizer, final-holdout, sample-size, profit-factor, or walk-forward validation evidence across the six categories. Read-only.",
        annotations=annotations,
    )
    def get_validation_status() -> Dict[str, Any]:
        _audit("get_validation_status")
        return _safe_json(intelligence.optimizer_summary())

    @mcp.tool(
        title="Get evidence health",
        description="Use this when you need to confirm all 40 markets are optimized, the active evidence schema, pending refresh work, and excluded legacy rows. Read-only.",
        annotations=annotations,
    )
    def get_evidence_health() -> Dict[str, Any]:
        _audit("get_evidence_health")
        return _safe_json(intelligence.evidence_coverage())

    @mcp.tool(
        title="Get category portfolio",
        description="Use this when you need current category IG DEMO positions, capacity, per-category exposure, opens/closes, or portfolio errors. Read-only.",
        annotations=annotations,
    )
    def get_category_portfolio(limit: int = 50) -> Dict[str, Any]:
        _audit("get_category_portfolio", limit=limit)
        limit = max(1, min(int(limit), 200))
        return _safe_json({"status": portfolio.status(), "positions": portfolio.positions(limit=limit)})

    @mcp.tool(
        title="Get Compound status",
        description="Use this when you need the current adaptive 80/20 Compound cycle, capital, reserve, target optimizer, basket, candidates, performance, or execution status. Read-only.",
        annotations=annotations,
    )
    def get_compound_status() -> Dict[str, Any]:
        _audit("get_compound_status")
        return _safe_json(_call_first(compound_engine, ("status",)) or {"status": "unavailable"})

    @mcp.tool(
        title="Get IG DEMO broker status",
        description="Use this when you need broker connectivity plus safe IG DEMO account balances and open positions. Broker credentials and session tokens are never returned. Read-only.",
        annotations=annotations,
    )
    def get_ig_demo_status() -> Dict[str, Any]:
        _audit("get_ig_demo_status")
        configured = bool(getattr(broker, "configured", lambda: False)())
        broker_status = _call_first(broker, ("status",))
        accounts = _call_first(broker, ("accounts",)) if configured else None
        positions = _call_first(broker, ("positions", "open_positions", "list_positions")) if configured else None
        return _safe_json({
            "version": VERSION,
            "configured": configured,
            "environment": "IG_DEMO_ONLY",
            "broker_status": broker_status,
            "accounts": accounts,
            "positions": positions,
            "live_money_execution": False,
        })

    @mcp.tool(
        title="Get forward IG DEMO evidence",
        description="Use this when you need broker-settled forward evidence, learning-phase state, recent broker win rate, reconciliation state, or execution guard information. Read-only.",
        annotations=annotations,
    )
    def get_forward_evidence() -> Dict[str, Any]:
        _audit("get_forward_evidence")
        if evidence_source is None:
            return {"version": VERSION, "available": False, "message": "Forward evidence source is not installed."}
        return _safe_json({
            "version": VERSION,
            "status": _call_first(evidence_source, ("status",)),
            "execution_guard": _call_first(evidence_source, ("execution_guard_snapshot",)),
            "live_money_execution": False,
        })

    @mcp.tool(
        title="Get broker-settled trade history",
        description="Use this when you need the current or a specific IG DEMO evidence phase, including accepted trades, wins, losses, broker P&L, confidence profiles, and diagnostic flags. Read-only.",
        annotations=annotations,
    )
    def get_trade_history(phase_id: int = 0) -> Dict[str, Any]:
        _audit("get_trade_history", phase_id=phase_id)
        if evidence_source is None or not hasattr(evidence_source, "phase_trade_analysis"):
            return {"version": VERSION, "available": False, "message": "Broker-settled phase history is not installed."}
        try:
            result = evidence_source.phase_trade_analysis(int(phase_id) if int(phase_id) > 0 else None)
        except Exception as exc:
            return {"version": VERSION, "available": False, "error": f"{type(exc).__name__}: {exc}"}
        return _safe_json(result)

    @mcp.tool(
        title="Get Jasong runtime diagnostics",
        description="Use this when you need the latest internal error and health surfaces across category intelligence, category execution, Compound, broker, forward evidence, and MCP installation. Read-only.",
        annotations=annotations,
    )
    def get_diagnostics() -> Dict[str, Any]:
        _audit("get_diagnostics")
        return _safe_json({
            "version": VERSION,
            "category": intelligence.status(),
            "portfolio": portfolio.status(),
            "compound": _call_first(compound_engine, ("status",)),
            "broker": _call_first(broker, ("status",)),
            "forward_evidence": _call_first(evidence_source, ("status",)) if evidence_source is not None else None,
            "mcp_install_error": getattr(app.state, "jasong_mcp_install_error", None),
            "live_money_execution": False,
        })

    @mcp.tool(
        title="Get MCP audit log",
        description="Use this when you need recent read-only ChatGPT MCP tool-call audit events for this backend. Read-only.",
        annotations=annotations,
    )
    def get_mcp_audit(limit: int = 50) -> Dict[str, Any]:
        _audit("get_mcp_audit", limit=limit)
        limit = max(1, min(int(limit), 200))
        target = Path(_audit_path())
        try:
            lines = target.read_text(encoding="utf-8").splitlines()[-limit:]
            events = []
            for line in lines:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        events.append(_safe_json(row))
                except Exception:
                    continue
        except Exception:
            events = []
        return {"version": VERSION, "count": len(events), "events": events}

    @mcp.tool(
        title="Search Jasong live state",
        description="Use this when you want to search the current ranked market state by symbol, market name, category, strategy, regime, state, or rejection reason. Read-only.",
        annotations=annotations,
    )
    def search(query: str, limit: int = 10) -> Dict[str, Any]:
        needle = str(query or "").lower().strip()
        _audit("search", query=needle, limit=limit)
        rows = [_compact_market(row) for row in _ranked_rows()]
        if needle:
            rows = [row for row in rows if needle in json.dumps(row, sort_keys=True).lower()]
        limit = max(1, min(int(limit), 50))
        return {
            "version": VERSION,
            "results": [
                {
                    "id": f"market:{row.get('key')}",
                    "title": f"{row.get('market')} — {row.get('state')}",
                    "text": json.dumps(row, separators=(",", ":")),
                }
                for row in rows[:limit]
            ],
        }

    @mcp.tool(
        title="Fetch Jasong live resource",
        description="Use this after search, or directly with ids system, optimizer, evidence, portfolio, compound, broker, or market:SYMBOL, to fetch current live data. Read-only.",
        annotations=annotations,
    )
    def fetch(id: str) -> Dict[str, Any]:
        resource_id = str(id or "").strip()
        _audit("fetch", id=resource_id)
        lower = resource_id.lower()
        if lower == "system":
            return get_system_status()
        if lower == "optimizer":
            return get_validation_status()
        if lower == "evidence":
            return get_evidence_health()
        if lower == "portfolio":
            return get_category_portfolio()
        if lower == "compound":
            return get_compound_status()
        if lower == "broker":
            return get_ig_demo_status()
        if lower == "forward":
            return get_forward_evidence()
        if lower == "trades":
            return get_trade_history()
        if lower == "diagnostics":
            return get_diagnostics()
        if lower == "audit":
            return get_mcp_audit()
        if lower.startswith("market:"):
            return get_market_details(resource_id.split(":", 1)[1])
        return {"version": VERSION, "found": False, "id": resource_id, "message": "Unknown resource id."}

    # Create transport before accessing session_manager.
    public = urlparse(_public_base_url())
    allowed_host = public.netloc
    allowed_hosts = [allowed_host]
    if allowed_host and ":" not in allowed_host:
        allowed_hosts.append(f"{allowed_host}:*")
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[_public_base_url()],
    )
    mcp_asgi = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        streamable_http_path="/",
        transport_security=transport_security,
    )
    protected_mcp = ProtectedMCPApp(mcp_asgi, auth_store)
    app.mount("/mcp", protected_mcp, name="jasong_chatgpt_mcp")

    # Starlette 1.6 removed add_event_handler()/on_event().
    # Compose the MCP session-manager lifecycle into the existing FastAPI
    # router lifespan instead. This preserves any lifespan already installed
    # by the application and keeps MCP failure isolated from IG DEMO trading.
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def jasong_mcp_lifespan(app_instance: Any):
        async with previous_lifespan(app_instance) as previous_state:
            cm = None
            try:
                cm = mcp.session_manager.run()
                await cm.__aenter__()
                app.state.jasong_mcp_lifespan_cm = cm
                app.state.jasong_mcp_start_error = None
            except Exception as exc:
                # The assistant bridge is optional. A transport startup problem
                # must never take the IG DEMO trading API offline.
                app.state.jasong_mcp_lifespan_cm = None
                app.state.jasong_mcp_start_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                cm = None

            try:
                yield previous_state
            finally:
                if cm is not None:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception as exc:
                        app.state.jasong_mcp_start_error = (
                            f"shutdown {type(exc).__name__}: {exc}"
                        )
                    finally:
                        app.state.jasong_mcp_lifespan_cm = None

    app.router.lifespan_context = jasong_mcp_lifespan

    async def oauth_server_metadata() -> JSONResponse:
        base = _public_base_url()
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "scopes_supported": [READ_SCOPE, OFFLINE_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        }, headers={"Cache-Control": "no-store"})

    async def protected_resource_metadata() -> JSONResponse:
        base = _public_base_url()
        return JSONResponse({
            "resource": _mcp_resource_url(),
            "resource_name": "Jasong AI Trader V6.9.3 Read-Only",
            "authorization_servers": [base],
            "scopes_supported": [READ_SCOPE],
            "bearer_methods_supported": ["header"],
        }, headers={"Cache-Control": "no-store"})

    async def register_client(request: Request) -> JSONResponse:
        if not _rate_allowed("oauth-register", _request_ip(request), limit=30, window_seconds=3600):
            return _json_error("temporarily_unavailable", "Too many client registration attempts", 429)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid registration payload")
            client = auth_store.register_client(payload)
            return JSONResponse(client, status_code=201, headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return _json_error("invalid_client_metadata", str(exc))
        except Exception:
            return _json_error("invalid_client_metadata", "Invalid registration payload")

    def _validate_authorization_request(params: Dict[str, str]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        client = auth_store.client(params.get("client_id", ""))
        if not client:
            return None, "Unknown client_id"
        if params.get("response_type") != "code":
            return None, "response_type must be code"
        if params.get("redirect_uri") not in client.get("redirect_uris", []):
            return None, "redirect_uri is not registered"
        if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
            return None, "PKCE S256 is required"
        scopes = _clean_scope(params.get("scope"))
        if READ_SCOPE not in scopes:
            return None, f"scope must include {READ_SCOPE}"
        unsupported_scopes = [scope for scope in scopes if scope not in {READ_SCOPE, OFFLINE_SCOPE}]
        if unsupported_scopes:
            return None, f"unsupported scope: {unsupported_scopes[0]}"
        resource = params.get("resource") or _mcp_resource_url()
        if resource.rstrip("/") != _mcp_resource_url().rstrip("/"):
            return None, "resource does not match the Jasong MCP endpoint"
        return client, None

    async def authorize_get(request: Request) -> Any:
        params = {key: value for key, value in request.query_params.items()}
        client, error = _validate_authorization_request(params)
        if error:
            return _json_error("invalid_request", error)
        client_name = html.escape(str((client or {}).get("client_name") or "ChatGPT MCP Client"))
        redirect_host = html.escape(urlparse(params.get("redirect_uri", "")).netloc)
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value, quote=True)}">'
            for key, value in params.items()
        )
        page = f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize Jasong AI Trader</title>
<style>body{{font-family:system-ui;background:#06131b;color:#eef;padding:32px;max-width:520px;margin:auto}}
.card{{background:#0e1a24;border:1px solid #17303a;border-radius:18px;padding:24px}}input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #24404b;background:#08151d;color:white;margin:10px 0 16px}}button{{width:100%;padding:12px;border:0;border-radius:10px;background:#65e6d3;color:#041014;font-weight:800}}small{{color:#9aa}}</style></head>
<body><div class="card"><h2>Jasong AI Trader</h2><p><b>{client_name}</b> is requesting read-only access to live IG DEMO diagnostics, signals, validation evidence, positions and performance.</p><p><small>Return destination: {redirect_host or 'registered client'}<br>No tool exposed by this connection can open or close a trade.</small></p>
<form method="post" action="/oauth/authorize">{hidden}<label>Private MCP password</label><input type="password" name="password" autocomplete="current-password" required><button type="submit">Authorize read-only access</button></form></div></body></html>"""
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    async def authorize_post(request: Request) -> Any:
        if not _rate_allowed("oauth-authorize", _request_ip(request), limit=12, window_seconds=900):
            return _json_error("temporarily_unavailable", "Too many authorization attempts; try again later", 429)
        form = await request.form()
        params = {key: str(value) for key, value in form.items() if key != "password"}
        _client, error = _validate_authorization_request(params)
        if error:
            return _json_error("invalid_request", error)
        configured_password = os.getenv("JASONG_MCP_ADMIN_PASSWORD", "")
        supplied_password = str(form.get("password") or "")
        if len(configured_password) < 12:
            return _json_error("server_error", "JASONG_MCP_ADMIN_PASSWORD is not securely configured", 503)
        if not hmac.compare_digest(configured_password, supplied_password):
            return HTMLResponse("<h3>Authorization denied</h3><p>Incorrect password.</p>", status_code=403, headers={"Cache-Control": "no-store"})
        scope = " ".join(_clean_scope(params.get("scope")))
        code = auth_store.issue_code(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            scope=scope,
            resource=params.get("resource") or _mcp_resource_url(),
        )
        redirect = _redirect_with_query(params["redirect_uri"], code=code, state=params.get("state", ""), iss=_public_base_url())
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    async def token_endpoint(request: Request) -> JSONResponse:
        form = await request.form()
        grant_type = str(form.get("grant_type") or "")
        client_id = str(form.get("client_id") or "")
        if not auth_store.client(client_id):
            return _json_error("invalid_client", "Unknown client_id", 401)
        if grant_type == "authorization_code":
            tokens = auth_store.exchange_code(
                code=str(form.get("code") or ""),
                client_id=client_id,
                redirect_uri=str(form.get("redirect_uri") or ""),
                code_verifier=str(form.get("code_verifier") or ""),
            )
            if not tokens:
                return _json_error("invalid_grant", "Invalid, expired, or already-used authorization code")
            return JSONResponse(tokens, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if grant_type == "refresh_token":
            tokens = auth_store.exchange_refresh(
                refresh_token=str(form.get("refresh_token") or ""),
                client_id=client_id,
            )
            if not tokens:
                return _json_error("invalid_grant", "Invalid or expired refresh token")
            return JSONResponse(tokens, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        return _json_error("unsupported_grant_type", "Use authorization_code or refresh_token")

    status = {
        "version": VERSION,
        "enabled": True,
        "installed": True,
        "endpoint": "/mcp",
        "public_endpoint": _mcp_resource_url(),
        "authentication": "OAuth authorization-code + PKCE + refresh token; optional static bearer for API testing",
        "read_only": True,
        "trade_write_tools_exposed": False,
        "required_scope": READ_SCOPE,
        "oauth_password_configured": len(os.getenv("JASONG_MCP_ADMIN_PASSWORD", "")) >= 12,
        "direct_bearer_configured": len(os.getenv("JASONG_MCP_BEARER_TOKEN", "")) >= 32,
        "tools": [
            "get_system_status",
            "get_market_opportunities",
            "get_market_details",
            "get_execution_blockers",
            "get_prime_markets",
            "get_validation_status",
            "get_evidence_health",
            "get_category_portfolio",
            "get_compound_status",
            "get_ig_demo_status",
            "get_forward_evidence",
            "get_trade_history",
            "get_diagnostics",
            "get_mcp_audit",
            "search",
            "fetch",
        ],
        "live_money_execution": False,
    }
    app.state.jasong_mcp_status = status
    app.state.jasong_mcp_installed = True
    app.state.jasong_mcp_server = mcp

    app.add_api_route("/.well-known/oauth-authorization-server", oauth_server_metadata, methods=["GET"], name="jasong_mcp_oauth_metadata")
    app.add_api_route("/.well-known/openid-configuration", oauth_server_metadata, methods=["GET"], name="jasong_mcp_oidc_compat_metadata")
    app.add_api_route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"], name="jasong_mcp_resource_metadata_root")
    app.add_api_route("/.well-known/oauth-protected-resource/mcp", protected_resource_metadata, methods=["GET"], name="jasong_mcp_resource_metadata")
    app.add_api_route("/oauth/register", register_client, methods=["POST"], name="jasong_mcp_oauth_register")
    app.add_api_route("/oauth/authorize", authorize_get, methods=["GET"], name="jasong_mcp_oauth_authorize_get")
    app.add_api_route("/oauth/authorize", authorize_post, methods=["POST"], name="jasong_mcp_oauth_authorize_post")
    app.add_api_route("/oauth/token", token_endpoint, methods=["POST"], name="jasong_mcp_oauth_token")
    def mcp_status() -> Dict[str, Any]:
        current = dict(status)
        current["startup_error"] = getattr(app.state, "jasong_mcp_start_error", None)
        current["runtime_ready"] = bool(
            getattr(app.state, "jasong_mcp_lifespan_cm", None) is not None
            and current["startup_error"] is None
        )
        return current

    app.add_api_route("/chatgpt-mcp/status", mcp_status, methods=["GET"], name="jasong_mcp_status")

    return dict(status)
