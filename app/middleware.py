"""请求中间件：串联链路标识、结构化日志、指标、错误归一化与诊断回读记录。"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.errors import classify
from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.replay import ReplayPipeline, StageRecord, TraceDiagnostic
from app.tracing import reset_current, resolve_trace_id, set_current

TRACE_ID_HEADER = "X-Trace-Id"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """为每个请求建立链路上下文，输出结构化日志、聚合指标并记录诊断时间线。

    任何异常在此被归一化为统一错误响应；日志/指标/诊断记录写入失败不影响响应。
    """

    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        logger: StructuredLogger,
        metrics: MetricsRegistry,
        replay: ReplayPipeline | None = None,
    ) -> None:
        super().__init__(app)
        self._logger = logger
        self._metrics = metrics
        self._replay = replay

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        ctx = resolve_trace_id(request.headers.get(TRACE_ID_HEADER))
        token = set_current(ctx)
        start = time.perf_counter()
        route = request.url.path
        error_category: str | None = None
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - 统一归一化出口
                classified = classify(exc)
                error_category = classified.category.value
                self._logger.emit(
                    "request_error",
                    level="error",
                    route=route,
                    method=request.method,
                    error_category=error_category,
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
            handler_end = time.perf_counter()
            handler_ms = (handler_end - start) * 1000.0
            status = response.status_code
            self._logger.emit(
                "request_end",
                route=route,
                method=request.method,
                status=status,
                latency_ms=round(handler_ms, 3),
                trace_id_source=ctx.source.value,
            )
            self._metrics.record(route, status, handler_ms, error=status >= 500)
            if self._replay is not None:
                respond_ms = (time.perf_counter() - handler_end) * 1000.0
                self._replay.record(
                    TraceDiagnostic(
                        trace_id=ctx.trace_id,
                        trace_id_source=ctx.source.value,
                        stages=(
                            StageRecord("receive", offset_ms=0.0, duration_ms=0.0),
                            StageRecord("handler", offset_ms=0.0, duration_ms=handler_ms),
                            StageRecord("respond", offset_ms=handler_ms, duration_ms=respond_ms),
                        ),
                        status=status,
                        error_category=error_category,
                        latency_ms=handler_ms + respond_ms,
                    )
                )
            response.headers[TRACE_ID_HEADER] = ctx.trace_id
            response.headers["X-Trace-Id-Source"] = ctx.source.value
            return response
        finally:
            reset_current(token)
