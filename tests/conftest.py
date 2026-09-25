"""pytest 公共夹具：内存日志收集、独立可观测实例与 TestClient。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.jsonlog import JsonLogger
from app.main import create_app
from app.observability import Observability


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def obs(log_stream: io.StringIO) -> Observability:
    return Observability(logger=JsonLogger(log_stream))


@pytest.fixture
def client(obs: Observability) -> TestClient:
    app = create_app(obs)
    return TestClient(app, raise_server_exceptions=False)
