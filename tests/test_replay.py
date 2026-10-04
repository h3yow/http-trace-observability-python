"""按链路回读：采样开关、命中/未命中/已淘汰区分、容量上界与淘汰计数、
保留环节失败隔离、并发下命中与计数守恒。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.sampling import SamplingConfig
from app.service import create_app
from app.timeline import RetentionPolicy, TimelineStore
from tests.conftest import FailingSink, ListSink, explain

TRACE_HEADER = "X-Trace-Id"


def _client(store, sampling, sink=None, metrics=None):
    return TestClient(create_app(
        logger=StructuredLogger(sink=sink or ListSink()),
        metrics=metrics or MetricsRegistry(),
        store=store,
        sampling=sampling,
    ))


def test_sampling_on_records_replayable_timeline() -> None:
    store = TimelineStore()
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0))

    resp = client.get("/work", params={"ms": 5}, headers={TRACE_HEADER: "ABCDEF0123456789"})
    replay = client.get("/traces/abcdef0123456789")

    normalized = replay.json()
    explain("采样开启：按链路命中回读",
            {"GET": "/work?ms=5", "X-Trace-Id": "ABCDEF0123456789（大写）"},
            "合法外部标识归一化为小写沿用；采样 ratio=1.0 全部保留；"
            "回读应含阶段时间线、状态、来源",
            {"resp_status": resp.status_code, "replay": normalized})
    assert resp.status_code == 200
    assert resp.headers[TRACE_HEADER] == "abcdef0123456789"
    assert replay.status_code == 200
    record = normalized["record"]
    assert normalized["status"] == "found"
    assert record["trace_id"] == "abcdef0123456789"
    assert record["source"] == "external"
    assert record["status"] == 200
    assert record["error_category"] is None
    stage_names = [s["name"] for s in record["stages"]]
    assert stage_names == ["received", "handler_completed", "response_finished"]
    assert record["stages"][-1]["offset_ms"] >= 5.0  # 覆盖真实耗时


def test_sampling_off_keeps_response_and_metrics_identical() -> None:
    metrics = MetricsRegistry()
    store = TimelineStore()
    client = _client(store, SamplingConfig(enabled=False, ratio=1.0), metrics=metrics)

    resp = client.get("/work", params={"ms": 1}, headers={TRACE_HEADER: "abcdef0123456789"})
    replay = client.get("/traces/abcdef0123456789")
    config = client.get("/trace-config").json()

    explain("采样关闭：业务结果不变且不可回读",
            {"GET": "/work", "sampling": "enabled=false"},
            "采样只影响诊断记录保留；状态码/响应头/响应体/聚合指标必须与开启时一致；"
            "回读应为 never_seen；配置接口可见 enabled=false",
            {"status": resp.status_code, "headers_trace": resp.headers.get(TRACE_HEADER),
             "body": resp.json(), "replay_status": replay.status_code,
             "replay_body": replay.json(), "config": config["sampling"],
             "store_puts": store.puts})
    assert resp.status_code == 200
    assert resp.headers[TRACE_HEADER] == "abcdef0123456789"
    assert resp.headers["X-Trace-Id-Source"] == "external"
    assert resp.json()["trace_id"] == "abcdef0123456789"
    assert store.puts == 0  # 未保留任何记录
    assert replay.status_code == 404
    assert replay.json()["status"] == "never_seen"
    assert config["sampling"] == {"enabled": False, "ratio": 1.0}
    # 聚合指标照常：/work + /traces + /trace-config 各一次
    assert metrics.snapshot()["total_requests"] == 3


def test_sampling_ratio_deterministic_selection() -> None:
    store = TimelineStore()
    client = _client(store, SamplingConfig(enabled=True, ratio=0.5))
    low_id = "00000001abcdef00"   # 前 8 位映射到低位桶，应被采中
    high_id = "ffffffffabcdef00"  # 前 8 位映射到高位桶，应被放弃

    client.get("/work", headers={TRACE_HEADER: low_id})
    client.get("/work", headers={TRACE_HEADER: high_id})
    # 直接查存储验证取舍（此刻回读请求尚未发出，计数干净）
    low_direct = store.get(low_id).status.value
    high_direct = store.get(high_id).status.value
    low = client.get(f"/traces/{low_id}")
    high = client.get(f"/traces/{high_id}")

    explain("按比例采样：确定性取舍",
            {"ratio": 0.5, "low_id": low_id, "high_id": high_id},
            "判定按 trace_id 前 8 位十六进制映射到 [0,2^32) 与 ratio*2^32 比较；"
            "低位命中、高位放弃，同一标识判定稳定",
            {"low_direct": low_direct, "high_direct": high_direct,
             "low_lookup": low.status_code, "high_lookup": high.status_code})
    assert low_direct == "found"
    assert high_direct == "never_seen"
    assert low.status_code == 200
    assert high.status_code == 404
    assert high.json()["status"] == "never_seen"


def test_lookup_distinguishes_never_seen_and_evicted() -> None:
    store = TimelineStore(RetentionPolicy(max_records=2))
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0))
    ids = ["a000000000000001", "a000000000000002", "a000000000000003"]
    for tid in ids:
        client.get("/work", headers={TRACE_HEADER: tid})

    evicted = client.get(f"/traces/{ids[0]}")   # 最旧，应已被 FIFO 淘汰
    found = client.get(f"/traces/{ids[2]}")     # 最新，应仍在
    never = client.get("/traces/b000000000000009")  # 从未出现

    explain("未命中两分枝：已淘汰 vs 从未出现",
            {"capacity": 2, "written": ids, "lookup": [ids[0], ids[2], "b000000000000009"]},
            "容量 2 写入 3 条，最旧者 FIFO 淘汰并留墓碑；"
            "墓碑内返回 evicted(410)，无痕迹返回 never_seen(404)",
            {"evicted": (evicted.status_code, evicted.json()),
             "found": (found.status_code, found.json()["status"]),
             "never_seen": (never.status_code, never.json())})
    assert evicted.status_code == 410
    assert evicted.json()["status"] == "evicted"
    assert found.status_code == 200
    assert found.json()["record"]["trace_id"] == ids[2]
    assert never.status_code == 404
    assert never.json()["status"] == "never_seen"


def test_capacity_bound_and_eviction_counters() -> None:
    store = TimelineStore(RetentionPolicy(max_records=4))
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0))

    for i in range(10):
        client.get("/work", headers={TRACE_HEADER: f"c0000000{i:08x}"})
    # 命中 2 次、evicted 未命中 1 次、never_seen 未命中 1 次
    # 留存记录为 6..9；墓碑窗口（容量 4）保留最近淘汰的 2..5
    client.get("/traces/c000000000000009")
    client.get("/traces/c000000000000008")
    client.get("/traces/c000000000000005")  # 已淘汰且在墓碑窗口内
    client.get("/traces/d000000000000000")  # 从未出现
    stats = client.get("/trace-config").json()["retention"]

    explain("容量上界与淘汰/命中计数",
            {"capacity": 4, "writes": 10, "lookups": "2 命中 + 1 evicted + 1 never_seen"},
            "stored_records 不超过 max_records；evictions = 写入数 - 留存数；"
            "hits/misses 按查询分支各自计数且互斥",
            stats)
    assert stats["stored_records"] <= 4
    assert stats["evictions"] == stats["puts"] - stats["stored_records"]
    assert stats["evictions"] > 0
    assert stats["hits"] == 2
    assert stats["misses_evicted"] == 1
    assert stats["misses_never_seen"] == 1


class _BrokenStore(TimelineStore):
    """put 永远失败的存储，用于验证保留环节失败隔离。"""

    def put(self, record) -> None:  # type: ignore[no-untyped-def]
        raise OSError("simulated retention backend down")


def test_retention_failure_isolated_from_response_and_metrics() -> None:
    store = _BrokenStore()
    metrics = MetricsRegistry()
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0), metrics=metrics)

    statuses = [client.get("/work", params={"ms": 1}).status_code for _ in range(8)]
    err = client.get("/boom")
    stats = store.stats()

    explain("保留环节失败隔离",
            {"requests": "8x /work + 1x /boom", "store": "put 永远抛 OSError"},
            "put 抛错被中间件吞掉并计入 write_failures；"
            "状态码/响应体/聚合指标不受影响",
            {"statuses": statuses, "boom_status": err.status_code,
             "boom_body": err.json(), "write_failures": stats["write_failures"],
             "total_requests": metrics.snapshot()["total_requests"]})
    assert statuses == [200] * 8
    assert err.status_code == 500
    assert err.json()["error"]["category"] == "internal"
    assert stats["write_failures"] == 9  # 每次请求各失败一次
    assert metrics.snapshot()["total_requests"] == 9  # 聚合不丢不重


def test_error_timeline_records_category_and_replacement_reason() -> None:
    store = TimelineStore()
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0))

    resp = client.get("/boom", headers={TRACE_HEADER: "not-valid!!"})
    tid = resp.headers[TRACE_HEADER]
    replay = client.get(f"/traces/{tid}").json()

    explain("错误请求的时间线：错误类别与标识替换原因",
            {"GET": "/boom", "X-Trace-Id": "not-valid!!（非法）"},
            "非法标识被替换且 source=generated_invalid；未预期异常归类 internal；"
            "时间线含 handler_failed 阶段",
            {"status": resp.status_code, "new_trace_id": tid, "record": replay["record"]})
    assert resp.status_code == 500
    record = replay["record"]
    assert record["source"] == "generated_invalid"
    assert record["error_category"] == "internal"
    assert record["status"] == 500
    assert "handler_failed" in [s["name"] for s in record["stages"]]


def test_concurrent_writes_and_lookups_conserved() -> None:
    store = TimelineStore(RetentionPolicy(max_records=256))
    metrics = MetricsRegistry()
    client = _client(store, SamplingConfig(enabled=True, ratio=1.0), metrics=metrics)
    ids = [f"e{i:015x}" for i in range(64)]

    def work(tid: str) -> tuple[int, str]:
        resp = client.get("/work", params={"ms": 1}, headers={TRACE_HEADER: tid})
        return resp.status_code, resp.headers[TRACE_HEADER]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(work, ids))

    def lookup(tid: str) -> tuple[int, str | None]:
        resp = client.get(f"/traces/{tid}")
        body = resp.json()
        return resp.status_code, body.get("record", {}).get("trace_id")

    with ThreadPoolExecutor(max_workers=16) as pool:
        lookups = list(pool.map(lookup, ids))

    stats = store.stats()
    explain("并发写入与回读守恒",
            {"requests": 64, "concurrency": 16, "capacity": 256},
            "每请求只写一次：64 业务请求 + 64 回读请求 => puts == 128；"
            "并发回读各自只看到自己的 trace_id；hits == 64 且 evictions == 0；"
            "聚合 total_requests 守恒",
            {"all_status_ok": all(s == 200 for s, _ in results),
             "ids_preserved": all(t == i for (_, t), i in zip(results, ids)),
             "lookups_ok": all(s == 200 and r == i for (s, r), i in zip(lookups, ids)),
             "stats": stats, "total_requests": metrics.snapshot()["total_requests"]})
    assert all(s == 200 for s, _ in results)
    assert all(t == i for (_, t), i in zip(results, ids))  # 响应头不串
    assert all(s == 200 and r == i for (s, r), i in zip(lookups, ids))  # 回读不串
    assert stats["puts"] == 128  # 每个请求（含回读请求自身）恰好写一次
    assert stats["hits"] == 64
    assert stats["evictions"] == 0
    assert stats["stored_records"] == 128
    # 聚合守恒：64 业务请求 + 64 回读请求，每个只计一次
    assert metrics.snapshot()["total_requests"] == 128
