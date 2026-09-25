"""结构化日志与失败安全写入。

- 每条日志为单行 JSON，字段统一：ts/event/trace_id/level + 业务字段。
- sink 写入失败时静默降级：累计 write_failures，绝不向请求路径抛错。
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.tracing import get_current


class LogSink(Protocol):
    """日志落盘/输出目标。"""

    def write(self, line: str) -> None: ...


class _StdoutSink:
    def write(self, line: str) -> None:
        print(line, file=sys.stdout, flush=True)


def new_stdout_sink() -> LogSink:
    """构造标准输出 sink。"""
    return _StdoutSink()


def _safe_value(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


@dataclass
class StructuredLogger:
    """按统一字段输出结构化日志；写入失败静默降级，不影响请求。"""

    sink: LogSink
    write_failures: int = field(default=0, init=False)

    def emit(self, event: str, level: str = "info", **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event": event,
        }
        ctx = get_current()
        record["trace_id"] = ctx.trace_id if ctx else None
        for key, value in fields.items():
            record[key] = _safe_value(value)
        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):  # 极端兜底：不应发生
            line = json.dumps({"event": event, "level": "error", "error": "encode_failed"})
        try:
            self.sink.write(line)
        except Exception:
            # 写入失败不得影响请求；仅计数并尝试向 stderr 留痕
            self.write_failures += 1
            try:
                traceback.print_exc(file=sys.stderr)
            except Exception:
                pass
