"""请求中间件：串联链路标识、结构化日志、指标、错误归一化与诊断时间线保留。"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.errors import category_for_status, classify
from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.sampling import SamplingConfig, should_sample
from app.timeline import TimelineStore, TraceRecord, TraceStage, utc_now_iso
from app.tracing import reset_current, resolve_trace_id, set_current

TRACE_ID_HEADER = "X-Trace-Id"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """为每个请求建立链路上下文，输出结构化日志、聚合指标并按采样保留诊断时间线。

    任何异常在此被归一化为统一错误响应；日志/指标/时间线保留任一环节失败
    都不影响响应的状态码、响应头与响应体。
    """

    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        logger: StructuredLogger,
        metrics: MetricsRegistry,
        store: TimelineStore | None = None,
        sampling: SamplingConfig | None = None,
    ) -> None:
        super().__init__(app)
        self._logger = logger
        self._metrics = metrics
        self._store = store
        self._sampling = sampling or SamplingConfig()

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        ctx = resolve_trace_id(request.headers.get(TRACE_ID_HEADER))
        token = set_current(ctx)
        start = time.perf_counter()
        route = request.url.path
        stages: list[TraceStage] = [TraceStage(name="received", offset_ms=0.0, duration_ms=0.0)]
        error_category: str | None = None

        def mark(name: str) -> None:
            offset = (time.perf_counter() - start) * 1000.0
            stages.append(TraceStage(name=name, offset_ms=offset,
                                     duration_ms=offset - stages[-1].offset_ms))

        try:
            try:
                response = await call_next(request)
                mark("handler_completed")
            except Exception as exc:  # noqa: BLE001 - 统一归一化出口
                classified = classify(exc)
                error_category = classified.category.value
                mark("handler_failed")
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
            if error_category is None:
                derived = category_for_status(status)
                error_category = derived.value if derived else None
            mark("response_finished")
            self._logger.emit(
                "request_end",
                route=route,
                method=request.method,
                status=status,
                latency_ms=round(latency_ms, 3),
                trace_id_source=ctx.source.value,
            )
            self._metrics.record(route, status, latency_ms, error=status >= 500)
            self._retain(ctx, request, route, status, latency_ms, error_category, stages)
            response.headers[TRACE_ID_HEADER] = ctx.trace_id
            response.headers["X-Trace-Id-Source"] = ctx.source.value
            return response
        finally:
            reset_current(token)

    def _retain(  # type: ignore[no-untyped-def]
        self, ctx, request: Request, route: str, status: int,
        latency_ms: float, error_category: str | None, stages: list[TraceStage],
    ) -> None:
        """按采样保留诊断时间线；任何失败只计数，绝不影响响应与指标。"""
        store = self._store
        if store is None or not should_sample(ctx.trace_id, self._sampling):
            return
        record = TraceRecord(
            trace_id=ctx.trace_id,
            source=ctx.source.value,
            route=route,
            method=request.method,
            status=status,
            latency_ms=latency_ms,
            error_category=error_category,
            stages=tuple(stages),
            finished_at=utc_now_iso(),
        )
        try:
            store.put(record)
        except Exception:  # noqa: BLE001 - 保留环节失败必须被隔离
            try:
                store.note_write_failure()
            except Exception:  # noqa: BLE001 - 计数本身也失败则彻底放弃
                pass
