"""按链路回读的诊断时间线存储。

- 记录以 trace_id 为键，容量有确定上界（RetentionPolicy.max_records）。
- 达到上界后按固定规则淘汰：**插入顺序最旧者优先（FIFO）**。
- 被淘汰的 trace_id 进入有界墓碑区，查询可区分：
    - found：记录仍在，可回读完整时间线；
    - evicted：记录曾经存在但已被淘汰（在墓碑窗口内）；
    - never_seen：该标识从未出现（或已旧到连墓碑也被淘汰，见 README 窗口说明）。
- 命中 / 淘汰 / 未命中均以计数暴露；全部操作在单锁下完成，并发不串扰。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LookupStatus(str, Enum):
    """回读结果状态。"""

    FOUND = "found"
    EVICTED = "evicted"
    NEVER_SEEN = "never_seen"


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略：记录数上界与墓碑（淘汰痕迹）容量。"""

    max_records: int = 1024
    tombstone_capacity: int | None = None  # None 时取 max_records

    def __post_init__(self) -> None:
        if self.max_records < 1:
            raise ValueError("max_records must be >= 1")
        if self.tombstone_capacity is not None and self.tombstone_capacity < 1:
            raise ValueError("tombstone_capacity must be >= 1")

    @property
    def effective_tombstone_capacity(self) -> int:
        return self.tombstone_capacity if self.tombstone_capacity is not None else self.max_records


@dataclass(frozen=True)
class TraceStage:
    """时间线上的一个关键阶段。

    offset_ms：相对请求开始的偏移；duration_ms：距上一阶段的耗时。
    """

    name: str
    offset_ms: float
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "offset_ms": round(self.offset_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass(frozen=True)
class TraceRecord:
    """单请求诊断时间线。"""

    trace_id: str
    source: str  # 标识来源：external / generated_missing / generated_invalid
    route: str
    method: str
    status: int
    latency_ms: float
    error_category: str | None
    stages: tuple[TraceStage, ...]
    finished_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "source": self.source,
            "route": self.route,
            "method": self.method,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 3),
            "error_category": self.error_category,
            "stages": [s.as_dict() for s in self.stages],
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class LookupResult:
    """回读结果：状态 + 命中的记录（未命中为 None）。"""

    status: LookupStatus
    record: TraceRecord | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TimelineStore:
    """有界时间线存储：FIFO 淘汰 + 有界墓碑 + 可读计数。

    所有公共方法在单锁下完成；put/get 各自原子，并发请求互不串扰。
    """

    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self._policy = policy or RetentionPolicy()
        self._records: OrderedDict[str, TraceRecord] = OrderedDict()
        self._tombstones: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self.puts = 0
        self.evictions = 0
        self.hits = 0
        self.misses_evicted = 0
        self.misses_never_seen = 0
        self.write_failures = 0

    def put(self, record: TraceRecord) -> None:
        """写入一条记录；超容量时按 FIFO 淘汰最旧记录并留墓碑。"""
        with self._lock:
            self.puts += 1
            if record.trace_id in self._records:
                # 同一标识重复写入：覆盖并视为最新（每请求只写一次，正常不发生）
                del self._records[record.trace_id]
            self._records[record.trace_id] = record
            self._tombstones.pop(record.trace_id, None)  # 复活：不再是“已淘汰”
            while len(self._records) > self._policy.max_records:
                evicted_id, _ = self._records.popitem(last=False)  # FIFO：最旧优先
                self._tombstones[evicted_id] = None
                self.evictions += 1
            while len(self._tombstones) > self._policy.effective_tombstone_capacity:
                self._tombstones.popitem(last=False)

    def get(self, trace_id: str) -> LookupResult:
        """按链路标识回读；三种状态分别计数，互斥不串扰。"""
        with self._lock:
            record = self._records.get(trace_id)
            if record is not None:
                self.hits += 1
                return LookupResult(status=LookupStatus.FOUND, record=record)
            if trace_id in self._tombstones:
                self.misses_evicted += 1
                return LookupResult(status=LookupStatus.EVICTED)
            self.misses_never_seen += 1
            return LookupResult(status=LookupStatus.NEVER_SEEN)

    def note_write_failure(self) -> None:
        """记录一次保留环节失败（由调用方在捕获异常后调用）。"""
        with self._lock:
            self.write_failures += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_records": self._policy.max_records,
                "tombstone_capacity": self._policy.effective_tombstone_capacity,
                "stored_records": len(self._records),
                "stored_tombstones": len(self._tombstones),
                "puts": self.puts,
                "evictions": self.evictions,
                "hits": self.hits,
                "misses_evicted": self.misses_evicted,
                "misses_never_seen": self.misses_never_seen,
                "write_failures": self.write_failures,
            }
