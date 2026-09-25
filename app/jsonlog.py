"""结构化 JSON 日志：行分隔 JSON，写入失败时静默降级，绝不影响请求。

统一字段：``ts``（ISO 时间）、``level``、``event``、``request_id``（缺省为
``req-unbound``），其余键值由调用方追加。日志器本身不抛异常；写入失败只累计
``failed_writes`` 计数，供退化观测。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from .context import current_request_id


class JsonLogger:
    """行分隔 JSON 日志器。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._failed_writes = 0
        # 测试用：置 True 后所有写入都模拟失败
        self.force_fail = False

    @property
    def failed_writes(self) -> int:
        """累计写入失败次数。"""
        return self._failed_writes

    def _build_record(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": fields.pop("level", "info"),
            "event": event,
            "request_id": fields.pop("request_id", current_request_id()),
        }
        record.update(fields)
        return record

    def log(self, event: str, **fields: Any) -> None:
        """输出一条结构化日志；任何写入失败都被吞掉并计数。"""
        try:
            if self.force_fail:
                raise OSError("simulated log sink failure")
            record = self._build_record(event, dict(fields))
            line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            # 日志失败不得影响业务请求；只记录退化次数
            self._failed_writes += 1
