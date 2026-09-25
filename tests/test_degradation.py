"""可控退化测试：日志写入失败不影响请求；指标维度有上界与稳定兜底分桶。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.logging import StructuredLogger
from app.metrics import OVERFLOW_BUCKET, MetricsRegistry
from app.service import create_app
from tests.conftest import FailingSink, explain


def test_log_write_failure_does_not_affect_requests() -> None:
    sink = FailingSink()
    logger = StructuredLogger(sink=sink)
    metrics = MetricsRegistry()
    client = TestClient(create_app(logger=logger, metrics=metrics))

    results = []
    for i in range(10):
        resp = client.get("/work", params={"ms": 1})
        results.append(resp.status_code)

    normalized = {
        "statuses": results,
        "sink_attempts": sink.attempts,
        "write_failures": logger.write_failures,
    }
    explain("日志写入失败降级", {"requests": 10, "sink": "always raises OSError"},
            "sink 写入抛错被吞掉并计数；请求结果与指标聚合不受影响", normalized)
    assert results == [200] * 10
    assert logger.write_failures == sink.attempts > 0
    # 指标仍然正常聚合
    snap = metrics.snapshot()
    assert snap["total_requests"] == 10


def test_metrics_dimension_bound_and_overflow_bucket() -> None:
    metrics = MetricsRegistry(max_dimensions=4)
    logger = StructuredLogger(sink=FailingSink())  # 日志也失败，双重退化
    client = TestClient(create_app(logger=logger, metrics=metrics))

    # 用不同路径制造高基数维度（每个路径一个维度）
    for i in range(50):
        resp = client.get(f"/no-such-{i}")
        assert resp.status_code == 404

    snap = client.get("/metrics").json()
    explain("高基数维度退化",
            {"distinct_routes": 50, "max_dimensions": 4},
            "维度数达到上界后新维度进入 (__overflow__, status_class) 兜底桶，"
            "active_dimensions 有界，dropped_dimensions 计数",
            snap)
    assert snap["active_dimensions"] <= 4 + 5  # 上界 + 兜底桶(按 status_class)
    assert snap["dropped_dimensions"] > 0
    overflow_keys = [k for k in snap["routes"] if k.startswith(OVERFLOW_BUCKET)]
    assert overflow_keys, "必须存在稳定兜底分桶"
    overflow_count = sum(snap["routes"][k]["count"] for k in overflow_keys)
    assert overflow_count == snap["dropped_dimensions"]
    # 总请求数守恒：正常 + 兜底 = 全部（/metrics 自身在快照后才计数）
    assert snap["total_requests"] == 50
    assert sum(b["count"] for b in snap["routes"].values()) == 50


def test_metrics_concurrent_recording_consistent() -> None:
    metrics = MetricsRegistry(max_dimensions=8)
    client = TestClient(create_app(logger=StructuredLogger(sink=FailingSink()), metrics=metrics))

    def hit(i: int) -> int:
        return client.get("/work", params={"ms": 1}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(hit, range(64)))

    snap = metrics.snapshot()
    explain("并发指标聚合一致性", {"requests": 64, "concurrency": 16},
            "同一路径并发记录应聚合到同一桶，total_requests 守恒",
            {"statuses_ok": all(s == 200 for s in statuses),
             "total_requests": snap["total_requests"],
             "bucket": snap["routes"].get("/work|2xx")})
    assert all(s == 200 for s in statuses)
    assert snap["routes"]["/work|2xx"]["count"] == 64
    assert snap["total_requests"] == 64  # /metrics 自身在快照后才计数


def test_structured_log_fields_uniform() -> None:
    from tests.conftest import ListSink

    sink = ListSink()
    client = TestClient(create_app(logger=StructuredLogger(sink=sink)))
    client.get("/work", headers={"X-Trace-Id": "abcdef0123456789"})

    records = sink.records()
    end = [r for r in records if r["event"] == "request_end"]
    normalized = end[-1]
    explain("结构化日志统一字段", {"GET": "/work", "X-Trace-Id": "abcdef0123456789"},
            "request_end 必须含 ts/level/event/trace_id/route/method/status/latency_ms",
            normalized)
    for key in ("ts", "level", "event", "trace_id", "route", "method", "status", "latency_ms"):
        assert key in normalized, f"缺少字段 {key}"
    assert normalized["trace_id"] == "abcdef0123456789"
    assert normalized["status"] == 200
