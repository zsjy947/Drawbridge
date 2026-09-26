# Drawbridge 升级架构说明与实现核查

> **定位**：原始设计文档（[TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) 与
> [MVP_IMPLEMENTATION_SPEC.md](MVP_IMPLEMENTATION_SPEC.md)）保持不动；本文记录
> **在两份原始设计基线之上迭代落地的架构增量**，并对每项"标明已实现"的能力
> 逐条核查其**是否真实实现**。
>
> **核查方式**（2026-09-26）：全量通读 `src/drawbridge` 39 个源文件 + configs/deploy/docs，
> 与设计声明逐项比对；并实测质量门：`uv run pytest -q`（284 passed / 4 skipped）、
> `uv run ruff check src tests`（零告警）、`uv run mypy`（strict 零告警）。
>
> **代码基线**：`main @ 922b22c`。
> **来源文档**：本仓与 drawbridge_ref 的两轮对比（结论已吸收进本文，对比文档为
> 本地工作文档，不进 git）；优化计划 A/B（已实施，原文已删除、内容吸收于本文）。
>
> **未完成事项**不在此展开，统一移交 [OPTIMIZATION_D_PRODUCTION_CUTOFF_AND_REVIEW_FIXES.md](OPTIMIZATION_D_PRODUCTION_CUTOFF_AND_REVIEW_FIXES.md)。

---

## 1. 相对原始设计的架构增量总览

| # | 增量 | 来源 | 提交 | 与原设计的关系 | 核查结论 |
|---|---|---|---|---|---|
| 1 | simulation 运行时适配器（Runtime Module 第二 adapter） | 对比轮 1 §8-1 / §10 | `66589b6` | 落实 TECHNICAL_DESIGN §4 的 Runtime Module seam（"两个以上 adapter 才证明 seam 有价值"） | ✅ 已实现 |
| 2 | `drawbridge-simulate` 命令行通信测试（fixture + `--http`） | 对比轮 1 §8-1 / 优化计划 B §2 | `66589b6` `7908103` | 原设计无等价物；通信验收手段 | ✅ 已实现（1 处注释瑕疵） |
| 3 | Runner 无 systemd 前台模式（`--once` / `--drain` / flock 单实例） | 对比轮 1 §8-2 / §10 | `66589b6` `7908103` | 原设计假定 systemd；扩展运维入口，不动准入/锁语义 | ✅ 已实现 |
| 4 | `ops_history` 运维可见性工具 | 优化计划 A §2 | `8f221b7` | TECHNICAL_DESIGN §5 工具表之外的新只读工具（数据早就在库里） | ✅ 已实现（releases 游标缺陷，移交 D8） |
| 5 | 保留策略落地（retention enforcement） | 优化计划 A §3 | `8f221b7` | 把 TECHNICAL_DESIGN §12 与 MVP §8 的保留约定从文档变为代码 | ✅ 已实现 |
| 6 | stale running job 启动自检（needs_attention reconcile） | 对比轮 1 §10-二1 / 优化计划 B §3 | `7908103` | 落实 TECHNICAL_DESIGN §7"不明现场进入 NeedsAttention"，自动检测、绝不重跑 | ✅ 已实现 |
| 7 | `drawbridge-init-config` 配置骨架生成 | 优化计划 B §4 | `7908103` | 原设计无 bootstrap；降低首次部署门槛 | ✅ 已实现 |
| 8 | MCP SDK TransportSecuritySettings 镜像边缘白名单（421 修复） | 910B 实机调试（原 DEBUG.md P4） | `6d01d97` | 修复 TECHNICAL_DESIGN §8"内网直连"承诺在 SDK 层被 421 拦截的实现缺口 | ✅ 已实现 |
| 9 | 配置模型接受 Windows 盘符路径（仅开发/模拟） | 对比轮 1 §9 | 随 simulation 落地 | 对 TECHNICAL_DESIGN §6"绝对路径"的开发机让步，生产仍 POSIX | ✅ 已实现 |
| 10 | `ops_catalog` 暴露各环境 `runtimes` 类型 | 优化计划 A §4 | `8f221b7` | 客户端可据此判断证据等级 | ✅ 已实现 |

---

## 2. 逐项核查（代码证据）

### 2.1 simulation 运行时适配器 ✅

- **实现**：`runner/simulation.py`（443 行）——`SimulationRuntime` 继承 `DeployRuntime` 并重绑定
  步骤注册表；`RuntimeSelector.adapter_for()` **只按** `environments.<env>.runtime`
  （`config/models.py:529` Literal["compose","simulation"]）选择 adapter，永远不来自请求参数；
  `build_runtime()` 是唯一构造点（`simulation.py:411-433`）。
