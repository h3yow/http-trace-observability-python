"""可观测门面：把一次请求的结构化日志与指标聚合串起来。

* 日志后端或指标后端的写入失败都被隔离：门面方法不抛异常，只累加退化计数；
* 提供测试用故障注入开关（``fail_log_writes`` / ``fail_metric_writes``）；
* ``stats`` 统一对外暴露聚合结果、有界上界与退化计数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .jsonlog import JsonLogger
from .metrics import MetricsRegistry, status_bucket


@dataclass(slots=True)
class FailureFlags:
    """测试用故障注入开关。"""

    log_fail: bool = False
    metrics_fail: bool = False


class Observability:
    """日志 + 指标的统一入口，任一后端失败都被吞掉并计数。"""

    def __init__(
        self,
        logger: JsonLogger | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.logger = logger if logger is not None else JsonLogger()
        self.metrics = metrics if metrics is not None else MetricsRegistry()
        self.failure = FailureFlags()

    # ---- 故障注入 -------------------------------------------------------
    def fail_log_writes(self) -> None:
        """让后续日志写入全部失败（模拟日志后端故障）。"""
        self.failure.log_fail = True
        self.logger.force_fail = True

    def fail_metric_writes(self) -> None:
        """让后续指标写入全部失败（模拟指标后端故障）。"""
        self.failure.metrics_fail = True
        self.metrics.force_fail = True

    # ---- 写入 -----------------------------------------------------------
    def event(self, event: str, **fields: Any) -> None:
        """记录一条结构化事件；失败不外抛。"""
        self.logger.log(event, **fields)

    def record(
        self,
        phase: str,
        result: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        """记录一次请求阶段指标；失败不外抛。"""
        self.metrics.observe(phase, result, status_bucket(status_code), duration_ms)

    # ---- 读取 -----------------------------------------------------------
    def degraded(self) -> bool:
        """是否处于退化状态（任一后端发生过写入失败）。"""
        return self.log_write_failures > 0 or self.metric_write_failures > 0

    @property
    def log_write_failures(self) -> int:
        return self.logger.failed_writes

    @property
    def metric_write_failures(self) -> int:
        return self.metrics.failed_writes

    def stats(self) -> dict[str, Any]:
        """导出聚合指标与退化计数。"""
        snapshot = self.metrics.snapshot()
        snapshot["degradation"] = {
            "log_write_failures": self.log_write_failures,
            "metric_write_failures": self.metric_write_failures,
            "degraded": self.degraded(),
        }
        return snapshot

    def reset(self) -> None:
        """重置指标与故障注入状态（日志失败计数保留在 logger 内，按需单独重置）。"""
        self.metrics.reset()
        self.metrics.force_fail = False
        self.logger.force_fail = False
        self.failure = FailureFlags()
