"""写入失败退化测试：日志/指标后端故障不得影响请求返回。"""
from __future__ import annotations

import io

from app.jsonlog import JsonLogger
from app.main import create_app
from app.observability import Observability
from fastapi.testclient import TestClient


def _client(fail_log: bool = False, fail_metrics: bool = False):
    obs = Observability(logger=JsonLogger(io.StringIO()))
    if fail_log:
        obs.fail_log_writes()
    if fail_metrics:
        obs.fail_metric_writes()
    return TestClient(create_app(obs), raise_server_exceptions=False), obs


def test_log_write_failure_does_not_break_request():
    client, obs = _client(fail_log=True)
    resp = client.get("/", headers={"X-Request-ID": "fail-log-0001"})
    print(
        f"\n[failure] 日志后端故障下 status={resp.status_code} "
        f"body={resp.json()} 日志失败计数={obs.log_write_failures}"
    )
    assert resp.status_code == 200
    assert resp.json()["request_id"] == "fail-log-0001"
    assert obs.log_write_failures >= 2  # start + complete 两条均失败
    assert obs.degraded() is True


def test_metrics_write_failure_does_not_break_request():
    client, obs = _client(fail_metrics=True)
    resp = client.get("/items/404", headers={"X-Request-ID": "fail-metric-0001"})
    print(
        f"\n[failure] 指标后端故障下 status={resp.status_code} "
        f"body={resp.json()} 指标失败计数={obs.metric_write_failures}"
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"
    assert obs.metric_write_failures >= 1
    assert obs.degraded() is True


def test_both_backends_failed_request_still_returns():
    client, obs = _client(fail_log=True, fail_metrics=True)
    for rid, path, expected in (
        ("both-ok-0001", "/", 200),
        ("both-404-001", "/items/404", 404),
        ("both-500-001", "/boom", 500),
    ):
        resp = client.get(path, headers={"X-Request-ID": rid})
        print(f"[failure] 双后端故障 path={path} -> status={resp.status_code}")
        assert resp.status_code == expected
    stats = client.get("/metrics").json()
    print(
        f"[failure] /metrics 仍可读: degraded={stats['degradation']} "
        f"series={stats['series_count']}"
    )
    assert stats["degradation"]["degraded"] is True
    assert stats["degradation"]["log_write_failures"] > 0
    assert stats["degradation"]["metric_write_failures"] > 0


def test_error_response_identical_when_backends_fail():
    """后端故障与正常情况下，对外错误结论必须一致（可复现）。"""
    good_client, _ = _client()
    bad_client, _ = _client(fail_log=True, fail_metrics=True)
    a = good_client.get("/boom", headers={"X-Request-ID": "stable-err-001"}).json()
    b = bad_client.get("/boom", headers={"X-Request-ID": "stable-err-001"}).json()
    # request_id 相同，其余结论应逐字一致
    print(f"\n[failure] 正常={a} 退化={b}")
    assert a == b
