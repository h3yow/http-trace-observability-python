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
          └─ replay.ReplayPipeline        采样判定 + 有界诊断记录（保留失败静默降级）
```

| 模块 | 职责 |
| --- | --- |
| `app/tracing.py` | 链路标识规则、生成与 `contextvars` 上下文传播 |
| `app/errors.py` | 错误类别枚举与异常归一化 |
| `app/logging.py` | 结构化日志与失败安全 sink |
| `app/metrics.py` | 有界维度指标聚合与兜底分桶 |
| `app/replay.py` | 按链路回读：采样策略、诊断时间线、有界保留与淘汰 |
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

## 按链路回读（诊断时间线）

请求结束后，可用其链路标识把该请求的诊断时间线查出来。

### 回读字段

`GET /replay/{trace_id}` 命中时（`200`，`{"result": "found", "record": {...}}`）返回：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 最终生效的链路标识（小写十六进制） |
| `trace_id_source` | 标识是沿用还是替换及原因：`external / generated_missing / generated_invalid` |
| `stages[]` | 关键阶段时间线：`receive / handler / respond`，各含 `offset_ms`（相对请求开始）与 `duration_ms` |
| `status` | 最终状态码 |
| `error_category` | 错误类别（无错误为 `null`） |
| `latency_ms` | 端到端耗时 |

### 匹配与淘汰规则

- **匹配**：按最终生效的 `trace_id` 精确匹配；未命中时明确区分两种情形——
  - `404 {"result": "never_seen"}`：该标识从未被保留（从未出现，或未采样）；
  - `410 {"result": "evicted"}`：记录曾存在但已被容量策略淘汰。
- **淘汰**：保留容量 `capacity`（默认 1024）是确定上界，写满后按 **FIFO（最早写入先淘汰）**
  这一固定规则淘汰；被淘汰的标识进入有界墓碑队列（`tombstone_capacity`，默认 1024），
  用于支撑 `evicted` 判定。墓碑同样写满后最老墓碑被丢弃，对应标识退化为 `never_seen`。
  内存上界 = `capacity` 条记录 + `tombstone_capacity` 个墓碑，长期运行不无界增长。
- **并发**：读写共用一把锁，每个请求只记录一次，命中/淘汰判定互不串扰。

### 采样策略

- `SamplingPolicy(enabled, rate)`：`enabled=false` 或 `rate<=0` 不保留任何记录；
  `rate>=1` 全量保留；其余按 `sha256(trace_id)[:8] / 2^64 < rate` 判定——
  对同一 `trace_id` 结果稳定、可解释、可复现。
- **未采样不影响业务**：状态码、响应头、响应体与既有聚合数字与全量保留时完全一致，
  仅不产生可回读记录（回读得到 `never_seen`）。
- 当前生效配置通过 `GET /replay` 读取：`sampling{enabled, rate, rule}`。

### 计数与失败行为

`GET /replay` 同时暴露保留与命中情况的可读计数：

```
retention{capacity, tombstone_capacity, size, tombstones,
          recorded, evicted, hits, misses_never_seen, misses_evicted}
record_failures        # 保留写入失败次数（失败被吞掉，绝不影响请求）
skipped_not_sampled    # 因采样未保留的请求数
```

- **保留失败隔离**：记录写入任何环节抛错都被吞掉并累计 `record_failures`，
  不改变状态码/响应头/响应体，也不让既有聚合计数丢失、重复或对不上。
- **守恒不变量**：`recorded == size + evicted`；`hits` 只在真实命中时 +1。

### 与旧行为的兼容范围

- 既有接口（`/`、`/work`、`/items/{id}`、`/flaky`、`/boom`、`/metrics`）的
  状态码、响应头、响应体与聚合口径完全不变；回读与采样是纯新增能力。
- 新增接口：`GET /replay`（配置与计数）、`GET /replay/{trace_id}`（按链路回读）。
- `create_app()` 新增可选参数 `replay: ReplayPipeline`，缺省自动构造
  （全量采样、容量 1024），旧调用方式无需修改。

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
curl -s http://127.0.0.1:PORT/replay/abcdef0123456789 | jq             # 按链路回读诊断时间线
curl -s http://127.0.0.1:PORT/replay | jq                              # 采样配置 + 保留/命中/淘汰计数
```

## 测试覆盖（`tests/`）

- `test_tracing.py`：合法标识沿用、缺失/非法标识的可区分替换、32 并发请求上下文隔离；
- `test_errors.py`：四类错误归一化、内部错误细节不外泄且与日志同一 `trace_id`；
- `test_degradation.py`：日志 sink 持续失败时请求与指标不受影响、
  高基数路径触发兜底分桶且维度有界、并发聚合计数守恒、日志字段完整性；
- `test_replay.py`：采样开/关两种情形（关闭时业务结果与聚合数字逐字段一致）、
  按链路命中 / `never_seen` / `evicted` 三态区分、容量上界与 FIFO 淘汰计数守恒、
  保留写入失败时的隔离（响应与指标不受影响且失败被计数）、
  64 并发写入 + 64 并发回读的命中与计数守恒。
