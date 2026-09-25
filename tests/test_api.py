"""端到端 API 测试：/metrics 聚合读取与结构化日志字段。"""
from __future__ import annotations

import json


def test_metrics_endpoint_aggregates_results(client):
    client.get("/", headers={"X-Request-ID": "api-ok-0000001"})
    client.get("/items/1", headers={"X-Request-ID": "api-ok-0000002"})
    client.get("/items/404", headers={"X-Request-ID": "api-nf-0000001"})
    client.get("/boom", headers={"X-Request-ID": "api-err-00001"})

    stats = client.get("/metrics").json()
    table = {
        f"{s['phase']}|{s['result']}|{s['status']}": s["count"] for s in stats["series"]
    }
    print(
        f"\n[api] 聚合表={json.dumps(table, ensure_ascii=False)} "
        f"上界={stats['bounds']['max_series']} 实际序列={stats['series_count']}"
    )
    assert table["request|success|2xx"] == 2
    assert table["request|not_found|4xx"] == 1
    assert table["request|error|5xx"] == 1
    assert stats["series_count"] <= stats["bounds"]["max_series"]
    # 耗时统计存在且数值确定
    success = next(
        s for s in stats["series"]
        if (s["phase"], s["result"], s["status"]) == ("request", "success", "2xx")
    )
    assert success["latency_ms"]["count"] == 2
    assert success["latency_ms"]["avg"] >= 0.0


def test_structured_logs_have_unified_fields(client, log_stream):
    client.get("/items/404", headers={"X-Request-ID": "api-log-000001"})
    client.get("/boom", headers={"X-Request-ID": "api-log-000002"})
    records = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    events = {(r["event"], r["request_id"]): r for r in records}
    print(f"\n[api] 结构化日志事件={sorted({r['event'] for r in records})}")

    not_found_complete = events[("request.complete", "api-log-000001")]
    print(
        f"[api] 404 complete: status_code={not_found_complete['status_code']} "
        f"result={not_found_complete['result']} "
        f"duration_ms={not_found_complete['duration_ms']} "
        f"path={not_found_complete['path']}"
    )
    assert not_found_complete["status_code"] == 404
    assert not_found_complete["result"] == "not_found"
    assert isinstance(not_found_complete["duration_ms"], (int, float))

    # 未受控异常必须产生 error 事件，且只含归一化类别，不含内部细节
    error_event = events[("request.error", "api-log-000002")]
    print(f"[api] 500 error 事件字段={json.dumps(error_event, ensure_ascii=False)}")
    assert error_event["category"] == "internal"
    assert error_event["error_type"] == "RuntimeError"
    assert "AKIA" not in json.dumps(error_event)
    boom_complete = events[("request.complete", "api-log-000002")]
    assert boom_complete["status_code"] == 500
    assert boom_complete["result"] == "error"

    # 统一字段：所有记录都具备 ts/level/request_id
    for rec in records:
        assert "ts" in rec and rec["level"] == "info" and "request_id" in rec


def test_healthz_echoes_request_id(client):
    resp = client.get("/healthz", headers={"X-Request-ID": "api-health-001"})
    print(f"\n[api] /healthz -> {resp.json()}")
    assert resp.status_code == 200
    assert resp.json()["request_id"] == "api-health-001"