- **不变量核查**：准入/幂等/锁/预算/审计与 compose 路径完全同代码（Runner 只认
  `step_executor` seam，`loop.py:410-415`）；模拟证据全部带 `simulated: true` /
  `validation_level: simulation` 标记（`simulation_checks`、各 step 返回体）；
  git 解析、源码快照（`git archive` + 安全解包）、compose 渲染契约、漂移检测（渲染文件
  引用核对）、全部 SQLite 状态机为真实路径。
- **无 Linux 门控**：与生产 `DeployRuntime.__call__` 的 `_RUNTIME_LINUX` 拒绝
  （`runtime.py:269-273`）刻意相反——这正是该 adapter 的存在目的，且不构成旁路。

### 2.2 `drawbridge-simulate` ✅（1 处注释瑕疵）

- **实现**：`entries/simulate_main.py`（1022 行）。fixture 模式程序化构造隔离 git 仓库 +
  simulation 配置（`.gitattributes` 防 CRLF 误报）；场景覆盖 catalog → status → logs →
  git_status → config_read → 双 plan/apply/deploy → ops_test → restart → rollback；
  `--http` 模式在同一进程起完整生产栈（EdgeMiddleware + MCP app + uvicorn，
  `proxy_headers=False`）并用官方 MCP SDK 客户端驱动，token 开启时含无凭据负例断言
  （`simulate_main.py:762-789`）。
- **瑕疵**：负例注释称中间件返回 401，实际 `middleware.py:127` 返回 403（测试靠异常类型
  通过，不受影响）→ 移交 D11 清理批。

### 2.3 Runner 无 systemd 前台模式 ✅

- **实现**：`entries/runner_main.py:26-41`（`--once`/`--drain`/`--drain-timeout`）；
  `loop.tick_once()`（tick + 排空）与 `run_until_idle(timeout)`（有界循环，超时抛 TIMEOUT）。
- **语义核查**：三种模式共用 `_tick()` 的维护门/容量/冷却/锁路径；跨进程目标锁被占时
  requeue（`loop.py:255-289`）；单实例由 `runner.lock` flock 强制（重复启动退出码 3；
  Windows 开发机按约定豁免，`runner_main.py:45-59`）。与 systemd 常驻实例可共存。

### 2.4 `ops_history` ✅（releases 游标缺陷，移交 D8）

- **实现**：`gateway/service.py::ops_history`（613-701 行）+ MCP 注册（`mcp_app.py`）+
  `Store.list_jobs/list_events/list_releases`（`store.py:481-532,769-778`）。
- **验收复核**：releases 视图带 `is_current`/`rollback_eligible` 且与
  `ops_release_rollback` 准入判定一致（status ∈ succeeded/rollback/superseded、非当前）；
  jobs/events 有界（≤50，越界/bool 严格拒绝）且游标翻页可用；直查 SQLite 不占诊断通道。
- **缺陷**：releases 分支返回 `next_cursor` 但 `cursor` 参数被忽略、`list_releases` 无
  before 过滤——第二页永远取不到；jobs/events 不受影响。

### 2.5 保留策略落地 ✅

- **实现**：`config/models.py::RetentionPolicyConfig`（`main.retention`，默认值内建）；
  `Store.retention_cleanup`（单 `BEGIN IMMEDIATE`，`store.py:839-926`）；
  `runner/retention.py::run_retention`（日志目录删除 + `retention_cleanup` 审计事件）；
  `Runner._tick` 按 `cleanup_interval_seconds` 节流触发（`loop.py:177-194`）。
- **保护不变量核查**（`store.py:884-898`）：被 release 行引用的 job 永不删（NOT EXISTS）；
  `rollback_failed`/`needs_attention` 永不删；releases/artifacts/events 行不触碰；
  steps 与幂等键先于 job 删除满足 FK；零删除不写审计事件（`retention.py:58`）；
  阻断语义（`store.py:51` BLOCKING_STATUSES）优先于保留。

### 2.6 stale running job 启动自检 ✅

- **实现**：`Store.reconcile_stale_running`（`store.py:597-643`，心跳缺失回退
  `started_at`）→ `needs_attention` + `job_reconciled` 审计事件；`Runner._maybe_reconcile_stale`
  首个 tick（`_next_reconcile_at = 0.0`）立即执行、此后随保留清理节流（`loop.py:196-221`）；
  阈值 `main.recovery.stale_running_job_seconds`（默认 900s）。
- **语义核查**：绝不自动重跑、绝不接管；单实例 flock 是"心跳 = 存活信号"的前提（代码
  注释与文档均已标注该前提）。

### 2.7 `drawbridge-init-config` ✅

