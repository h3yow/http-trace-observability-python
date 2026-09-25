"""指标聚合：有界维度、稳定兜底分桶、写入失败隔离。

维度固定为三元组 ``(phase, result, status_bucket)``，且每个维度都有**确定上界**：

* ``phase``：仅接受预登记集合（``request``/``http``），其余落入 ``other``；
* ``result``：仅接受 ``success``/``validation``/``not_found``/``unavailable``/
  ``upstream_failure``/``timeout``/``error``，其余落入 ``other``；
* ``status_bucket``：HTTP 状态码按区间归入 ``2xx/3xx/4xx/5xx/xxx``，不保留原始码。

因此 key 数量上界为
``(len(phase)+1) * (len(result)+1) * 5``，与标签取值的基数无关；
高基数标签（如任意 request_id、任意状态码）永远不会造成无界增长。

为避免延迟样本无界累积，耗时只保留 ``count/sum/min/max`` 滚动聚合。
"""
from __future__ import annotations

import threading
from typing import Any

OTHER = "other"

_ALLOWED_PHASE = frozenset({"request", "http"})
_ALLOWED_RESULT = frozenset(
    {
        "success",
        "validation",
        "not_found",
        "unavailable",
        "upstream_failure",
        "timeout",
        "error",
    }
)
_STATUS_BUCKETS = ("2xx", "3xx", "4xx", "5xx", "xxx")
MAX_CATEGORY_CARDINALITY = len(_ALLOWED_RESULT) + 1
MAX_RESULT_CARDINALITY = MAX_CATEGORY_CARDINALITY
MAX_STATUS_CARDINALITY = len(_STATUS_BUCKETS)
# 聚合 key 总数的确定上界
MAX_SERIES = (len(_ALLOWED_PHASE) + 1) * MAX_CATEGORY_CARDINALITY * MAX_STATUS_CARDINALITY


def status_bucket(status_code: int) -> str:
    """把任意 HTTP 状态码归入稳定的区间桶。"""
    if 200 <= status_code < 300:
        return "2xx"
    if 300 <= status_code < 400:
        return "3xx"
    if 400 <= status_code < 500:
        return "4xx"
    if 500 <= status_code < 600:
        return "5xx"
    return "xxx"


def _bounded(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else OTHER


class MetricsRegistry:
    """有界、线程安全的内存指标聚合器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str, str], int] = {}
        self._latency_count: dict[tuple[str, str, str], int] = {}
        self._latency_sum: dict[tuple[str, str, str], float] = {}
        self._latency_min: dict[tuple[str, str, str], float] = {}
        self._latency_max: dict[tuple[str, str, str], float] = {}
        self._failed_writes = 0
        # 测试用：置 True 后所有写入模拟失败
        self.force_fail = False

    @property
    def failed_writes(self) -> int:
        """累计写入失败次数。"""
        return self._failed_writes

    def observe(
        self,
        phase: str,
        result: str,
        status_bucket: str,
        duration_ms: float | None = None,
    ) -> None:
        """记录一次观测。所有标签值都会先做有界化，失败只计数不外抛。"""
        try:
            if self.force_fail:
                raise RuntimeError("simulated metrics sink failure")
            key = (
                _bounded(phase, _ALLOWED_PHASE),
                _bounded(result, _ALLOWED_RESULT),
                status_bucket
                if status_bucket in _STATUS_BUCKETS
                else "xxx",
            )
            with self._lock:
                self._counters[key] = self._counters.get(key, 0) + 1
                if duration_ms is not None:
                    count = self._latency_count.get(key, 0) + 1
                    self._latency_count[key] = count
                    self._latency_sum[key] = self._latency_sum.get(key, 0.0) + duration_ms
                    if key not in self._latency_min or duration_ms < self._latency_min[key]:
                        self._latency_min[key] = duration_ms
                    if key not in self._latency_max or duration_ms > self._latency_max[key]:
                        self._latency_max[key] = duration_ms
        except Exception:
            with self._lock:
                self._failed_writes += 1

    def snapshot(self) -> dict[str, Any]:
        """导出当前聚合结果（拷贝，调用方可直接序列化）。"""
        with self._lock:
            series = []
            for key, count in sorted(self._counters.items()):
                n = self._latency_count.get(key, 0)
                item: dict[str, Any] = {
                    "phase": key[0],
                    "result": key[1],
                    "status": key[2],
                    "count": count,
                }
                if n:
                    total = self._latency_sum[key]
                    item["latency_ms"] = {
                        "count": n,
                        "sum": round(total, 3),
                        "min": round(self._latency_min[key], 3),
                        "max": round(self._latency_max[key], 3),
                        "avg": round(total / n, 3),
                    }
                series.append(item)
            return {
                "bounds": {
                    "max_series": MAX_SERIES,
                    "phases": sorted(_ALLOWED_PHASE) + [OTHER],
                    "results": sorted(_ALLOWED_RESULT) + [OTHER],
                    "status_buckets": list(_STATUS_BUCKETS),
                },
                "series_count": len(series),
                "series": series,
            }

    def reset(self) -> None:
        """清空聚合状态与失败计数。"""
        with self._lock:
            self._counters.clear()
            self._latency_count.clear()
            self._latency_sum.clear()
            self._latency_min.clear()
            self._latency_max.clear()
            self._failed_writes = 0
