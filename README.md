# HTTP 链路追踪与运行时可观测

在 FastAPI 服务上提供请求级链路标识、结构化日志、指标聚合与错误归一化能力，
保证同一请求在并发、异常与写入失败等情形下得到一致且可解释的诊断结论。

## 架构

```
请求 -> ObservabilityMiddleware
          ├─ tracing.resolve_trace_id()   解析/生成链路标识，写入 contextvars
          ├─ errors.classify()            异常归一化为对外安全视图
          ├─ logging.StructuredLogger     单行 JSON 结构化日志（写入失败静默降级）
          └─ metrics.MetricsRegistry      (route, status_class) 维度聚合（维度有硬上界）
```

| 模块 | 职责 |
| --- | --- |
| `app/tracing.py` | 链路标识规则、生成与 `contextvars` 上下文传播 |
| `app/errors.py` | 错误类别枚举与异常归一化 |
| `app/logging.py` | 结构化日志与失败安全 sink |
| `app/metrics.py` | 有界维度指标聚合与兜底分桶 |
| `app/middleware.py` | 串联以上能力的请求中间件 |
| `app/service.py` | 应用装配、路由与异常处理器 |

## 链路标识规则

- 请求头：`X-Trace-Id`。
- **合法标识**：8~64 个十六进制字符（大小写均可），且不全为 `0`。
- 处理结果通过响应头 `X-Trace-Id-Source` 给出可区分的原因：
  - `external`：外部传入且合法，归一化为小写后**沿用**；
  - `generated_missing`：头部缺失，服务端生成 32 位小写十六进制标识；
  - `generated_invalid`：传入非法（非十六进制 / 长度越界 / 全 0），**替换**为新标识。
- 上下文经 `contextvars` 传播，并发请求之间互不串扰；响应头始终回写 `X-Trace-Id`。

## 结构化日志

每条日志为单行 JSON，统一字段：`ts / level / event / trace_id`，
请求结束事件（`request_end`）附加 `route / method / status / latency_ms / trace_id_source`；
异常事件（`request_error`）附加 `error_category / error_type / error_detail`（内部细节只进日志）。

## 错误分类

所有异常归一化为四类，响应体固定为
`{"error": {"category", "message", "trace_id"}}`，内部细节（堆栈、连接串、密码等）不透出：

| category | 含义 | 默认状态码 |
| --- | --- | --- |
| `validation` | 请求参数不合法 | 422 |
| `not_found` | 资源/路由不存在 | 404 |
| `dependency` | 下游依赖失败 | 502 |
| `internal` | 未预期内部错误 | 500 |

## 退化与上限策略

- **日志写入失败**：sink 抛错被吞掉并累计 `logger.write_failures`，绝不影响请求返回；
  指标聚合同样独立，日志与指标任一失败都不阻塞请求路径。
- **指标维度上界**：维度为 `(route, status_class)`，`status_class` 仅 `1xx~5xx` 五种；
  路由维度数达到 `max_dimensions`（默认 64）后，新维度统一计入稳定的
  `(__overflow__, status_class)` 兜底分桶并累计 `dropped_dimensions`，
  内存占用上界为 `max_dimensions + 5` 个桶，杜绝高基数标签造成的无界增长。
- 聚合结果通过 `GET /metrics` 读取，含 `active_dimensions / dropped_dimensions /
  total_requests / total_errors / routes{count,error_count,avg_latency_ms,max_latency_ms}`。

## 运行与验证

```bash
uv sync                 # 安装依赖
uv run python main.py   # 启动服务（127.0.0.1，随机端口）
uv run pytest -q        # 运行全部测试
uv run pytest -q -s     # 查看每个用例打印的输入 / 判定依据 / 归一化结果
```

手工验证示例：

```bash
curl -i -H 'X-Trace-Id: abcdef0123456789' http://127.0.0.1:PORT/work   # 沿用外部标识
curl -i -H 'X-Trace-Id: bad!!' http://127.0.0.1:PORT/                  # generated_invalid
curl -i http://127.0.0.1:PORT/boom                                     # internal，细节不透出
curl -s http://127.0.0.1:PORT/metrics | jq                             # 读取聚合指标
```

## 测试覆盖（`tests/`）

- `test_tracing.py`：合法标识沿用、缺失/非法标识的可区分替换、32 并发请求上下文隔离；
- `test_errors.py`：四类错误归一化、内部错误细节不外泄且与日志同一 `trace_id`；
- `test_degradation.py`：日志 sink 持续失败时请求与指标不受影响、
  高基数路径触发兜底分桶且维度有界、并发聚合计数守恒、日志字段完整性。
