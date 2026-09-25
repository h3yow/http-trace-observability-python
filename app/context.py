"""请求上下文：基于 :mod:`contextvars` 的并发隔离存储。

每个请求在自己的 asyncio Task 中绑定标识，天然实现并发请求互不串扰；
``bind`` 返回重置令牌，请求结束后恢复外层上下文。
"""
from __future__ import annotations

from contextvars import ContextVar, Token

from .identity import RequestIdentity

_current: ContextVar[RequestIdentity | None] = ContextVar("request_identity", default=None)

UNBOUND_REQUEST_ID = "req-unbound"


def bind(identity: RequestIdentity) -> Token[RequestIdentity | None]:
    """把标识绑定到当前执行上下文，返回用于恢复的令牌。"""
    return _current.set(identity)


def reset(token: Token[RequestIdentity | None]) -> None:
    """恢复到绑定前的上下文。"""
    _current.reset(token)


def set_current(identity: RequestIdentity) -> Token[RequestIdentity | None]:
    """``bind`` 的语义化别名。"""
    return bind(identity)


def current_identity() -> RequestIdentity | None:
    """读取当前请求标识；未绑定时返回 ``None``。"""
    return _current.get()


def current_request_id() -> str:
    """读取当前请求标识字符串；未绑定时返回占位值 ``req-unbound``。"""
    identity = _current.get()
    return identity.request_id if identity is not None else UNBOUND_REQUEST_ID
