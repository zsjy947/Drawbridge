# 优化计划 A：运维可见性与保留策略落地

> 状态：已实施（见文末验收记录）。本文档进入 git 追踪。
> 来源：与 drawbridge_ref 的架构对比结论（对比文档 §10 第一阶段清单）。
> 原则：所有改动沿用既有不变量——只读工具直接查库（与 ops_release_status 同类）、
> 清理只作用于登记范围、审计事件只追加。

## 1. 背景与问题

对比 review 暴露了两个"数据在库里、能力没暴露"或"文档写了、代码没有"的缺口：

1. **发布历史与任务历史对 MCP 客户端不可见**。`releases`/`jobs`/`events` 表完整
   记录了一切，但工具面只有单点查询（ops_release_status by id）。AI 客户端要
   回滚时没有工具列出版本，要排障时没有工具看最近任务——只能靠运维手工
   sqlite3。ref 同样缺这个能力（对比文档 §8 第 6 条），这是两边共同的洞。
2. **保留策略只存在于文档**。OPERATIONS.md §5 描述了"幂等键 7 天、诊断 job
   24h、job 日志 7 天"等保留行为，但代码中没有任何清理任务（对比文档 §8 /
   Drawbridge 已知缺口第 9 条）。长期运行下 idempotency_keys/plans/jobs 无限
   增长，spool 日志目录不被回收。

## 2. 方案一：`ops_history` 只读工具

### 接口

```
ops_history(app, environment, what=releases|jobs|events, limit<=50, cursor?)
```

- **releases**：按时间倒序列出该目标的 release（id/commit/image/status/
  rollback_of/created_at），并标注 `rollback_eligible`（status ∈
  succeeded/rollback/superseded 且非当前）。这是回滚选版的信息来源。
- **jobs**：最近任务（id/kind/action/status/queued_at/finished_at/
  runtime_change_started），支持 `queued_before` 游标翻页。
- **events**：该目标最近的审计事件（kind/ts/job_id/release_id/摘要），上限同
  样 50 行；append-only 语义不变，只是受限读取。

### 设计决策

- **不经 Runner 诊断通道**：与 `ops_release_status` 同类——纯 SQLite 有界读、
  无副作用、毫秒级，走 Gateway 直查。诊断通道保留给需要执行命令的操作
  （MVP spec §1 的原意是"只读命令"，不是"只读数据库访问"）。
- 不暴露全局（跨 app）查询；必须绑定 app+environment，与其他工具的
  目标语义一致。
- limit 上限 50（比日志的 200 更紧：历史行更宽）。

## 3. 方案二：保留清理任务（retention enforcement）

### 配置（drawbridge.yaml 新增段，全部有默认值，现有部署不用改）

```yaml
retention:
  cleanup_interval_seconds: 3600   # Runner 内的执行周期
  idempotency_key_days: 7          # MVP spec §8：幂等键保留 7 天
  plan_days: 7                     # 已过期 plan 的保留期
  job_record_days: 7               # 终态 job 记录保留期（被 release 引用的除外）
```

诊断 job 的保留沿用既有 `diagnostics.retention_seconds`（24h）；spool 日志
目录的保留沿用各 app 的 `retention.job_logs_days`。

### 清理规则（Runner 每小时执行一次，单事务 + 审计事件）

| 对象 | 条件 | 动作 |
|---|---|---|
| 幂等键 | `created_at` 超过 idempotency_key_days | 删除（先于 job 删除，避免 FK） |
| plans | `expires_at` 早于 now-plan_days 或终态且超期 | 删除（jobs.plan_id 无 FK，安全） |
| 诊断 job | 终态且 `finished_at` 早于 diagnostics.retention_seconds | 删 steps 后删 job |
| 其他终态 job | `finished_at` 超过 job_record_days **且** 未被任何 release 引用 **且** status ∉ {rollback_failed, needs_attention} | 删 steps（及关联幂等键）后删 job |
| spool 日志目录 | 随被删除的 job 一并移除 `log_dir/<job_id>/` | 目录删除，失败仅告警 |

**保护不变量**：

- `rollback_failed` / `needs_attention` 的 job 永不清理（目标阻断语义优先）；
- 被 release 行引用的 job 永不清理（审计链完整）；
- releases / artifacts 行本身不删（审计与回滚链），镜像实体的清理由目标机
  管理员按 OPERATIONS.md 执行——本任务不碰 Docker；
- 每次执行写 `retention_cleanup` 审计事件（零删除不写，避免测试与生产噪音）；
- 全部删除发生在单个 `BEGIN IMMEDIATE` 事务内。

### 触发点

`Runner._tick()` 内按 `cleanup_interval_seconds` 节流（进程内时间戳）；新增
`runner/retention.py` 承载逻辑，`Store` 提供 `retention_cleanup()` 数据库侧
原语。CLI 场景（simulate）不触发清理以外的行为变化。

## 4. 附带：ops_catalog 暴露 runtime 类型

`ops_catalog` 的 apps 段为每个 environment 增加 `runtime`（compose|
simulation），客户端可以据此判断证据等级（simulation 结果带标记）。

## 5. 验收标准

1. `ops_history(releases)` 返回按时间倒序的版本列表，当前版本标注正确，
   rollback_eligible 与 ops_release_rollback 的准入判断一致；
2. `ops_history(jobs/events)` 有界（≤50）且游标翻页可用；
3. 未知 `what` / 越界 limit 被严格拒绝（INVALID_PARAMETER）；
4. 清理任务：超期幂等键/plans/诊断 job 被删除；被 release 引用的 job、
   阻断状态的 job 保留；steps 随 job 删除；日志目录随 job 删除；
5. 零删除时不产生审计事件；有删除时事件含各类计数；
6. 全量 pytest / ruff / mypy 通过（Windows 开发机）。

## 6. 实施与验收记录（2026-09-20）

- `ops_history` 工具：`gateway/service.py::ops_history` + MCP 注册
  （`ops_catalog` 同时暴露各环境 `runtimes`）；releases 视图带
  `is_current`/`rollback_eligible`，jobs/events 支持游标翻页。
- 保留清理：`config/models.py::RetentionPolicyConfig`（`main.retention`，
  默认值内建，存量部署无需改 YAML）；`Store.retention_cleanup`（单事务）+
  `runner/retention.py::run_retention`（日志目录 + 审计事件）；
  `Runner._tick` 按 `cleanup_interval_seconds` 节流触发。
- schema 追加 `idx_events_target` 索引（IF NOT EXISTS，存量库重启自动生效）。
- 保护不变量按 §3 落实：release 引用与阻断状态的 job、releases/artifacts/
  events 行、Docker 实体均不触碰。
- 测试：`tests/unit/test_retention_and_history.py`（6 例：历史翻页/严格校验/
  保留保护不变量/活跃任务不受影响/Runner 触发）；
  `tests/integration/test_functional_flows.py` 增加 ops_history 的 HTTP 面
  与 catalog runtimes 断言。Windows 开发机全量 pytest 277 通过、ruff/mypy
  零告警。