- **实现**：`entries/initconfig_main.py`——operations/workflows 从仓库 configs/ 原样复制
  （与代码目录耦合、共同评审），drawbridge/apps/compose 按 `--root` 锚定生成，
  `REPLACE_*` 占位符与手工步骤打印到输出；目标目录含任一同名文件即拒绝（不覆盖）。

### 2.8 MCP SDK transport security 镜像 ✅

- **实现**：`gateway/mcp_app.py:42-54` `_transport_security()`——把边缘白名单
  （allowed_hosts 小写化、allowed_origins 原样）镜像进 SDK 的
  `TransportSecuritySettings(enable_dns_rebinding_protection=True, ...)`，
  `build_asgi_app()` 同时传 `host=bind_address`。
- **背景**：SDK 默认 localhost-only Host 校验会在边缘中间件之前以 421 拒绝所有局域网
  Host（910B 实机排障定位，当时全仓搜不到该响应串证明 421 非 Drawbridge 产生）。
  本修复保留 SDK 层防护而非关闭它，两层读同一管理员白名单。

### 2.9 开发机路径让步 ✅

- **实现**：`config/models.py:36-46` `_is_absolute_path` 接受盘符绝对路径
  （`_WINDOWS_ABSOLUTE`），仅服务于 Windows 开发/模拟配置；生产 POSIX 绝对路径不受影响；
  toolchain/paths 校验共用该判定。

### 2.10 `ops_catalog` runtimes 暴露 ✅

- **实现**：`gateway/service.py:266-280`——apps 段每个 environment 返回 `runtimes`
  （compose|simulation），客户端可据此判断证据等级。

---

## 3. 优化计划 A/B 验收标准复核汇总

两份计划原文已删除（内容吸收于本文 §2），其验收标准逐条复核结果：

| 计划 | 验收标准 | 复核结论 |
|---|---|---|
| A | ops_history 三视图 + is_current/rollback_eligible 与回滚准入一致 | ✅（releases 游标除外 → D8） |
| A | jobs/events ≤50 且游标翻页；未知 what/越界 limit 严格拒绝 | ✅ |
| A | 清理：超期幂等键/plans/诊断 job 删除；release 引用与阻断状态保留；steps/日志目录随删 | ✅ |
| A | 零删除无审计事件；有删除事件含计数 | ✅ |
| A | ops_catalog 暴露 runtimes | ✅ |
| B | `--http` 真实 HTTP 往返 + token 正负例 + `mode: http` 报告 | ✅（`tests/integration/test_http_scenario.py` 随全量回归通过） |
| B | stale reconcile：超时翻转/阻断/审计；新鲜与自有 job 不受影响 | ✅ |
| B | init-config 生成 bundle 可加载、拒绝覆盖 | ✅ |
| B | 全量质量门 | ✅（2026-09-26 实测 284 passed / 4 skipped + ruff + mypy strict） |

---

## 4. 核查中发现的偏差与缺陷（移交 OPTIMIZATION_D）

以下不属于 A/B 交付承诺本身的失败，而是核查过程中发现的"规格声明/文档承诺与实现"
之间的缺口，全部登记进 [OPTIMIZATION_D_PRODUCTION_CUTOFF_AND_REVIEW_FIXES.md](OPTIMIZATION_D_PRODUCTION_CUTOFF_AND_REVIEW_FIXES.md)：

1. `ops_history` releases 游标不可用（A 遗留，→ D8）；
2. `project_list` 参数与 MVP §4 声明不一致：规格登记 `subdir、limit、cursor、可选 release_id`，
   实现只登记 `subdir`，handler 返回的 `next_cursor` 因此无法被客户端使用（→ D8）；
3. `config_read` 的"可选 release_id"（MVP §4）未实现，诊断固定当前诊断根（→ D8 一并裁决：
   实现或在文档层明确降级为非目标）；
4. 其余小项（401/403 注释、store 内 assert、死校验器等）→ D11 清理批；
5. AGENTS.md"已知边界"两条（`step_stop_initial` 归属核对缺失、紧急维护标志跨重启被配置
   覆盖且 OPERATIONS.md 紧急路径与该行为矛盾）在本次核查中确认仍然成立 → D10/D3 关联处理。

---

## 5. 当前状态

- 代码基线 `main @ 922b22c`；质量门三绿（见文首核查方式）。
- 910B 实机进度：**simulation 运行时已从零部署并完成 MCP 全链路验收**
  （initialize / tools_list / catalog / status / git_status / plan / apply /
  deploy succeeded / logs / compose_status；8×910B2 NPU 只读观测通过，
  npu-smi 24.1.rc2）。切生产（`runtime: compose` + Docker + rootless BuildKit +
  `/dev/davinci2` 设备挂载登记）为下一步，验收清单与前置任务见 OPTIMIZATION_D。
