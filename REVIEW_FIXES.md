# 评审问题修复说明

针对评审提出的三类问题，逐项修复并验证如下。所有结论均可通过文末命令复现。

## 问题 1：多数据库与安全控制未实际启用

**修复内容**

- `server.py` 在 lifespan 中为**每个数据库**创建独立的 `SQLValidator` 与 `SQLExecutor`（`_effective_security_policy`：数据库级 `blocked_tables` / `blocked_columns` / `allow_explain` 覆盖全局 `SecurityConfig`，`None` 表示继承全局值）。
- `orchestrator.py` 按 `database_name` 分发到对应库的验证器与执行器，访问不存在的数据库返回明确错误。
- **新增修复**：验证器此前会放行"数据修改 CTE"（如 `WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d`）。已在 `sql_validator.py` 新增 `_check_cte_safety`，递归拦截 CTE 内的写操作；EXPLAIN 包裹的同类语句同样被拦截。
- **新增修复**：双层括号子查询 `SELECT * FROM ((SELECT 1)) s` 被误判为非 SELECT。已修复 `_check_subquery_safety`：解包嵌套括号，放行只读的集合操作与 VALUES 行构造。
- **新增修复**：日志过滤器此前不脱敏顶层列表中的敏感字段（`[{"token": ...}]`），已修复。

**验证方式**：`uv run python demo_usecase_v2.py` 用例 1（医院三库场景），输出 7 类 SQL × 3 库的拦截矩阵。

## 问题 2：弹性与可观测性未整合进请求流程

**修复内容（整合点均在 `orchestrator.py` 的 `execute_query` 真实链路中）**

- **速率限制**：`MultiRateLimiter`（查询/LLM 并发上限）在请求入口获取槽位，超时返回 `rate_limit_exceeded`。
- **重试/退避**：验证失败按指数退避重试，并把上一次错误作为反馈传给 LLM；重试耗尽返回 `security_violation` 并记录 `sql_rejected` 指标。
- **熔断器**：`CircuitBreaker`（CLOSED→OPEN→HALF_OPEN→CLOSED）保护 LLM 调用。
- **指标**：Prometheus 指标（请求数、耗时、LLM 调用/延迟/token、SQL 拒绝数）在管线各步骤自动记录。
- **追踪**：每个请求生成 `request_id`，贯穿日志与响应。

**验证方式**：`uv run python demo_usecase_v2.py` 用例 2 依次演示两级退避重试、重试耗尽最终拒绝、并发限流（6 请求上限 3）、熔断器含 HALF_OPEN→OPEN 再跳闸的状态迁移、Prometheus 指标快照。

## 问题 3：响应/模型缺陷与测试覆盖不足

**修复内容**

- `to_dict` 无重复实现：全代码库仅 `ErrorDetail.to_dict` 与 `QueryResponse.to_dict` 各一处。
- 配置字段全部落地使用（`pool_timeout`、`max_execution_time`、`safe_search_path`、`readonly_role`、`sample_rows`、`timeout_seconds`、`confidence_threshold`、`schema_ttl`、`rate_limit_timeout`）。
- **新增测试**：为验证器补充边界测试（数据修改 CTE、工具命令、VALUES 子查询、EXPLAIN 变体等）；并新增 5 个测试文件覆盖此前薄弱模块：`test_logging.py`、`test_tracing.py`、`test_result_validator.py`、`test_introspection.py`、`test_pool.py`。

**当前质量门**

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| 测试 | `uv run pytest` | **408 passed**（46 个需真实 PG/LLM 的用例跳过） |
| 总体覆盖率 | `uv run pytest --cov=src` | **96.24%**（要求 ≥80%） |
| 核心安全模块 | 同上 | sql_validator 95%、introspection/pool/tracing 100%、logging 96%、result_validator 98% |
| Lint | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy src` | no issues in 30 source files |

## 截图采集指引

1. `uv run pytest --cov=src --cov-report=term` — 测试通过与覆盖率表
2. `uv run ruff check . && uv run mypy src` — Lint 与类型检查
3. `uv run python demo_usecase_v2.py` — 三段演示（英文输出）：三库安全矩阵 / 弹性与指标 / 模型与质量门，可分段截图
4. （可选）启动服务后访问 `http://localhost:9090` 查看 Prometheus 指标端点
