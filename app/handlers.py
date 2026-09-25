"""示例业务处理器：覆盖成功、业务异常、内部错误与超时等路径。

这些路由用于演示不同归一化错误类别，响应中只回显当前链路标识，
不依赖任何外部资源。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from .context import current_request_id
from .errors import AppError, ErrorCategory

router = APIRouter()


class NotFoundError(AppError):
    category = ErrorCategory.NOT_FOUND
    status_code = 404
    public_message = "resource not found"


class ValidationFailure(AppError):
    category = ErrorCategory.VALIDATION
    status_code = 422
    public_message = "request validation failed"


class UpstreamUnavailable(AppError):
    category = ErrorCategory.UPSTREAM_FAILURE
    status_code = 502
    public_message = "upstream dependency failure"


SLOW_TIMEOUT_MS = 100


@router.get("/")
async def root() -> dict[str, str]:
    """健康首页：回显当前请求标识。"""
    return {"status": "ok", "request_id": current_request_id()}


@router.get("/items/{item_id}")
async def get_item(item_id: int) -> dict[str, object]:
    """按编号取资源，演示参数校验、未找到与上游失败。

    * 负数：参数校验失败（422/validation）；
    * 404：资源不存在（404/not_found）；
    * 502：上游依赖故障（502/upstream_failure）；
    * 其余：成功。
    """
    if item_id < 0:
        raise ValidationFailure(f"negative item id: {item_id}")
    if item_id == 404:
        raise NotFoundError("item 404 not found")
    if item_id == 502:
        raise UpstreamUnavailable("connection refused to upstream 10.0.0.1:6379")
    return {"item_id": item_id, "request_id": current_request_id()}


@router.get("/boom")
async def boom() -> dict[str, object]:
    """抛出未受控内部错误，演示 internal 归一化与细节脱敏。"""
    raise RuntimeError("secret detail: token=AKIA-INTERNAL should not leak")


@router.get("/slow")
async def slow(ms: int = 50) -> dict[str, object]:
    """模拟延迟；超过 100ms 视为超时（504/timeout）。

    实际等待时间封顶 60ms，避免测试拖慢：超过阈值只决定是否归一化为超时，
    并不真正等待完整时长。
    """
    capped_ms = min(ms, SLOW_TIMEOUT_MS // 2)
    await asyncio.sleep(capped_ms / 1000.0)
    if ms > SLOW_TIMEOUT_MS:
        raise asyncio.TimeoutError()
    return {"slept_ms": capped_ms, "request_id": current_request_id()}
