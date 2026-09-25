# HTTP 请求链路追踪与运行时可观测能力（Python / FastAPI）

在现有 FastAPI 服务中实现了一套**请求级链路标识、上下文传播、结构化日志、
有界指标聚合、错误归一化与故障退化**能力，保证同一请求在并发、异常与写入失败
等情形下都能得到一致且可解释的诊断结论。

## 1. 模块布局

| 模块 | 职责 |
| --- | --- |
| `app/identity.py` | 链路标识的解析、校验、生成与拒绝原因 |
| `app/context.py` | 基于 `contextvars` 的请求上下文，并发隔离 |
| `app/errors.py` | 异常类别归一化与安全错误模型 |
`app/jsonlog.py` | 行分隔 JSON 结构化日志，写入失败静默降级 |
| `app/metrics.py` | 有界内存指标聚合（确定上界 + 兜底桶） |
| `app/observability.py` | 日志/指标门面、故障注入与退化计数 |
| `app/middleware.py` | ASGI 中间件：标识、上下文、计时、归因 |
| `app/handlers.py` | 示例业务路由（成功/校验/未找到/上游/内部错误/超时） |
| `app/main.py` | 应用装配、统一异常处理、`/metrics` 与 `/healthz` |

## 2. 链路标识规则

每个请求由 `TraceMiddleware` 处理 `X-Request-ID` 头：

* **合法外部标识沿用**：长度 `8~128`，字符仅限 ASCII 字母、数字与 `-_.~`。
* **缺失或非法时替换**为内部生成值 `req-<uuid4hex>`（36 字符，带固定前缀），
  并通过响应头 `X-Request-ID-Source` 给出**可区分原因**：

| 输入情形 | `X-Request-ID-Source` |
| --- | --- |
| 合法值原样沿用 | `supplied` |
| 头缺失 / 空串 / 纯空白 | `generated:missing` |
| 长度 > 128 | `generated:too_long` |
| 含不可打印/控制字符（如 `\t`、NUL） | `generated:non_printable` |
| 含非法字符（空格、`=!`、非 ASCII 等） | `generated:malformed` |
| 长度 < 8 | `generated:malformed` |

响应头 `X-Request-ID` 始终回写最终标识；响应体（成功与错误）也带 `request_id`，
方便端到端对账。

**并发隔离**：标识绑定在 `contextvars` 上，每个请求在自己的 asyncio Task 中
运行，100 请求 / 20 线程并发测试下响应头、响应体与每条日志的归属完全一致，
互不串扰。

## 3. 结构化日志

日志为单行 JSON，统一字段：

```json
{"ts":"2026-09-25T...Z","level":"info","event":"request.complete",
 "request_id":"req-...","method":"GET","path":"/items/404",
 "status_code":404,"duration_ms":1.23,"result":"not_found",
 "id_supplied":false,"id_reason":"missing"}
```

关键事件：`request.start`、`request.error`（未受控异常时）、
`request.complete`、`error.normalized`。耗时以 `perf_counter` 毫秒计。

## 4. 错误分类与对外安全

异常经 `app.errors.normalize` 归一为 6 个**可区分类别**，响应统一为

```json
{"error":{"category":"not_found","message":"resource not found",
          "request_id":"req-..."}}
```

| 类别 `category` | HTTP | 固定对外文案 | 来源 |
| --- | --- | --- | --- |
| `validation` | 422 | request validation failed | 参数校验（含 FastAPI 校验） |
| `not_found` | 404 | resource not found | `AppError` 子类 |
| `unavailable` | 503 | service temporarily unavailable | `ConnectionError` 等 |
| `upstream_failure` | 502 | upstream dependency failure | `AppError` 子类 |
| `timeout` | 504 | request timed out | `TimeoutError`/`asyncio.TimeoutError` |
| `internal` | 500 | internal server error | 其它一切未受控异常 |

内部异常的 `str()`、堆栈、令牌等细节**只写日志（仅记录异常类名）**，
绝不进入响应体（`/boom` 即演示该脱敏路径）。

## 5. 退化与上限策略

**写入失败隔离**：日志与指标后端的任何写入异常都会被捕获并计数
（`failed_writes`），门面方法不抛出，业务响应与正常情况逐字一致；
指标读取接口 `/metrics` 始终可用。退化状态可通过
`degradation.degraded` 观察。

**有界聚合（防高基数）**：指标 key 固定为 `(phase, result, status)` 三元组，
每个维度都有白名单与兜底桶 `other`；状态码只保留 `2xx/3xx/4xx/5xx/xxx`
区间，不保留原值；延迟不保留原始样本，仅滚动 `count/sum/min/max/avg`。

序列数有确定上界：

```
max_series = (2 个 phase + other) × (7 个 result + other) × 5 个状态桶 = 120
```

即使把 request_id 等高基数值直接塞进标签（测试中灌入 10 万个不同值），
序列数也恒不超过 120，内存与性能不会无界增长。

## 6. 对外接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /`、`/healthz` | 成功路径，回显当前 `request_id` |
| `GET /items/{id}` | `<0`→422；`404`→404；`502`→502；其余成功 |
| `GET /boom` | 未受控内部错误 → 500（脱敏演示） |
| `GET /slow?ms=` | `ms>100` → 504 超时 |
| `GET /metrics` | 聚合序列、耗时统计、维度上界声明、退化计数 |

`/metrics` 响应示例片段：

```json
{"bounds":{"max_series":120,"phases":["http","request","other"],...},
 "series_count":3,
 "series":[{"phase":"request","result":"success","status":"2xx",
            "count":2,"latency_ms":{"count":2,"avg":0.8,"min":0.4,
            "max":1.2,"sum":1.6}}],
 "degradation":{"log_write_failures":0,"metric_write_failures":0,
                "degraded":false}}
```

## 7. 验证方法

```bash
# 安装依赖（Python >= 3.14）
uv sync

# 运行全部测试（36 个，含并发/非法标识/写入失败/高基数场景）
uv run python -m pytest -q

# 带单测诊断日志（输入、判定依据、归一化结果）运行
uv run python -m pytest -q -s

# 本地启动（随机端口）
uv run python main.py
```

手工验证：

```bash
curl -i -H 'X-Request-ID: trace-abcdef123456' http://127.0.0.1:<port>/
curl -i http://127.0.0.1:<port>/items/404
curl -s http://127.0.0.1:<port>/metrics | python -m json.tool
```

## 8. 测试覆盖结论（可复现）

* `tests/test_identity.py`：12 种标识输入的归一化结论 + 2000 并发生成唯一性
  + 4 种拒绝原因互斥可区分；
* `tests/test_middleware.py`：7 种请求头情形、错误响应携带标识、
  100 并发请求（成功/错误混合）响应与日志零串线、内部错误细节不泄漏；
* `tests/test_metrics.py`：10 万高基数标签 → 序列数 ≤ 120、已知维度保留、
  状态码分桶稳定、5000 次并发计数精确、写入失败被隔离；
* `tests/test_failure.py`：日志/指标单后端及双后端故障下请求照常返回，
  且对外错误结论与正常时逐字一致；
* `tests/test_api.py`：`/metrics` 聚合数值、结构化日志统一字段与脱敏。

每个测试用例均用 `print` 输出**输入、判定依据与归一化结果**，使用 `-s` 可查看。
