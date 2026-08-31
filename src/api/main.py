# src/api/main.py
import os
import logging
import hashlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

# Initialize ConfigManager before importing routes
from ..config_manager import init_config_manager
config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
nodes_path = os.environ.get("NODES_PATH", "config/nodes.yaml")
init_config_manager(config_path, nodes_path)

# ── Auth & Security ──────────────────────────────────────────────────────────
from .auth import (
    AuthMiddleware,
    get_cors_origins,
    API_KEY_ADMIN,
    API_KEY_VIEWER,
    ALLOWED_ORIGINS,
)

# ── Rate Limiting ────────────────────────────────────────────────────────────
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=["120/minute"],
        storage_uri="memory://",
    )
    SLOWAPI_AVAILABLE = True
except ImportError:
    limiter = None
    SLOWAPI_AVAILABLE = False
    logger.warning("slowapi not installed — rate limiting disabled. Install: pip install slowapi")


# ── Security Headers Middleware ──────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # HSTS — only if HTTPS is configured
        if os.environ.get("ENABLE_HSTS", "").lower() == "true":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Monitoreo IA - Vidanet",
    description="Sistema de monitoreo inteligente para red FTTH",
    version="1.1.0",
)

# Rate limiter (if available)
if SLOWAPI_AVAILABLE:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware order matters: outermost = first applied to request
# 1. Security headers (outermost — always applied)
app.add_middleware(SecurityHeadersMiddleware)
# 2. Auth check (before routing)
app.add_middleware(AuthMiddleware)
# 3. No-cache for API responses
app.add_middleware(NoCacheMiddleware)

# CORS
cors_origins = get_cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["X-API-Key", "Content-Type", "Authorization"],
    )
    logger.info(f"CORS enabled for origins: {cors_origins}")
else:
    logger.info("CORS disabled — same-origin requests only")

# Paths
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        content = index_file.read_text(encoding="utf-8")
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        content = content.replace(
            'tailwindcss">',
            f'tailwindcss?v={content_hash}">'
        ).replace(
            'vue@3/dist/vue.global.prod.js">',
            f'vue@3/dist/vue.global.prod.js?v={content_hash}">'
        ).replace(
            'apexcharts">',
            f'apexcharts?v={content_hash}">'
        )
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Content-Type-Options": "nosniff",
            }
        )
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)


@app.api_route("/backbone", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def backbone_dashboard():
    backbone_file = STATIC_DIR / "backbone.html"
    if backbone_file.exists():
        content = backbone_file.read_text(encoding="utf-8")
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return HTMLResponse(content="<h1>Backbone Dashboard not found</h1>", status_code=404)


@app.api_route("/olts", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def olts_dashboard():
    olts_file = STATIC_DIR / "olts.html"
    if olts_file.exists():
        content = olts_file.read_text(encoding="utf-8")
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return HTMLResponse(content="<h1>OLTs Dashboard not found</h1>", status_code=404)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "monitoreo-ia",
        "version": "1.1.0"
    }


# ── Import & register routes ─────────────────────────────────────────────────
from .routes import metrics, anomalies, health as health_route, nodes, devices, agent, ping, reporte, agent_llm, tv

app.include_router(metrics.router, prefix="/api/v1/metrics", tags=["metrics"])
app.include_router(anomalies.router, prefix="/api/v1/anomalies", tags=["anomalies"])
app.include_router(health_route.router, prefix="/api/v1/health", tags=["health"])
app.include_router(nodes.router, tags=["nodes"])
app.include_router(devices.router, tags=["devices"])
app.include_router(agent.router, tags=["agent"])
app.include_router(ping.router, tags=["ping"])
app.include_router(reporte.router, tags=["reporte"])
app.include_router(agent_llm.router, tags=["agent-llm"])
app.include_router(tv.router, tags=["tv"])

# ── Startup log ──────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _log_security_status():
    auth_status = "ENABLED" if API_KEY_ADMIN else "DISABLED (no API_KEY set)"
    viewer_status = "configured" if API_KEY_VIEWER else "not configured"
    rate_status = "ENABLED" if SLOWAPI_AVAILABLE else "DISABLED (slowapi not installed)"
    cors_status = f"origins={ALLOWED_ORIGINS}" if ALLOWED_ORIGINS else "disabled (same-origin only)"

    logger.info("=== Security Status ===")
    logger.info(f"  Authentication: {auth_status}")
    logger.info(f"  Viewer key: {viewer_status}")
    logger.info(f"  Rate limiting: {rate_status}")
    logger.info(f"  CORS: {cors_status}")
    logger.info(f"  Security headers: ENABLED")
    if not API_KEY_ADMIN:
        logger.warning(
            "  ⚠ No API_KEY configured — all endpoints are OPEN. "
            "Set API_KEY env var to enable authentication."
        )
    logger.info("======================")
