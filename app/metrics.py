"""指标聚合：维度有确定上界，超限进入稳定兜底分桶。

- 维度为 (route, status_class)，status_class 形如 "2xx"/"4xx"，天然低基数。
- route 维度数量达到 max_dimensions 后，新维度不再建桶，
  统一计入稳定的 (OVERFLOW_BUCKET, status_class) 兜底分桶，并累计 dropped_dimensions。
- 因此内存占用有确定上界：max_dimensions + 5（兜底桶按 status_class 最多 5 个）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


OVERFLOW_BUCKET = "__overflow__"


@dataclass
class RouteMetrics:
    """单维度聚合结果。"""

    count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0

    def add(self, latency_ms: float, *, error: bool) -> None:
        self.count += 1
        if error:
            self.error_count += 1
        self.total_latency_ms += latency_ms
        if latency_ms > self.max_latency_ms:
            self.max_latency_ms = latency_ms

    def as_dict(self) -> dict[str, Any]:
        avg = self.total_latency_ms / self.count if self.count else 0.0
        return {
            "count": self.count,
            "error_count": self.error_count,
            "avg_latency_ms": round(avg, 3),
            "max_latency_ms": round(self.max_latency_ms, 3),
        }


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


@dataclass
class MetricsRegistry:
    """按 (route, status_class) 维度聚合，维度数有硬上界。"""

    max_dimensions: int = 64
    _buckets: dict[tuple[str, str], RouteMetrics] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    dropped_dimensions: int = field(default=0, init=False)

    def record(self, route: str, status_code: int, latency_ms: float, *, error: bool) -> None:
        key = (route, _status_class(status_code))
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_dimensions:
                    # 高基数退化：进入稳定兜底分桶
                    self.dropped_dimensions += 1
                    key = (OVERFLOW_BUCKET, key[1])
                    bucket = self._buckets.setdefault(key, RouteMetrics())
                else:
                    bucket = RouteMetrics()
                    self._buckets[key] = bucket
            bucket.add(latency_ms, error=error)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            routes = {
                f"{route}|{cls}": bucket.as_dict()
                for (route, cls), bucket in sorted(self._buckets.items())
            }
            total = sum(b.count for b in self._buckets.values())
            errors = sum(b.error_count for b in self._buckets.values())
            return {
                "max_dimensions": self.max_dimensions,
                "active_dimensions": len(self._buckets),
                "dropped_dimensions": self.dropped_dimensions,
                "total_requests": total,
                "total_errors": errors,
                "routes": routes,
            }
