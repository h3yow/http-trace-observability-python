"""错误分类与归一化测试。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.errors import ErrorCategory
from tests.conftest import ListSink, explain


def _check(client: TestClient, sink: ListSink, case: str, url: str,
           expected_status: int, expected_category: ErrorCategory) -> None:
    resp = client.get(url)
    body = resp.json()
    normalized = {
        "status": resp.status_code,
        "category": body["error"]["category"],
        "message": body["error"]["message"],
        "trace_id": body["error"]["trace_id"],
    }
    explain(case, {"GET": url},
            f"异常归一化为类别 {expected_category.value}，状态码 {expected_status}，"
            "响应仅含 category/message/trace_id，内部细节不透出", normalized)
    assert resp.status_code == expected_status
    assert normalized["category"] == expected_category.value
    assert set(body["error"].keys()) == {"category", "message", "trace_id"}
    # 服务端日志留有可关联记录
    end = [r for r in sink.records() if r.get("event") == "request_end"]
    assert end and end[-1]["status"] == expected_status
    assert end[-1]["trace_id"] == normalized["trace_id"]


def test_not_found_normalized(client: TestClient, sink: ListSink) -> None:
    _check(client, sink, "资源不存在", "/items/999", 404, ErrorCategory.NOT_FOUND)


def test_dependency_normalized(client: TestClient, sink: ListSink) -> None:
    _check(client, sink, "下游依赖失败", "/flaky?fail=true", 502, ErrorCategory.DEPENDENCY)


def test_validation_normalized(client: TestClient, sink: ListSink) -> None:
    _check(client, sink, "参数越界", "/work?ms=-1", 422, ErrorCategory.VALIDATION)


def test_unknown_route_normalized(client: TestClient, sink: ListSink) -> None:
    _check(client, sink, "未知路由", "/no-such-route", 404, ErrorCategory.NOT_FOUND)


def test_internal_error_details_not_leaked(client: TestClient, sink: ListSink) -> None:
    resp = client.get("/boom")
    body = resp.json()
    normalized = {"status": resp.status_code, "body": body}
    explain("内部错误归一化", {"GET": "/boom"},
            "RuntimeError(含密码/内网地址) -> internal/500；细节只进服务端日志", normalized)
    assert resp.status_code == 500
    assert body["error"]["category"] == ErrorCategory.INTERNAL.value
    for leaked in ("secret", "10.0.0.1", "password", "RuntimeError"):
        assert leaked not in resp.text
    # 内部细节应出现在服务端错误日志中，且与响应同一 trace_id
    err = [r for r in sink.records() if r.get("event") == "request_error"]
    assert err, "应有 request_error 日志"
    rec = err[-1]
    print(f"[server-log] {json.dumps(rec, ensure_ascii=False)}")
    assert rec["error_type"] == "RuntimeError"
    assert "secret" in rec["error_detail"]
    assert rec["trace_id"] == body["error"]["trace_id"]
