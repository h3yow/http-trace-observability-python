"""请求中间件：标识解析、上下文绑定、阶段计时与错误归因。

每个 HTTP 请求的处理流程：

1. 从 ``X-Request-ID`` 解析链路标识（合法沿用，非法/缺失替换并记录原因）；
2. 绑定到 :mod:`contextvars`，使并发请求互不串扰；
3. 透传时在响应头回写 ``X-Request-ID`` 与 ``X-Request-ID-Source``；
4. 捕获异常并归一化分类，内部细节只进日志、不进响应；
5. 结束阶段输出统一结构化日志与有界指标（含耗时、状态码桶、结果类别）。
"""
from __future__ import annotations

import time
from typing import Any

from starlette.responses import JSONResponse

from .context import bind, reset
from .errors import ErrorCategory, NormalizedError, normalize
from .identity import IdRejectReason, RequestIdentity, resolve_identity
from .observability import Observability

REQUEST_ID_HEADER = b"x-request-id"

# HTTP 状态码 -> 有界结果类别
_STATUS_RESULT: dict[int, str] = {
    200: "success",
    404: "not_found",
    422: "validation",
    502: "upstream_failure",
    503: "unavailable",
    504: "timeout",
}


def result_from_status(status_code: int) -> str:
    """把状态码归一到有界结果标签；5xx 归 error，其它非常见值归 other。"""
    if status_code in _STATUS_RESULT:
        return _STATUS_RESULT[status_code]
    if 200 <= status_code < 400:
        return "success" if status_code < 300 else "other"
    if 400 <= status_code < 500:
        return "other"
    if 500 <= status_code < 600:
        return "error"
    return "other"


def _error_response(error: NormalizedError, identity: RequestIdentity) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "category": error.category.value,
                "message": error.public_message,
                "request_id": identity.request_id,
            }
        },
        headers={
            "X-Request-ID": identity.request_id,
            "X-Request-ID-Source": "supplied"
            if identity.supplied
            else f"generated:{identity.reason.value if identity.reason else 'unknown'}",
        },
    )


class TraceMiddleware:
    """ASGI 中间件：为每个请求建立可解释的追踪结论。"""

    def __init__(self, app: Any, obs: Observability) -> None:
        self.app = app
        self.obs = obs

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1) 解析外部标识
        raw_id: str | None = None
        for key, value in scope.get("headers", []):
            if key == REQUEST_ID_HEADER:
                raw_id = value.decode("latin-1", errors="replace")
                break
        identity = resolve_identity(raw_id)
        token = bind(identity)

        source = (
            "supplied"
            if identity.supplied
            else f"generated:{identity.reason.value if identity.reason else 'unknown'}"
        )
        started = time.perf_counter()
        status_code = 500
        response_started = False
        captured_error: NormalizedError | None = None

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 500))
                headers = message.setdefault("headers", [])
                # 回写链路标识，避免重复
                headers[:] = [(k, v) for k, v in headers if k != REQUEST_ID_HEADER]
                headers.append((b"x-request-id", identity.request_id.encode("ascii")))
                headers.append(
                    (b"x-request-id-source", source.encode("ascii"))
                )
            await send(message)

        self.obs.event(
            "request.start",
            method=scope.get("method"),
            path=scope.get("path"),
            request_id=identity.request_id,
            id_supplied=identity.supplied,
            id_reason=identity.reason.value if identity.reason else None,
        )

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - 边界处归一化一切异常
            captured_error = normalize(exc)
            # 内部细节（exc 文本/堆栈）绝不进入响应；仅以类别记录
            self.obs.event(
                "request.error",
                request_id=identity.request_id,
                category=captured_error.category.value,
                error_type=type(exc).__name__,
            )
            if not response_started:
                status_code = captured_error.status_code
                response = _error_response(captured_error, identity)
                await response(scope, receive, send)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            if captured_error is not None:
                result = captured_error.category.value
                if result == ErrorCategory.INTERNAL.value:
                    result = "error"
            else:
                result = result_from_status(status_code)
            self.obs.record("request", result, status_code, duration_ms)
            self.obs.event(
                "request.complete",
                request_id=identity.request_id,
                method=scope.get("method"),
                path=scope.get("path"),
                status_code=status_code,
                duration_ms=round(duration_ms, 3),
                result=result,
                id_supplied=identity.supplied,
                id_reason=identity.reason.value if identity.reason else None,
            )
            reset(token)
