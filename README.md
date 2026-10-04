# HTTP 链路追踪与运行时可观测

在 FastAPI 服务上提供请求级链路标识、结构化日志、指标聚合与错误归一化能力，
保证同一请求在并发、异常与写入失败等情形下得到一致且可解释的诊断结论。

## 架构

```
请求 -> ObservabilityMiddleware
          ├─ tracing.resolve_trace_id()   解析/生成链路标识，写入 contextvars
          ├─ errors.classify()            异常归一化为对外安全视图
          ├─ logging.StructuredLogger     单行 JSON 结构化日志（写入失败静默降级）
          ├─ metrics.MetricsRegistry      (route, status_class) 维度聚合（维度有硬上界）
          ├─ sampling.should_sample()     按配置判定是否保留可回读记录（确定性）
          └─ timeline.TimelineStore       有界诊断时间线存储（FIFO 淘汰 + 墓碑区分）
```

| 模块 | 职责 |
| --- | --- |
| `app/tracing.py` | 链路标识规则、生成与 `contextvars` 上下文传播 |
| `app/errors.py` | 错误类别枚举与异常归一化 |
| `app/logging.py` | 结构化日志与失败安全 sink |
| `app/metrics.py` | 有界维度指标聚合与兜底分桶 |
| `app/sampling.py` | 采样配置与确定性判定 |
| `app/timeline.py` | 按链路回读的有界时间线存储 |
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

## 按链路回读（诊断时间线）

请求结束后，可用其链路标识把该请求的诊断时间线查回来：

```bash
curl -s http://127.0.0.1:PORT/traces/<trace_id> | jq
```

**匹配规则**：按响应头 `X-Trace-Id` 的最终值（归一化小写）精确匹配；
查询结果三分支，对外明确区分、不会混为一谈：

| 状态 | HTTP | 含义 |
| --- | --- | --- |
| `found` | 200 | 记录仍在，返回完整时间线 |
| `evicted` | 410 | 记录曾存在但已被淘汰（在墓碑窗口内） |
| `never_seen` | 404 | 该标识从未出现（或已旧到连墓碑也被淘汰） |

**记录字段**（`record`）：

| 字段 | 含义 |
| --- | --- |
| `trace_id` / `source` | 最终链路标识；来源 `external`（沿用）/ `generated_missing` / `generated_invalid`（替换原因） |
| `route` / `method` / `status` | 路由、方法、最终状态码 |
| `latency_ms` | 端到端耗时 |
| `error_category` | 错误类别（`validation`/`not_found`/`dependency`/`internal`），无错误为 `null`；未抛异常的 4xx/5xx 按状态码推断 |
| `stages[]` | 关键阶段时间线：`received → handler_completed/handler_failed → response_finished`，各含 `offset_ms`（相对开始）与 `duration_ms`（距上一阶段耗时） |
| `finished_at` | 完成时间（UTC ISO8601） |

**淘汰规则**：记录数上界为 `RetentionPolicy.max_records`（默认 1024），
达到上界后按 **FIFO（插入顺序最旧者优先）** 淘汰；被淘汰的标识进入有界墓碑区
（`tombstone_capacity`，默认等于 `max_records`），墓碑窗口内查询返回 `evicted`，
窗口外退化为 `never_seen`。内存占用上界为 `max_records + tombstone_capacity`，长期运行无界增长。

## 采样策略

- 配置：`SamplingConfig(enabled, ratio)`，`enabled` 为总开关，`ratio ∈ [0,1]` 为保留比例。
- 判定为**确定性**：按 `trace_id` 前 8 位十六进制映射到 `[0, 2^32)` 与 `ratio × 2^32` 比较，
  同一标识判定结果稳定，可复现、可测试。
- 采样**只影响诊断记录是否保留**：未被采样的请求，其状态码、响应头、响应体与
  聚合指标与采样开启时完全一致。
- 当前生效配置与保留统计通过 `GET /trace-config` 读取：

```json
{
  "sampling": {"enabled": true, "ratio": 1.0},
  "retention": {"max_records": 1024, "stored_records": 3, "puts": 3,
                "evictions": 0, "hits": 1, "misses_evicted": 0,
                "misses_never_seen": 1, "write_failures": 0, "...": "..."}
}
```

## 退化与上限策略

- **日志写入失败**：sink 抛错被吞掉并累计 `logger.write_failures`，绝不影响请求返回；
  指标聚合同样独立，日志与指标任一失败都不阻塞请求路径。
- **指标维度上界**：维度为 `(route, status_class)`，`status_class` 仅 `1xx~5xx` 五种；
  路由维度数达到 `max_dimensions`（默认 64）后，新维度统一计入稳定的
  `(__overflow__, status_class)` 兜底分桶并累计 `dropped_dimensions`，
  内存占用上界为 `max_dimensions + 5` 个桶，杜绝高基数标签造成的无界增长。
- 聚合结果通过 `GET /metrics` 读取，含 `active_dimensions / dropped_dimensions /
  total_requests / total_errors / routes{count,error_count,avg_latency_ms,max_latency_ms}`。
- **时间线保留失败隔离**：`TimelineStore.put` 抛错被中间件吞掉并累计
  `write_failures`（见 `GET /trace-config`），请求的状态码 / 响应头 / 响应体不变，
  聚合计数不丢、不重、不错位；每个请求恰好保留（或尝试保留）一次，
  命中 / 淘汰判断在单锁下完成，并发不串扰。

## 兼容范围

- 既有行为完全保留：链路标识规则、响应头（`X-Trace-Id` / `X-Trace-Id-Source`）、
  错误响应体结构、结构化日志字段、`/metrics` 聚合语义均不变。
- 新增默认行为：默认 `SamplingConfig(enabled=True, ratio=1.0)` + 默认容量的
  `TimelineStore`，即默认全量保留可回读记录；不需要时可通过
  `create_app(sampling=SamplingConfig(enabled=False))` 关闭，关闭后除回读接口
  查不到记录外，其余对外行为与旧版一致。
- 新增接口：`GET /traces/{trace_id}`（回读）、`GET /trace-config`（采样配置与保留统计）。
  这些接口自身也经过中间件，会被正常记录日志、聚合与（按采样）保留。

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
curl -s http://127.0.0.1:PORT/traces/abcdef0123456789 | jq             # 按链路回读时间线
curl -s http://127.0.0.1:PORT/trace-config | jq                        # 采样配置与保留/淘汰/命中计数
```

## 测试覆盖（`tests/`）

- `test_tracing.py`：合法标识沿用、缺失/非法标识的可区分替换、32 并发请求上下文隔离；
- `test_errors.py`：四类错误归一化、内部错误细节不外泄且与日志同一 `trace_id`；
- `test_degradation.py`：日志 sink 持续失败时请求与指标不受影响、
  高基数路径触发兜底分桶且维度有界、并发聚合计数守恒、日志字段完整性；
- `test_replay.py`：采样开/关两种情形下业务结果不变、按比例确定性取舍、
  按链路命中与 `evicted`/`never_seen` 区分、容量上界与淘汰/命中计数、
  保留环节失败隔离、错误时间线的类别与替换原因、并发写入与回读守恒。
