"""指标聚合测试：有界维度、兜底分桶、高基数不失控、写入失败隔离。"""
from __future__ import annotations

import concurrent.futures

from app.metrics import MAX_SERIES, MetricsRegistry, status_bucket


def test_high_cardinality_labels_are_bounded():
    reg = MetricsRegistry()
    # 10 万个高基数标签值，模拟 request_id 维度直接进标签的恶劣情况
    for i in range(100_000):
        reg.observe(
            phase=f"phase-{i}",
            result=f"result-{i}",
            status_bucket=status_bucket(200 + (i % 4) * 100),
            duration_ms=float(i % 1000),
        )
    snap = reg.snapshot()
    print(
        f"\n[metrics] 输入 100000 个高基数标签 -> series={snap['series_count']} "
        f"确定上界 max_series={snap['bounds']['max_series']}"
    )
    assert snap["series_count"] <= MAX_SERIES
    assert snap["bounds"]["max_series"] == MAX_SERIES
    assert all(s["phase"] == "other" for s in snap["series"])
    assert all(s["result"] == "other" for s in snap["series"])
    total = sum(s["count"] for s in snap["series"])
    assert total == 100_000


def test_known_dimensions_are_preserved():
    reg = MetricsRegistry()
    reg.observe("request", "success", "2xx", 10.0)
    reg.observe("request", "success", "2xx", 20.0)
    reg.observe("request", "not_found", "4xx", 5.0)
    snap = reg.snapshot()
    keys = {(s["phase"], s["result"], s["status"]) for s in snap["series"]}
    print(f"\n[metrics] 已知维度保留: keys={sorted(keys)}")
    assert ("request", "success", "2xx") in keys
    assert ("request", "not_found", "4xx") in keys
    success = next(s for s in snap["series"] if s["result"] == "success")
    assert success["latency_ms"]["count"] == 2
    assert success["latency_ms"]["avg"] == 15.0
    assert success["latency_ms"]["min"] == 10.0
    assert success["latency_ms"]["max"] == 20.0


def test_status_buckets_are_stable():
    mapping = {code: status_bucket(code) for code in (200, 204, 301, 404, 422, 500, 503, 999)}
    print(f"\n[metrics] 状态码分桶归一化: {mapping}")
    assert mapping == {
        200: "2xx", 204: "2xx", 301: "3xx", 404: "4xx",
        422: "4xx", 500: "5xx", 503: "5xx", 999: "xxx",
    }


def test_concurrent_observations_remain_consistent():
    reg = MetricsRegistry()

    def worker(_: int) -> None:
        reg.observe("request", "success", "2xx", 1.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(worker, range(5000)))
    snap = reg.snapshot()
    total = sum(s["count"] for s in snap["series"])
    print(f"\n[metrics] 32 线程并发记录 5000 次 -> 实际累计 {total}")
    assert total == 5000


def test_metrics_write_failure_is_isolated():
    reg = MetricsRegistry()
    reg.force_fail = True
    reg.observe("request", "success", "2xx", 1.0)  # 不应抛出
    assert reg.failed_writes == 1
    snap = reg.snapshot()  # 读取仍然可用
    print(f"\n[metrics] 写入失败计数={reg.failed_writes}，读取仍可用 series={snap['series_count']}")
    assert snap["series"] == []
