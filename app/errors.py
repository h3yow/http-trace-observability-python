"""错误分类与归一化。

- 业务可预期错误用 AppError 显式携带类别与对外信息。
- 其余异常统一归入 INTERNAL，对外只暴露类别与通用提示，
  内部细节（异常类型、堆栈、消息）只写入服务端日志，不透出到响应。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException


class ErrorCategory(str, Enum):
    """对外可区分的错误类别。"""

    VALIDATION = "validation"  # 请求参数不合法
    NOT_FOUND = "not_found"  # 资源不存在
    DEPENDENCY = "dependency"  # 下游依赖失败
    INTERNAL = "internal"  # 未预期的内部错误


_CATEGORY_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.VALIDATION: 422,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.DEPENDENCY: 502,
    ErrorCategory.INTERNAL: 500,
}

_PUBLIC_MESSAGES: dict[ErrorCategory, str] = {
    ErrorCategory.VALIDATION: "request validation failed",
    ErrorCategory.NOT_FOUND: "resource not found",
    ErrorCategory.DEPENDENCY: "upstream dependency failed",
    ErrorCategory.INTERNAL: "internal server error",
}


@dataclass(frozen=True)
class ClassifiedError:
    """归一化后的错误视图：对外只暴露类别与安全信息。"""

    category: ErrorCategory
    public_message: str
    status_code: int


class AppError(Exception):
    """业务可预期错误，携带对外类别。"""

    def __init__(
        self,
        category: ErrorCategory,
        public_message: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.category = category
        self.public_message = public_message or _PUBLIC_MESSAGES[category]
        self.status_code = status_code or _CATEGORY_STATUS[category]
        super().__init__(self.public_message)


def category_for_status(status_code: int) -> ErrorCategory | None:
    """按状态码推断错误类别（用于未抛异常的错误响应）；2xx/3xx 返回 None。"""
    if status_code < 400:
        return None
    if status_code == 404:
        return ErrorCategory.NOT_FOUND
    if status_code < 500:
        return ErrorCategory.VALIDATION
    return ErrorCategory.INTERNAL


def classify(exc: BaseException) -> ClassifiedError:
    """将任意异常归一化为可对外暴露的错误视图，内部细节不透出。"""
    if isinstance(exc, AppError):
        return ClassifiedError(
            category=exc.category,
            public_message=exc.public_message,
            status_code=exc.status_code,
        )
    if isinstance(exc, RequestValidationError):
        return ClassifiedError(
            category=ErrorCategory.VALIDATION,
            public_message=_PUBLIC_MESSAGES[ErrorCategory.VALIDATION],
            status_code=422,
        )
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            category = ErrorCategory.NOT_FOUND
        elif 400 <= exc.status_code < 500:
            category = ErrorCategory.VALIDATION
        else:
            category = ErrorCategory.INTERNAL
        return ClassifiedError(
            category=category,
            public_message=_PUBLIC_MESSAGES[category],
            status_code=exc.status_code if exc.status_code >= 400 else 500,
        )
    # 未预期异常：一律归为 INTERNAL，细节不透出
    return ClassifiedError(
        category=ErrorCategory.INTERNAL,
        public_message=_PUBLIC_MESSAGES[ErrorCategory.INTERNAL],
        status_code=500,
    )
