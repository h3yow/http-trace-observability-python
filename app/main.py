"""FastAPI 应用装配：中间件、路由、异常处理与对外指标接口。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .context import current_request_id
from .errors import AppError, ErrorCategory, normalize
from .handlers import router
from .middleware import TraceMiddleware
from .observability import Observability


def _error_payload(category: str, message: str) -> dict[str, object]:
    return {
        "error": {
            "category": category,
            "message": message,
            "request_id": current_request_id(),
        }
    }


def _id_headers() -> dict[str, str]:
    return {"X-Request-ID": current_request_id()}


def create_app(obs: Observability | None = None) -> FastAPI:
    """构造可观测 HTTP 应用。"""
    app = FastAPI(title="http-trace-observability", docs_url=None, redoc_url=None)
    observability = obs if obs is not None else Observability()
    app.state.obs = observability

    app.add_middleware(TraceMiddleware, obs=observability)
    app.include_router(router)

    # ---- 统一错误响应：只透出类别 + 固定文案 + 链路标识 ----
    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        normalized = normalize(exc)
        return JSONResponse(
            status_code=normalized.status_code,
            content=_error_payload(normalized.category.value, normalized.public_message),
            headers=_id_headers(),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        # 不回显用户输入或内部定位细节，只给稳定的归一化结论
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                ErrorCategory.VALIDATION.value, "request validation failed"
            ),
            headers=_id_headers(),
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(_request: Request, exc: Exception) -> JSONResponse:
        normalized = normalize(exc)  # 内部异常统一 500，细节仅进日志
        observability.event(
            "error.normalized",
            category=normalized.category.value,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=normalized.status_code,
            content=_error_payload(normalized.category.value, normalized.public_message),
            headers=_id_headers(),
        )

    # ---- 对外可观测读取接口 ----
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "request_id": current_request_id()}

    @app.get("/metrics")
    async def metrics() -> dict[str, object]:
        """读取聚合指标：有界序列、上界声明与退化计数。"""
        return observability.stats()

    return app


app = create_app()
