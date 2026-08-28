import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import MediaSyncError
from app.core.logging import suppress_sensitive_http_client_logs
from app.scheduler import start_scheduler, stop_scheduler
from app.services.update_execution_gate import build_update_execution_gate

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
suppress_sensitive_http_client_logs()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "background_execution_mode_selected process=api mode=%s",
        settings.background_execution_mode,
    )
    legacy_mode = settings.background_execution_mode == "legacy"
    if legacy_mode:
        start_scheduler()
    try:
        yield
    finally:
        if legacy_mode:
            stop_scheduler()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix=settings.api_prefix)

_CANDIDATE_SAFE_ENDPOINTS = {
    ("GET", "/api/v1/system/health"),
    ("GET", "/api/v1/system/update"),
    ("GET", "/api/v1/auth/status"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
}


@app.middleware("http")
async def candidate_validation_guard(request: Request, call_next):
    gate = build_update_execution_gate()
    if gate.pending_marker_present():
        from app.core.database import SessionLocal

        with SessionLocal() as session:
            decision = gate.evaluate(session)
        if (
            decision.mode in {"candidate_validation", "candidate_invalid"}
            and (request.method, request.url.path) not in _CANDIDATE_SAFE_ENDPOINTS
        ):
            return JSONResponse(
                status_code=423,
                content={
                    "detail": "候选版本验证中，当前接口已暂时关闭",
                    "runtime_mode": decision.mode,
                },
            )
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(MediaSyncError)
async def mediasync_error_handler(request: Request, exc: MediaSyncError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "request_id", None),
                "details": None,
            }
        },
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"name": settings.app_name, "version": settings.app_version}
