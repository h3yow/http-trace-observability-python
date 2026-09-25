"""链路标识规则与请求上下文传播。

规则：
- 合法标识：8~64 个十六进制字符（大小写均可），且不全为 0。
- 外部传入合法标识：原样沿用（归一化为小写）。
- 缺失或非法：服务端生成 32 位小写十六进制标识，并记录可区分的原因。
- 上下文通过 contextvars 传播，并发请求之间互不串扰。
"""
from __future__ import annotations

import contextvars
import secrets
from dataclasses import dataclass
from enum import Enum

_MIN_LEN = 8
_MAX_LEN = 64
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class TraceIdSource(str, Enum):
    """链路标识来源。"""

    EXTERNAL = "external"  # 外部显式传入且合法，被沿用
    GENERATED_MISSING = "generated_missing"  # 外部缺失，服务端生成
    GENERATED_INVALID = "generated_invalid"  # 外部传入但非法，服务端替换


@dataclass(frozen=True)
class TraceContext:
    """单请求链路上下文。"""

    trace_id: str
    source: TraceIdSource


def is_valid_trace_id(value: str | None) -> bool:
    """判断外部传入的链路标识是否合法。"""
    if value is None:
        return False
    if not (_MIN_LEN <= len(value) <= _MAX_LEN):
        return False
    if any(ch not in _HEX_DIGITS for ch in value):
        return False
    if set(value) <= {"0"}:  # 全 0 视为非法（与 W3C 约定一致）
        return False
    return True


def generate_trace_id() -> str:
    """生成新的链路标识：32 位小写十六进制。"""
    return secrets.token_hex(16)


def resolve_trace_id(raw: str | None) -> TraceContext:
    """根据外部传入值解析出本次请求的链路上下文。"""
    if raw is None or raw.strip() == "":
        return TraceContext(trace_id=generate_trace_id(), source=TraceIdSource.GENERATED_MISSING)
    candidate = raw.strip()
    if is_valid_trace_id(candidate):
        return TraceContext(trace_id=candidate.lower(), source=TraceIdSource.EXTERNAL)
    return TraceContext(trace_id=generate_trace_id(), source=TraceIdSource.GENERATED_INVALID)


_current: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "trace_context", default=None
)


def set_current(ctx: TraceContext) -> contextvars.Token:
    """设置当前请求上下文，返回用于复位的 token。"""
    return _current.set(ctx)


def get_current() -> TraceContext | None:
    """读取当前请求上下文。"""
    return _current.get()


def reset_current(token: contextvars.Token) -> None:
    """复位当前请求上下文。"""
    _current.reset(token)
