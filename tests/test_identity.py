"""标识解析测试：合法沿用、缺失/非法替换、原因可区分。"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.identity import (
    IdRejectReason,
    MAX_ID_LEN,
    RequestIdentity,
    new_request_id,
    resolve_identity,
)

CASES = [
    # (输入, 是否沿用, 拒绝原因, 判定依据)
    ("trace-0123456789abcdef", True, None, "合法字符集且长度在 8~128 内"),
    ("ABCDefgh1234._-~", True, None, "允许字母数字与 -_.~ 连接符"),
    (None, False, IdRejectReason.MISSING, "外部未提供标识"),
    ("", False, IdRejectReason.MISSING, "空字符串视同缺失"),
    ("    ", False, IdRejectReason.MISSING, "纯空白视同缺失"),
    ("x" * (MAX_ID_LEN + 1), False, IdRejectReason.TOO_LONG, "长度超过 128"),
    ("abc\tdef12", False, IdRejectReason.NON_PRINTABLE, "含制表符等不可打印字符"),
    ("abc\x00def12", False, IdRejectReason.NON_PRINTABLE, "含 NUL 控制字符"),
    ("bad id 12", False, IdRejectReason.MALFORMED, "含非法空格"),
    ("id=1234!", False, IdRejectReason.MALFORMED, "含 =! 等非法字符"),
    ("short1", False, IdRejectReason.MALFORMED, "长度不足 8"),
    ("汉字标识1234", False, IdRejectReason.MALFORMED, "非 ASCII 字符"),
]


@pytest.mark.parametrize("raw,supplied,reason,why", CASES)
def test_resolve_identity(raw, supplied, reason, why):
        result = resolve_identity(raw)
        # 单测日志：输入、判定依据与归一化结果
        print(f"\n[identity] input={raw!r} basis={why} "
              f"-> supplied={result.supplied} reason={result.reason} "
              f"request_id={result.request_id}")
        assert result.supplied is supplied
        assert result.reason is reason
        if supplied:
            assert result.request_id == raw
        else:
            assert result.request_id.startswith("req-")
            assert result.request_id != raw
        assert isinstance(result, RequestIdentity)


def test_generated_ids_are_unique_under_concurrency():
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: new_request_id(), range(2000)))
    print(f"\n[identity] 并发生成 2000 个标识，去重后数量={len(set(ids))}")
    assert len(set(ids)) == 2000


def test_reasons_are_distinguishable():
    reasons = {resolve_identity(v).reason for v in (None, "x" * 129, "a\tb12345", "bad id!!")}
    print(f"\n[identity] 非法输入归一化原因集合={sorted(r.value for r in reasons)}")
    assert reasons == {
        IdRejectReason.MISSING,
        IdRejectReason.TOO_LONG,
        IdRejectReason.NON_PRINTABLE,
        IdRejectReason.MALFORMED,
    }
