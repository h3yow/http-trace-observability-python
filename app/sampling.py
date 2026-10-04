"""诊断记录采样配置与判定。

- enabled 为总开关：关闭时任何链路都不保留可回读记录。
- ratio 为 [0,1] 比例：按 trace_id 做确定性判定，
  同一 trace_id 的判定结果稳定，便于测试与跨环境复现。
- 采样只影响诊断记录是否保留，不影响状态码 / 响应头 / 响应体 / 聚合指标。
"""
from __future__ import annotations

from dataclasses import dataclass

_BUCKET_COUNT = 1 << 32  # 判定 空间：取 trace_id 前 8 位十六进制映射到 [0, 2^32)


@dataclass(frozen=True)
class SamplingConfig:
    """采样配置：enabled 为总开关，ratio 为保留比例。"""

    enabled: bool = True
    ratio: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError(f"ratio must be in [0, 1], got {self.ratio}")

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "ratio": self.ratio}


def should_sample(trace_id: str, config: SamplingConfig) -> bool:
    """判定某链路是否保留可回读记录（确定性：同一 trace_id 结果稳定）。"""
    if not config.enabled or config.ratio <= 0.0:
        return False
    if config.ratio >= 1.0:
        return True
    bucket = int(trace_id[:8], 16)  # trace_id 必为合法十六进制（上游已归一化）
    return bucket < config.ratio * _BUCKET_COUNT
