"""测试公共设施：内存 sink、应用工厂、判定打印助手。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.logging import StructuredLogger
from app.metrics import MetricsRegistry
from app.service import create_app


class ListSink:
    """内存日志 sink，按行收集 JSON 日志。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]


class FailingSink:
    """写入永远失败的 sink，用于验证降级。"""

    def __init__(self) -> None:
        self.attempts = 0

    def write(self, line: str) -> None:
        self.attempts += 1
        raise OSError("simulated disk full")


@pytest.fixture()
def sink() -> ListSink:
    return ListSink()


@pytest.fixture()
def logger(sink: ListSink) -> StructuredLogger:
    return StructuredLogger(sink=sink)


@pytest.fixture()
def metrics() -> MetricsRegistry:
    return MetricsRegistry(max_dimensions=8)


@pytest.fixture()
def client(logger: StructuredLogger, metrics: MetricsRegistry) -> TestClient:
    return TestClient(create_app(logger=logger, metrics=metrics))


def explain(case: str, inputs: dict, basis: str, normalized: dict) -> None:
    """按任务要求打印：输入、判定依据与归一化后的结果。"""
    print(f"\n[case] {case}")
    print(f"[input] {json.dumps(inputs, ensure_ascii=False, default=str)}")
    print(f"[basis] {basis}")
    print(f"[normalized] {json.dumps(normalized, ensure_ascii=False, default=str)}")
