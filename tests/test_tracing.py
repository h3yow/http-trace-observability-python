"""链路标识与上下文传播测试。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.tracing import TraceIdSource, is_valid_trace_id
from tests.conftest import explain


def test_external_valid_id_is_reused(client: TestClient) -> None:
    raw = "ABCDEF0123456789abcdef0123456789"
    resp = client.get("/work", headers={"X-Trace-Id": raw})
    normalized = {
        "status": resp.status_code,
        "trace_id": resp.headers["x-trace-id"],
        "source": resp.headers["x-trace-id-source"],
    }
    explain("外部合法标识沿用", {"X-Trace-Id": raw},
            "32 位十六进制 -> 合法，归一化为小写并沿用", normalized)
    assert resp.status_code == 200
    assert normalized["trace_id"] == raw.lower()
    assert normalized["source"] == TraceIdSource.EXTERNAL.value
    assert resp.json()["trace_id"] == raw.lower()


def test_missing_id_is_generated(client: TestClient) -> None:
    resp = client.get("/")
    normalized = {
        "trace_id": resp.headers["x-trace-id"],
        "source": resp.headers["x-trace-id-source"],
    }
    explain("缺失标识服务端生成", {"X-Trace-Id": None},
            "头部缺失 -> generated_missing，生成 32 位小写十六进制", normalized)
    assert normalized["source"] == TraceIdSource.GENERATED_MISSING.value
    assert is_valid_trace_id(normalized["trace_id"])


def test_invalid_id_is_replaced_with_distinguishable_reason(client: TestClient) -> None:
    for raw in ["not-a-trace!!", "abc", "0" * 32, "g" * 32]:
        resp = client.get("/", headers={"X-Trace-Id": raw})
        normalized = {
            "trace_id": resp.headers["x-trace-id"],
            "source": resp.headers["x-trace-id-source"],
        }
        explain("非法标识被替换", {"X-Trace-Id": raw},
                "非十六进制/长度越界/全 0 -> generated_invalid，且替换值合法", normalized)
        assert normalized["source"] == TraceIdSource.GENERATED_INVALID.value
        assert normalized["trace_id"] != raw
        assert is_valid_trace_id(normalized["trace_id"])


def test_concurrent_requests_do_not_cross_contaminate(client: TestClient) -> None:
    ids = [f"{i:032x}" for i in range(1, 33)]

    def hit(trace_id: str) -> dict:
        resp = client.get("/work", params={"ms": 5}, headers={"X-Trace-Id": trace_id})
        return {
            "expected": trace_id,
            "header": resp.headers["x-trace-id"],
            "body": resp.json()["trace_id"],
        }

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(hit, ids))

    mismatches = [r for r in results if r["expected"] != r["header"] or r["expected"] != r["body"]]
    explain("并发请求上下文隔离",
            {"concurrency": 16, "requests": len(ids), "ids_sample": ids[:3]},
            "每个请求响应头与响应体内的 trace_id 必须等于各自传入值",
            {"total": len(results), "mismatches": len(mismatches)})
    assert mismatches == []
