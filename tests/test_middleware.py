"""中间件测试：标识回写、非法标识原因、并发请求上下文隔离。"""
from __future__ import annotations

import concurrent.futures
import json

import pytest

HEADER_CASES = [
    # (传入头, 期望来源标记, 判定依据)
    ("trace-0123456789abcdef", "supplied", "合法外部标识必须沿用"),
    (None, "generated:missing", "未提供标识时替换，原因 missing"),
    ("", "generated:missing", "空头替换，原因 missing"),
    ("x" * 129, "generated:too_long", "超长标识替换，原因 too_long"),
    ("abc\tdef12", "generated:non_printable", "不可打印字符替换，原因 non_printable"),
    ("bad id!!", "generated:malformed", "非法字符替换，原因 malformed"),
    ("short", "generated:malformed", "长度不足替换，原因 malformed"),
]


@pytest.mark.parametrize("raw,source,why", HEADER_CASES)
def test_request_id_header_resolution(client, raw, source, why):
    headers = {"X-Request-ID": raw} if raw is not None else {}
    resp = client.get("/", headers=headers)
    body_id = resp.json()["request_id"]
    print(
        f"\n[middleware] input={raw!r} basis={why} -> "
        f"source={resp.headers['x-request-id-source']} body_id={body_id}"
    )
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == body_id
    assert resp.headers["x-request-id-source"] == source
    if source == "supplied":
        assert body_id == raw
    else:
        assert body_id.startswith("req-")
        assert body_id != raw


def test_error_responses_carry_request_id(client):
    resp = client.get("/items/404", headers={"X-Request-ID": "err-trace-0001"})
    payload = resp.json()
    print(f"\n[middleware] 错误响应体={payload} 响应头 id={resp.headers['x-request-id']}")
    assert resp.status_code == 404
    assert payload["error"]["request_id"] == "err-trace-0001"
    assert resp.headers["x-request-id"] == "err-trace-0001"


def test_concurrent_requests_do_not_cross_talk(client, log_stream):
    n = 100

    def one(i: int):
        rid = f"trace-concurrent-{i:08d}"
        # 偶数请求打 /items/1（成功），奇数打 /items/404（错误）
        path = "/items/404" if i % 2 else "/items/1"
        resp = client.get(path, headers={"X-Request-ID": rid})
        return rid, resp.headers["x-request-id"], path, resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(one, range(n)))

    # 响应体/响应头的 id 必须与传入一致
    mismatches = [(rid, echo) for rid, echo, _p, _s in results if rid != echo]
    print(
        f"\n[middleware] {n} 个并发请求（20 线程），响应头标识不一致数={len(mismatches)}"
    )
    assert mismatches == []
    assert len({rid for rid, _e, _p, _s in results}) == n

    # 日志中每条 complete 记录的 request_id、path、status_code 必须自洽，
    # 据此证明并发下日志没有与别的请求串线
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    events = [e for e in lines if e.get("event") == "request.complete"]
    print(f"[middleware] 采集 request.complete 日志 {len(events)} 条")
    assert len(events) == n
    for rid, _echo, path, status in results:
        match = [e for e in events if e["request_id"] == rid]
        assert len(match) == 1, f"{rid} 日志条数异常: {len(match)}"
        assert match[0]["path"] == path
        assert match[0]["status_code"] == status


def test_internal_error_detail_never_leaks(client):
    resp = client.get("/boom")
    print(f"\n[middleware] /boom 响应={resp.text}")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["category"] == "internal"
    assert "AKIA" not in resp.text
    assert "token" not in resp.text
