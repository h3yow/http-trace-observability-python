"""按链路回读测试：采样、命中/未命中/淘汰区分、容量上界、失败隔离与并发守恒。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.replay import (
    LookupStatus,
    ReplayPipeline,
    ReplayStore,
    SamplingPolicy,
    StageRecord,
    TraceDiagnostic,
)
from app.service import create_app
from tests.conftest import FailingSink, ListSink, explain


def _diag(trace_id: str, status: int = 200) -> TraceDiagnostic:
    return TraceDiagnostic(
        trace_id=trace_id,
        trace_id_source="external",
        stages=(StageRecord("handler", 0.0, 1.0),),
        status=status,
        error_category=None,
        latency_ms=1.0,
    )


def _client_with(pipeline: ReplayPipeline, metrics: MetricsRegistry | None = None) -> TestClient:
    return TestClient(create_app(
        logger=StructuredLogger(sink=ListSink()),
        metrics=metrics or MetricsRegistry(),
        replay=pipeline,
    ))


def test_sampling_on_records_are_replayable() -> None:
    pipeline = ReplayPipeline(store=ReplayStore(capacity=16), sampling=SamplingPolicy(True, 1.0))
    client = _client_with(pipeline)

    raw = "ABCDEF0123456789abcdef0123456789"
    resp = client.get("/work", headers={"X-Trace-Id": raw})
    tid = resp.headers["x-trace-id"]
    replay = client.get(f"/replay/{tid}")
    record = replay.json()["record"]
    normalized = {"status": replay.status_code, "result": replay.json()["result"],
                  "trace_id": record["trace_id"], "source": record["trace_id_source"],
                  "stages": [s["name"] for s in record["stages"]],
                  "final_status": record["status"], "error_category": record["error_category"]}
    explain("采样开启-按链路命中", {"X-Trace-Id": raw, "sampling": "enabled,rate=1.0"},
            "rate=1.0 全量保留；回读应含阶段时间线/最终结果/标识来源", normalized)
    assert resp.status_code == 200
    assert replay.status_code == 200
    assert normalized["result"] == "found"
    assert normalized["trace_id"] == raw.lower()
    assert normalized["source"] == "external"
    assert normalized["stages"] == ["receive", "handler", "respond"]
    assert normalized["final_status"] == 200
    assert normalized["error_category"] is None


def test_error_request_replay_carries_error_category_and_source_reason() -> None:
    pipeline = ReplayPipeline(store=ReplayStore(capacity=16), sampling=SamplingPolicy(True, 1.0))
    client = _client_with(pipeline)

    resp = client.get("/boom", headers={"X-Trace-Id": "bad!!"})
    tid = resp.headers["x-trace-id"]
    record = client.get(f"/replay/{tid}").json()["record"]
    normalized = {"source": record["trace_id_source"], "status": record["status"],
                  "error_category": record["error_category"]}
    explain("错误请求回读", {"X-Trace-Id": "bad!!", "GET": "/boom"},
            "非法标识被替换(generated_invalid)；未预期异常归一化为 internal/500",
            normalized)
    assert resp.status_code == 500
    assert normalized == {"source": "generated_invalid", "status": 500, "error_category": "internal"}


def test_sampling_off_keeps_nothing_but_business_results_unchanged() -> None:
    metrics_on, metrics_off = MetricsRegistry(), MetricsRegistry()
    ids = [f"{i:032x}" for i in range(1, 9)]

    def run(pipeline: ReplayPipeline, metrics: MetricsRegistry) -> list[dict]:
        client = _client_with(pipeline, metrics)
        out = []
        for tid in ids:
            r = client.get("/work", params={"ms": 1}, headers={"X-Trace-Id": tid})
            out.append({"status": r.status_code, "body": r.json(),
                        "source": r.headers["x-trace-id-source"],
                        "trace_id": r.headers["x-trace-id"]})
        return out

    on = run(ReplayPipeline(store=ReplayStore(64), sampling=SamplingPolicy(True, 1.0)), metrics_on)
    off_pipeline = ReplayPipeline(store=ReplayStore(64), sampling=SamplingPolicy(False, 1.0))
    off = run(off_pipeline, metrics_off)
    skipped_after_work = off_pipeline.skipped_not_sampled  # 之后的回读请求也会走同一 pipeline

    off_client = _client_with(off_pipeline, MetricsRegistry())
    lookups = [off_client.get(f"/replay/{tid}").json()["result"] for tid in ids]
    def counts(snap: dict) -> dict:
        return {"total_requests": snap["total_requests"], "total_errors": snap["total_errors"],
                "routes": {k: {"count": v["count"], "error_count": v["error_count"]}
                           for k, v in snap["routes"].items()}}

    normalized = {
        "business_results_identical": on == off,
        "metrics_identical": counts(metrics_on.snapshot()) == counts(metrics_off.snapshot()),
        "skipped_not_sampled": skipped_after_work,
        "recorded": off_pipeline.store.stats()["recorded"],
        "lookup_results": sorted(set(lookups)),
    }
    explain("采样关闭-业务结果不变", {"requests": 8, "sampling": "disabled"},
            "未采样请求的状态码/响应头/响应体/聚合数字必须与采样开启完全一致；"
            "不保留任何记录，回读一律 never_seen", normalized)
    assert normalized["business_results_identical"] is True
    assert normalized["metrics_identical"] is True
    assert normalized["skipped_not_sampled"] == 8
    assert normalized["recorded"] == 0
    assert normalized["lookup_results"] == ["never_seen"]


def test_sampling_config_is_readable_from_api() -> None:
    pipeline = ReplayPipeline(store=ReplayStore(capacity=32), sampling=SamplingPolicy(True, 0.25))
    client = _client_with(pipeline)
    body = client.get("/replay").json()
    explain("采样配置对外可读", {"GET": "/replay"},
            "对外接口必须返回当前生效的 enabled/rate 与判定规则", body["sampling"])
    assert body["sampling"]["enabled"] is True
    assert body["sampling"]["rate"] == 0.25
    assert "sha256" in body["sampling"]["rule"]


def test_sampling_ratio_is_deterministic_per_trace_id() -> None:
    policy = SamplingPolicy(True, 0.5)
    ids = [f"{i:032x}" for i in range(200)]
    decisions = {tid: policy.should_keep(tid) for tid in ids}
    kept = sum(decisions.values())
    stable = all(policy.should_keep(tid) == keep for tid, keep in decisions.items())
    explain("比例采样确定性", {"rate": 0.5, "ids": 200},
            "同一 trace_id 判定稳定；命中数应大致符合比例（允许统计涨落）",
            {"kept": kept, "stable": stable})
    assert stable
    assert 50 < kept < 150  # 粗略比例校验，避免哈希分布导致的偶发失败


def test_lookup_distinguishes_never_seen_and_evicted() -> None:
    store = ReplayStore(capacity=3, tombstone_capacity=8)
    pipeline = ReplayPipeline(store=store, sampling=SamplingPolicy(True, 1.0))
    client = _client_with(pipeline)
    ids = [f"{i:032x}" for i in range(1, 6)]  # 5 条写入，容量 3 -> 前 2 条被淘汰

    for tid in ids:
        client.get("/work", headers={"X-Trace-Id": tid})

    evicted = client.get(f"/replay/{ids[0]}")  # 先查被淘汰的（查询自身也会占容量）
    found = client.get(f"/replay/{ids[4]}")
    never = client.get(f"/replay/{'f' * 32}")
    normalized = {"evicted": (evicted.status_code, evicted.json()["result"]),
                  "found": (found.status_code, found.json()["result"]),
                  "never_seen": (never.status_code, never.json()["result"])}
    explain("回读三态区分", {"capacity": 3, "written": 5},
            "最早写入的 2 条按 FIFO 淘汰 -> 410 evicted；仍在容量内 -> 200 found；"
            "从未写入 -> 404 never_seen", normalized)
    assert normalized["evicted"] == (410, "evicted")
    assert normalized["found"] == (200, "found")
    assert normalized["never_seen"] == (404, "never_seen")


def test_store_capacity_bound_and_fifo_eviction_counters() -> None:
    store = ReplayStore(capacity=4, tombstone_capacity=8)
    for i in range(10):
        store.put(_diag(f"{i:032x}"))

    survivors = [store.lookup(f"{i:032x}")[0] for i in range(6, 10)]
    evicted = [store.lookup(f"{i:032x}")[0] for i in range(0, 6)]
    stats = store.stats()
    normalized = {"size": stats["size"], "evicted": stats["evicted"],
                  "survivors": [s.value for s in survivors],
                  "evicted_lookups": [s.value for s in evicted],
                  "hits": stats["hits"], "misses_evicted": stats["misses_evicted"]}
    explain("容量上界与 FIFO 淘汰", {"capacity": 4, "written": 10},
            "记录数永不超过容量；最老的 6 条按写入先后被淘汰并留墓碑；"
            "recorded == hits + 留存 + 已淘汰", normalized)
    assert stats["size"] == 4 <= store.capacity
    assert stats["evicted"] == 6
    assert all(s is LookupStatus.FOUND for s in survivors)
    assert all(s is LookupStatus.EVICTED for s in evicted)
    assert stats["hits"] == 4 and stats["misses_evicted"] == 6
    # 守恒：写入 10 = 留存 4 + 淘汰 6
    assert stats["recorded"] == stats["size"] + stats["evicted"]


def test_record_failure_isolated_from_response_and_metrics() -> None:
    class FailingStore(ReplayStore):
        def put(self, diag: TraceDiagnostic) -> None:
            raise OSError("simulated retention backend down")

    metrics = MetricsRegistry()
    pipeline = ReplayPipeline(store=FailingStore(capacity=8), sampling=SamplingPolicy(True, 1.0))
    client = _client_with(pipeline, metrics)

    statuses = [client.get("/work", params={"ms": 1}).status_code for _ in range(10)]
    boom = client.get("/boom")
    snap = metrics.snapshot()
    normalized = {"statuses": statuses, "boom_status": boom.status_code,
                  "record_failures": pipeline.record_failures,
                  "total_requests": snap["total_requests"],
                  "total_errors": snap["total_errors"]}
    explain("保留环节失败隔离", {"requests": 10, "store": "put always raises"},
            "保留失败不得改变状态码/响应体，不得丢失或重复聚合计数；失败被计数",
            normalized)
    assert statuses == [200] * 10
    assert boom.status_code == 500
    assert pipeline.record_failures == 11  # 每个请求只尝试记录一次
    assert snap["total_requests"] == 11
    assert snap["total_errors"] == 1


def test_concurrent_record_and_lookup_conservation() -> None:
    pipeline = ReplayPipeline(store=ReplayStore(capacity=1024), sampling=SamplingPolicy(True, 1.0))
    client = _client_with(pipeline)
    ids = [f"{i:032x}" for i in range(1, 65)]

    def hit(tid: str) -> str:
        resp = client.get("/work", params={"ms": 1}, headers={"X-Trace-Id": tid})
        return resp.headers["x-trace-id"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        returned = list(pool.map(hit, ids))

    before = pipeline.store.stats()

    def read(tid: str) -> tuple[int, str]:
        r = client.get(f"/replay/{tid}")
        body = r.json()
        got = body["record"]["trace_id"] if body["result"] == "found" else ""
        return r.status_code, got

    with ThreadPoolExecutor(max_workers=16) as pool:
        lookups = list(pool.map(read, ids))

    after = pipeline.store.stats()
    mismatches = [got for code, got in lookups if code != 200 or got not in ids]
    normalized = {"recorded_after_writes": before["recorded"], "size": before["size"],
                  "evicted": before["evicted"], "hits_delta": after["hits"] - before["hits"],
                  "lookup_mismatches": len(mismatches)}
    explain("并发写入与回读守恒", {"writers": 64, "concurrency": 16},
            "每个请求只记录一次：recorded == 64 且无淘汰；64 次并发回读全部命中各自标识，"
            "hits 恰好增加 64，互不串扰", normalized)
    assert sorted(returned) == sorted(ids)
    assert before["recorded"] == 64 and before["size"] == 64 and before["evicted"] == 0
    assert mismatches == []
    assert after["hits"] - before["hits"] == 64
