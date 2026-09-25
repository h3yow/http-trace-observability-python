"""错误归一化：异常分类、统一错误模型与安全转换。

对外错误只暴露归一化后的类别与固定文案，内部异常的 ``str()``、堆栈等细节
一律不进入响应体，避免内部信息泄漏。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    """对外可区分的归一化错误类别。"""

    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    UPSTREAM_FAILURE = "upstream_failure"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


# 各类别对应的 HTTP 状态码与固定对外文案
_CATEGORY_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.VALIDATION: 422,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.UNAVAILABLE: 503,
    ErrorCategory.UPSTREAM_FAILURE: 502,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.INTERNAL: 500,
}
_CATEGORY_MESSAGE: dict[ErrorCategory, str] = {
    ErrorCategory.VALIDATION: "request validation failed",
    ErrorCategory.NOT_FOUND: "resource not found",
    ErrorCategory.UNAVAILABLE: "service temporarily unavailable",
    ErrorCategory.UPSTREAM_FAILURE: "upstream dependency failure",
    ErrorCategory.TIMEOUT: "request timed out",
    ErrorCategory.INTERNAL: "internal server error",
}


@dataclass(frozen=True, slots=True)
class NormalizedError:
    """归一化后的错误结论。"""

    category: ErrorCategory
    status_code: int
    public_message: str


class AppError(Exception):
    """携带归一化类别的业务异常基类。"""

    category: ErrorCategory = ErrorCategory.INTERNAL
    status_code: int = 500
    public_message: str = "internal error"


# 已知基础设施异常到归一化类别的映射（内部细节不透出）
_EXCEPTION_CATEGORY: dict[type[BaseException], ErrorCategory] = {
    TimeoutError: ErrorCategory.TIMEOUT,
    asyncio.TimeoutError: ErrorCategory.TIMEOUT,
    ConnectionError: ErrorCategory.UNAVAILABLE,
}


def normalize(exc: BaseException) -> NormalizedError:
    """把任意异常归一化为安全的对外错误结论。"""
    # 1) 受控业务异常：沿用其声明的类别、状态码与文案
    if isinstance(exc, AppError):
        return NormalizedError(exc.category, exc.status_code, exc.public_message)

    # 2) 已知基础设施异常：按 MRO 精确匹配映射，文案使用固定模板
    category = ErrorCategory.INTERNAL
    for exc_type in type(exc).__mro__:
        mapped = _EXCEPTION_CATEGORY.get(exc_type)
        if mapped is not None:
            category = mapped
            break

    return NormalizedError(
        category=category,
        status_code=_CATEGORY_STATUS[category],
        public_message=_CATEGORY_MESSAGE[category],
    )
