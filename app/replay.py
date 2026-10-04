"""按链路回读：诊断记录、采样策略与有界保留。

- 采样：SamplingPolicy 按开关 + 比例决定哪些链路保留可回读记录；
  判定对 trace_id 做确定性哈希，同一标识结果稳定、可解释、可复现。
- 保留：ReplayStore 容量有确定上界，写满后按 FIFO（最早写入先淘汰）
  这一固定规则淘汰；被淘汰的标识进入有界墓碑队列，用于区分
  “从未出现”与“已被淘汰”。墓碑同样上界，超界后最老墓碑被丢弃，
  对应标识退化为 never_seen（文档中说明）。
- 并发：所有读写共用一把锁，单请求只计一次，命中/淘汰判断不串扰。
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LookupStatus(str, Enum):
    """回读结果状态：命中 / 从未出现 / 已被淘汰。"""

    FOUND = "found"
    NEVER_SEEN = "never_seen"
    EVICTED = "evicted"


@dataclass(frozen=True)
class SamplingPolicy:
    """采样策略：开关 + 比例，决定哪些链路保留可回读记录。

    - enabled=False：一律不保留；
    - rate<=0：不保留；rate>=1：全量保留；
    - 其余按 trace_id 的 SHA-256 前 8 字节归一化到 [0,1) 与 rate 比较，
      同一 trace_id 判定结果稳定，便于解释与复现。
    """

    enabled: bool = True
    rate: float = 1.0

    def should_keep(self, trace_id: str) -> bool:
        """判定某条链路是否保留诊断记录。"""
        if not self.enabled or self.rate <= 0.0:
            return False
        if self.rate >= 1.0:
            return True
        digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return bucket < self.rate

    def as_dict(self) -> dict[str, Any]:
        """对外可读的当前生效配置。"""
        return {
            "enabled": self.enabled,
            "rate": self.rate,
            "rule": (
                "disabled"
                if not self.enabled
                else "keep_all"
                if self.rate >= 1.0
                else "drop_all"
                if self.rate <= 0.0
                else "sha256(trace_id)[:8]/2^64 < rate"
            ),
        }


@dataclass(frozen=True)
class StageRecord:
    """诊断时间线上的一个关键阶段。"""

    name: str
    offset_ms: float  # 相对请求开始的偏移
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "offset_ms": round(self.offset_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass(frozen=True)
class TraceDiagnostic:
    """单请求的诊断时间线记录。"""

    trace_id: str
    trace_id_source: str  # external / generated_missing / generated_invalid
    stages: tuple[StageRecord, ...]
    status: int
    error_category: str | None
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        """归一化为对外可读字典。"""
        return {
            "trace_id": self.trace_id,
            "trace_id_source": self.trace_id_source,
            "stages": [s.as_dict() for s in self.stages],
            "status": self.status,
            "error_category": self.error_category,
            "latency_ms": round(self.latency_ms, 3),
        }


@dataclass
class ReplayStore:
    """有界诊断记录仓库：容量上界 + FIFO 淘汰 + 有界墓碑 + 可读计数。

    内存上界：capacity 条记录 + tombstone_capacity 个墓碑，长期运行不无界增长。
    """

    capacity: int = 1024
    tombstone_capacity: int = 1024
    _records: OrderedDict[str, TraceDiagnostic] = field(default_factory=OrderedDict, init=False)
    _tombstones: deque[str] = field(default_factory=deque, init=False)
    _tombstone_set: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    recorded: int = field(default=0, init=False)
    evicted: int = field(default=0, init=False)
    hits: int = field(default=0, init=False)
    misses_never_seen: int = field(default=0, init=False)
    misses_evicted: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be >= 1")
        if self.tombstone_capacity < 0:
            raise ValueError("tombstone_capacity must be >= 0")

    def put(self, diag: TraceDiagnostic) -> None:
        """写入一条诊断记录；同标识重复写入视为更新并移至最新位置。"""
        with self._lock:
            if diag.trace_id in self._records:
                del self._records[diag.trace_id]
            self._records[diag.trace_id] = diag
            self.recorded += 1
            self._tombstone_set.discard(diag.trace_id)
            while len(self._records) > self.capacity:
                old_id, _ = self._records.popitem(last=False)  # FIFO：最早写入先淘汰
                self.evicted += 1
                if self.tombstone_capacity > 0:
                    while len(self._tombstones) >= self.tombstone_capacity:
                        self._tombstone_set.discard(self._tombstones.popleft())
                    self._tombstones.append(old_id)
                    self._tombstone_set.add(old_id)

    def lookup(self, trace_id: str) -> tuple[LookupStatus, TraceDiagnostic | None]:
        """按链路标识回读，区分命中 / 从未出现 / 已被淘汰。"""
        with self._lock:
            diag = self._records.get(trace_id)
            if diag is not None:
                self.hits += 1
                return LookupStatus.FOUND, diag
            if trace_id in self._tombstone_set:
                self.misses_evicted += 1
                return LookupStatus.EVICTED, None
            self.misses_never_seen += 1
            return LookupStatus.NEVER_SEEN, None

    def stats(self) -> dict[str, Any]:
        """暴露容量与命中/淘汰计数。"""
        with self._lock:
            return {
                "capacity": self.capacity,
                "tombstone_capacity": self.tombstone_capacity,
                "size": len(self._records),
                "tombstones": len(self._tombstones),
                "recorded": self.recorded,
                "evicted": self.evicted,
                "hits": self.hits,
                "misses_never_seen": self.misses_never_seen,
                "misses_evicted": self.misses_evicted,
            }


@dataclass
class ReplayPipeline:
    """采样判定 +  guarded 写入 + 计数的共享入口。

    中间件只调用 record()：未采样直接跳过计数；写入抛错被吞掉并累计
    record_failures，绝不影响请求路径与既有聚合。
    """

    store: ReplayStore
    sampling: SamplingPolicy = field(default_factory=SamplingPolicy)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    record_failures: int = field(default=0, init=False)
    skipped_not_sampled: int = field(default=0, init=False)

    def record(self, diag: TraceDiagnostic) -> None:
        """按采样策略决定是否保留；保留失败静默降级并计数。"""
        if not self.sampling.should_keep(diag.trace_id):
            with self._lock:
                self.skipped_not_sampled += 1
            return
        try:
            self.store.put(diag)
        except Exception:
            with self._lock:
                self.record_failures += 1

    def stats(self) -> dict[str, Any]:
        """合并采样配置、保留容量与全部计数，供对外接口读取。"""
        with self._lock:
            record_failures = self.record_failures
            skipped = self.skipped_not_sampled
        return {
            "sampling": self.sampling.as_dict(),
            "retention": self.store.stats(),
            "record_failures": record_failures,
            "skipped_not_sampled": skipped,
        }
