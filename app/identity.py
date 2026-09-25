"""请求级链路标识：解析、校验与生成。

标识规则
--------
* 合法外部标识（``X-Request-ID``）：长度 8~128，字符仅限 ASCII 字母、数字
  以及连接符 ``-_.~``。合法则原样沿用（``supplied=True``）。
* 缺失或为空白：替换为内部生成标识，原因为 ``missing``。
* 超长（>128）：替换，原因为 ``too_long``。
* 含不可打印/控制字符：替换，原因为 ``non_printable``。
* 其它非法字符或长度不足 8：替换，原因为 ``malformed``。

内部生成标识形如 ``req-<uuid4hex>``，长度 36，自带前缀，绝不与外部值冲突。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum


class IdRejectReason(str, Enum):
    """外部标识被拒绝（替换）的可区分原因。"""

    MISSING = "missing"
    MALFORMED = "malformed"
    TOO_LONG = "too_long"
    NON_PRINTABLE = "non_printable"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """一次请求的链路标识解析结果。"""

    request_id: str
    supplied: bool
    reason: IdRejectReason | None


# 合法外部标识：8~128 字符；字母/数字及连接符（-_.~）
MAX_ID_LEN = 128
MIN_ID_LEN = 8
_ALLOWED_EXTRA = frozenset("-_.~")
_INTERNAL_PREFIX = "req-"


def _is_allowed_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in _ALLOWED_EXTRA)


def new_request_id() -> str:
    """生成一个新的内部请求标识。"""
    return f"{_INTERNAL_PREFIX}{uuid.uuid4().hex}"


def resolve_identity(raw: str | None) -> RequestIdentity:
    """解析外部传入标识：合法沿用，非法/缺失则替换并给出可区分原因。"""
    # 1) 缺失 / 空白
    if raw is None or raw.strip() == "":
        return RequestIdentity(new_request_id(), supplied=False, reason=IdRejectReason.MISSING)

    candidate = raw.strip()

    # 2) 超长优先判定，给出独立原因
    if len(candidate) > MAX_ID_LEN:
        return RequestIdentity(new_request_id(), supplied=False, reason=IdRejectReason.TOO_LONG)

    # 3) 字符集：不可打印字符与普通非法字符分开归因
    for ch in candidate:
        if not ch.isprintable():
            return RequestIdentity(
                new_request_id(), supplied=False, reason=IdRejectReason.NON_PRINTABLE
            )
    for ch in candidate:
        if not _is_allowed_char(ch):
            return RequestIdentity(
                new_request_id(), supplied=False, reason=IdRejectReason.MALFORMED
            )

    # 4) 长度不足
    if len(candidate) < MIN_ID_LEN:
        return RequestIdentity(new_request_id(), supplied=False, reason=IdRejectReason.MALFORMED)

    return RequestIdentity(candidate, supplied=True, reason=None)
