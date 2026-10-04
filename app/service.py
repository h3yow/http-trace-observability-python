"""应用装配：构建 FastAPI 应用并挂载可观测能力。"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, ErrorCategory, classify
from app.logging import StructuredLogger, new_stdout_sink
from app.metrics import MetricsRegistry
from app.middleware import ObservabilityMiddleware
from app.sampling import SamplingConfig
from app.timeline import LookupStatus, TimelineStore
from app.tracing import get_current

_ITEMS = {"1": {"id": "1", "name": "alpha"}, "2": {"id": "2", "name": "beta"}}


def _error_response(request: Request, exc: BaseException) -> JSONResponse:
    classified = classify(exc)
    ctx = get_current()
    return JSONResponse(
        status_code=classified.status_code,
        content={
            "error": {
                "category": classified.category.value,
                "message": classified.public_message,
                "trace_id": ctx.trace_id if ctx else None,
            }
        },
    )


def create_app(
    logger: StructuredLogger | None = None,
    metrics: MetricsRegistry | None = None,
    store: TimelineStore | None = None,
    sampling: SamplingConfig | None = None,
) -> FastAPI:
    """构建应用：注册中间件、路由与异常处理。"""
    logger = logger or StructuredLogger(sink=new_stdout_sink())
    metrics = metrics or MetricsRegistry()
    store = store or TimelineStore()
    sampling = sampling or SamplingConfig()

    app = FastAPI()
    app.state.logger = logger
    app.state.metrics = metrics
    app.state.timeline_store = store
    app.state.sampling = sampling

    app.add_middleware(
        ObservabilityMiddleware, logger=logger, metrics=metrics, store=store, sampling=sampling
    )

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(request, exc)

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"Hello": "World"}

    @app.get("/metrics")
    def read_metrics() -> dict:
        return metrics.snapshot()

    @app.get("/trace-config")
    def read_trace_config() -> dict:
        """读取当前生效的采样配置与保留/淘汰/命中计数。"""
        return {"sampling": sampling.as_dict(), "retention": store.stats()}

    @app.get("/traces/{trace_id}")
    def read_trace(trace_id: str) -> JSONResponse:
        """按链路标识回读诊断时间线；区分 found / evicted / never_seen。"""
        result = store.get(trace_id)
        if result.status is LookupStatus.FOUND:
            return JSONResponse(
                status_code=200,
                content={"status": result.status.value, "record": result.record.as_dict()},
            )
        # evicted：记录曾存在但已淘汰（410）；never_seen：从未出现（404）
        status_code = 410 if result.status is LookupStatus.EVICTED else 404
        return JSONResponse(
            status_code=status_code,
            content={"status": result.status.value, "trace_id": trace_id},
        )

    @app.get("/items/{item_id}")
    def read_item(item_id: str) -> dict:
        item = _ITEMS.get(item_id)
        if item is None:
            raise AppError(ErrorCategory.NOT_FOUND)
        return item

    @app.get("/work")
    async def do_work(ms: int = 0) -> dict:
        """模拟耗时任务；ms 非法时触发参数校验错误。"""
        if ms < 0 or ms > 10_000:
            raise AppError(ErrorCategory.VALIDATION, public_message="ms out of range", status_code=422)
        await asyncio.sleep(ms / 1000.0)
        ctx = get_current()
        return {"trace_id": ctx.trace_id if ctx else None, "slept_ms": ms}

    @app.get("/flaky")
    def flaky(fail: bool = False) -> dict:
        """模拟下游依赖失败。"""
        if fail:
            raise AppError(ErrorCategory.DEPENDENCY)
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict:
        """模拟未预期内部错误：细节不得透出。"""
        raise RuntimeError("db connect failed: password=secret at 10.0.0.1:5432")

    return app
