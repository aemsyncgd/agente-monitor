# src/api/auth.py
"""
Authentication and authorization for the monitoring API.

Uses API key-based auth via X-API-Key header (or ?api_key= query param).
Roles: admin (full access), operator (read + limited write), viewer (read-only).

Environment variables:
    API_KEY          - Admin API key (required for write operations)
    API_KEY_OPERATOR - Operator API key (optional, read + config write)
    API_KEY_VIEWER   - Viewer API key (optional, read-only)
    ALLOWED_ORIGINS  - Comma-separated CORS origins (default: same-origin)
"""
import os
import time
import logging
from typing import Optional, Set

from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

API_KEY_ADMIN = os.environ.get("API_KEY", "")
API_KEY_VIEWER = os.environ.get("API_KEY_VIEWER", "")
API_KEY_OPERATOR = os.environ.get("API_KEY_OPERATOR", "")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

# Paths that NEVER require authentication
PUBLIC_PATHS: Set[str] = {
    "/health",
    "/api/v1/health/",
    "/api/v1/health/collectors",
    "/api/v1/health/ai",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# Paths that require admin role (destructive write operations)
ADMIN_PATHS: Set[str] = {
    "/api/nodes/",           # POST create node
    "/api/agent/instructions",  # POST create instruction
}

# Paths that are deprecated (Zabbix) — still accessible but logged
DEPRECATED_PREFIXES: tuple = ("/api/zabbix",)


# ── Role definitions ─────────────────────────────────────────────────────────

class Role:
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    NONE = "none"


def get_role_for_key(api_key: str) -> str:
    """Resolve an API key to a role. Returns Role.NONE if invalid."""
    if not api_key:
        return Role.NONE
    if API_KEY_ADMIN and api_key == API_KEY_ADMIN:
        return Role.ADMIN
    if API_KEY_OPERATOR and api_key == API_KEY_OPERATOR:
        return Role.OPERATOR
    if API_KEY_VIEWER and api_key == API_KEY_VIEWER:
        return Role.VIEWER
    return Role.NONE


# ── Auth Middleware ───────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """
    Checks X-API-Key header (or ?api_key= query param) on every /api/* request.
    Public paths (/health, /docs, etc.) are exempt.
    Sets request.state.role for downstream use.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Exempt public paths
        if path in PUBLIC_PATHS or path.startswith("/static") or path == "/":
            response = await call_next(request)
            return response

        # Only enforce auth on /api/* paths
        if not path.startswith("/api/"):
            response = await call_next(request)
            return response

        # Extract API key from header or query param
        api_key = request.headers.get("X-API-Key", "")
        if not api_key:
            api_key = request.query_params.get("api_key", "")

        # Resolve role
        role = get_role_for_key(api_key)

        # If no auth configured, allow all (backward compat during migration)
        if not API_KEY_ADMIN and not API_KEY_VIEWER:
            request.state.role = Role.ADMIN
            logger.warning(
                "No API_KEY configured — all requests allowed. "
                "Set API_KEY environment variable to enable authentication."
            )
            response = await call_next(request)
            return response

        # If auth is configured but no key provided
        if role == Role.NONE:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Unauthorized",
                    "detail": "Valid X-API-Key header required. "
                              "Obtain an API key from the administrator.",
                },
            )

        # Check write permissions for non-GET methods
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if role == Role.VIEWER:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Forbidden",
                        "detail": "Viewer role cannot perform write operations.",
                    },
                )

        # Store role in request state for route-level use
        request.state.role = role
        request.state.api_key_preview = api_key[:8] + "..." if len(api_key) > 8 else "***"

        response = await call_next(request)
        return response


# ── Rate Limiting ────────────────────────────────────────────────────────────

# Rate limit categories with their limits (requests per window)
RATE_LIMITS = {
    "default": "120/minute",
    "chat": "10/minute",           # LLM chat is expensive
    "test_device": "5/minute",     # Runs ping/SNMP — abuse risk
    "generate_report": "3/minute", # Sends to Telegram
    "write_config": "20/minute",   # Config mutations
}


def get_rate_limit_key(request: Request) -> str:
    """Return the rate limit category for a given request."""
    path = request.url.path

    if path == "/api/agent/llm/chat":
        return "chat"
    if path == "/api/devices/test":
        return "test_device"
    if path == "/api/reporte/generate":
        return "generate_report"
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        return "write_config"
    return "default"


# ── CORS ─────────────────────────────────────────────────────────────────────

def get_cors_origins() -> list:
    """Return configured CORS origins. Empty list = same-origin only."""
    if ALLOWED_ORIGINS:
        return ALLOWED_ORIGINS
    # Default: no cross-origin requests allowed
    return []


# ── Route-level dependencies ─────────────────────────────────────────────────

def require_role(min_role: str):
    """
    FastAPI dependency factory for role-based access control.

    Usage:
        @router.post("/something", dependencies=[Depends(require_role(Role.ADMIN))])
        async def create_thing(): ...

        @router.get("/something", dependencies=[Depends(require_role(Role.VIEWER))])
        async def read_thing(): ...
    """
    role_hierarchy = {
        Role.ADMIN: 3,
        Role.OPERATOR: 2,
        Role.VIEWER: 1,
        Role.NONE: 0,
    }

    async def _check(request: Request):
        # If no auth configured, allow all
        if not API_KEY_ADMIN and not API_KEY_VIEWER:
            return

        current_role = getattr(request.state, "role", Role.NONE)
        if role_hierarchy.get(current_role, 0) < role_hierarchy.get(min_role, 0):
            raise HTTPException(
                status_code=403,
                detail=f"Role '{min_role}' or higher required. Current role: '{current_role}'.",
            )

    return _check
