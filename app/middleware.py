"""请求中间件：串联链路标识、结构化日志、指标与错误归一化。"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.errors import classify
from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.tracing import reset_current, resolve_trace_id, set_current

TRACE_ID_HEADER = "X-Trace-Id"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """为每个请求建立链路上下文，输出结构化日志并聚合指标。

    任何异常在此被归一化为统一错误响应；日志/指标写入失败不影响响应。
    """

    def __init__(self, app, logger: StructuredLogger, metrics: MetricsRegistry) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._logger = logger
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        ctx = resolve_trace_id(request.headers.get(TRACE_ID_HEADER))
        token = set_current(ctx)
        start = time.perf_counter()
        route = request.url.path
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - 统一归一化出口
                classified = classify(exc)
                self._logger.emit(
                    "request_error",
                    level="error",
                    route=route,
                    method=request.method,
                    error_category=classified.category.value,
                    error_type=type(exc).__name__,  # 内部细节只进日志
                    error_detail=str(exc),
                )
                response = JSONResponse(
                    status_code=classified.status_code,
                    content={
                        "error": {
                            "category": classified.category.value,
                            "message": classified.public_message,
                            "trace_id": ctx.trace_id,
                        }
                    },
                )
            latency_ms = (time.perf_counter() - start) * 1000.0
            status = response.status_code
            self._logger.emit(
                "request_end",
                route=route,
                method=request.method,
                status=status,
                latency_ms=round(latency_ms, 3),
                trace_id_source=ctx.source.value,
            )
            self._metrics.record(route, status, latency_ms, error=status >= 500)
            response.headers[TRACE_ID_HEADER] = ctx.trace_id
            response.headers["X-Trace-Id-Source"] = ctx.source.value
            return response
        finally:
            reset_current(token)
