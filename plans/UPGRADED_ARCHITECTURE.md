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
> **代码基线**：`main @ 922b22c`（A/B 时代）→ 计划 D 实施后见 §5。
> **来源文档**：本仓与 drawbridge_ref 的两轮对比（结论已吸收进本文，对比文档为
> 本地工作文档，不进 git）；优化计划 A/B（已实施，原文已删除、内容吸收于本文）；
> 优化计划 D「910B 切生产就绪与审查修复」（D0–D16 已全部实施，2026-09-26，
> 原文为本地规划文档不入库，实施结论吸收于本文 §2.11–§2.15 与
> [docs/VERIFICATION_RECORD.md](../docs/VERIFICATION_RECORD.md) 的逐任务验收留痕）。

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
| 11 | 一键质量门 + 追加式验收证据链（`scripts/verify.py` + VERIFICATION_RECORD） | 计划 D D0 | `96a894f` | 原设计无等价物；防"声称与实际不符" | ✅ 已实现 |
| 12 | Compose 模板结构校验 + 指纹冻结 + plan schema v2（STALE_PLAN 五条件） | 计划 D D3 | `b3b5a60` | 把模板纳入 plan 复核权威（原设计模板不参与任何校验） | ✅ 已实现 |
| 13 | 部署/回滚成功路径原子完成（`complete_job_with_release` 单事务） | 计划 D D4 | `45c9a32` | 强化 TECHNICAL_DESIGN §7 的崩溃窗口语义（消除人工 reconcile 面） | ✅ 已实现 |
| 14 | 受控输入防线补强：Dockerfile `# syntax=` 拒绝、git 本地 config 危险键扫描、`gc.auto=0` | 计划 D D2/D5 | `9c189a7` `b1f4709` | 落实 MVP §3 受控构建/Git 前缀契约的代码强制 | ✅ 已实现 |
| 15 | 跨运行时基线身份（releases.simulated + current/基线按 runtime 过滤 + 恢复前镜像探针） | 计划 D D12（含 D3 迁移列） | `e93e6ef` | 补原设计未覆盖的模式切换语义 | ✅ 已实现 |
| 16 | BuildKit 产物交接目录 + buildctl du 可达性探测（`build_output_dir` 必填） | 计划 D D16 | `1ad45d4` | 落实 PROFILES §2 交接模型的代码实现 | ✅ 已实现 |
| 17 | 诊断准入上限（max_read_requests）+ 分页收口（releases 游标 / project_list 参数面） | 计划 D D7/D8 | `442bf79` `bd737bc` | 修正 MVP §7/§4 声明的实现缺口 | ✅ 已实现 |
| 18 | 运维可用性批：selfcheck 角色、empty.env 交付与 config_dir 解析、失败证据 details、诊断根修复、工作目录回收、SIGTERM 优雅停机、清理批 | 计划 D D1/D6/D9/D10/D11/D13/D14/D15 | `c229a79`–`0a9bfd7` | 原设计假设面之内的缺口补齐 | ✅ 已实现 |

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

### 2.11 计划 D：切生产前置防线（D0–D5、D13、D16）✅

- **D0 证据链**：`scripts/verify.py`（ruff/mypy/pytest 顺序执行、输出与
  report.json 落盘 `var/verification/<UTC 时间戳>/`）+
  `docs/VERIFICATION_RECORD.md`（追加式、固定节格式、显式记录未验收项）；
  AGENTS.md 质量门补一键命令。
- **D1 systemd 权限**：两 unit `UMask=0007`（双账号共享 state.db 组可写）、
  runner `SupplementaryGroups=docker`、NPU DeviceAllow 注释模板；
  DEPLOYMENT §2 / OPERATIONS §8 同步。
- **D2 构建前端防护**：`scan_dockerfile_directives` 纯文本扫描，`# syntax=`
  → `BUILD_UNSUPPORTED_FRONTEND`（任何 buildctl 调用之前拒绝）；escape/check
  放行并记入步骤结果。
- **D3 模板指纹**：`config/compose_template.py`（严格 YAML/非空 services/
  恰好一个 image token/服务集与登记一致/规范化 JSON SHA-256 指纹）；加载期
  校验（文件存在即校验，缺失在 plan/apply 拒绝——开发机加载样例 bundle 的
  /etc 路径不受影响）；STALE_PLAN 五条件（gateway 与 runner 两处同步）；
  SCHEMA_VERSION 1→2 单事务迁移（plans +2 列、releases +simulated 列 +
  evidence 回填），存量 NULL 指纹 plan 恒 STALE_PLAN。
- **D4 原子完成**：`Store.complete_job_with_release` 单 BEGIN IMMEDIATE 写
  releases/artifacts/release_recorded/job 终态/job_finished；DeployWorkflow
  交付 `StagedRelease`（成功路径不再先写 release）；`release_rollback` 经
  `JobContext.staged_releases` 同样原子化；失败/恢复路径与
  `reconcile_stale_running` 分工不变。
- **D5 git 防线**：`GitClient` 远程接触前纯文本扫描 `<repo>/.git/config`
  （include/includeIf、insteadOf/pushInsteadOf、core.sshCommand/hooksPath、
  credential.*、http(s).proxy、extraheader、submodule.*.update →
  `REPO_CONFIG_REJECTED`；二进制/超 1 MiB 拒绝）；每次 git argv 追加
  `-c gc.auto=0`（同步进 git_safe 预设）。
- **D13 compose 插值钉死文件**：`compose_env_file(config_dir)` 唯一来源
  （三处字面量归一）；`configs/compose/empty.env` 随仓交付、init-config 生成；
  生产 preflight 缺失即 `CONFIG_INVALID`（变更前干净失败），simulation
  preflight 覆写为磁盘/基线检查；selfcheck runner 视角缺失 FAIL；
  check_project_config.sh 同源解析；gitconfig/ssh-wrapper 交付步骤与 WARN。
- **D16 BuildKit 交接**：`build_output_dir` 必填（缺失 CONFIG_INVALID）；
  buildctl `dest=<交接目录>/<job_id>.tar` → Runner 校验存在/非空 → 复制到
  `jobs/<id>/image.tar` → 大小 + 流式 SHA-256（记入步骤结果与 release
  evidence）→ 清理交接副本 → docker load；selfcheck runner 角色以
  `buildctl --addr <socket> du` 真实探测（失败 FAIL）。

### 2.12 计划 D：运行时模式切换基线语义（D12）✅

- **写入点**：`deploy._finalize` 与 `run_release_rollback` 依
  `environments.<env>.runtime` 写 `releases.simulated`（schema v2 列）。
- **读取点**：`get_current_release(exclude_simulated=)`——plan 创建、apply
  复核、执行前复核、ops_status、ops_history（is_current/rollback_eligible/
  simulated 标注）、ops_test、回滚 current 判定全部按
  `excludes_simulated_releases(runtime)` 过滤；simulation 目标视图不变。
- **入口**：compose 目标上显式回滚 sim release → `INVALID_PARAMETER`（异源
  说明）。
- **快失败**：`step_restore_previous` / `restore_to_release` 在 compose up 前
  `docker image inspect` 基线镜像；缺失（simulated 或人工清理）→ 结构化
  `ROLLBACK_FAILED` 指向人工 reconcile。切换后首次部署自动回到无基线语义
  （失败 → stop_initial → FAILED_NO_BASELINE）。
- **OPERATIONS §8** 新增模式切换章节。

### 2.13 计划 D：准入与可见性（D7/D8）✅

- **D7**：`concurrency.max_read_requests` 落地为诊断通道准入上限（库内
  queued+running 计数、终态自动回收、跨重启保持；饱和 → BUSY 不建 job）；
  `_check_capacity` 过滤 diagnostic（积压不再挤占变更容量）。
- **D8**：`list_releases(before_created_at=)` + releases 游标（复用界检解析）；
  `project_list` 登记 `cursor`（opaque_cursor，默认 "0"=首页）/`limit`
  （1–200，默认 100），handler 严格拒绝非数字 cursor（不再静默回首页）。

### 2.14 计划 D：失败证据与运维体验（D14/D6/D11）✅

- **D14**：七个变更步骤失败点附加 `_failure_evidence`（终止原因/退出码/
  预算内完整 head+tail stderr 环/字节数/log_ref）进 `DrawbridgeError.details`，
  随步骤 detail_json 与 job result `error.details` 客户端可读；原始日志仍
  不上 MCP。诊断根默认改为 `{root}/repos/demo`（原 `deploy_root/current`
  永不创建），缺失根返回结构化 `NO_BASELINE` 指引。
- **D6**：selfcheck `--role {gateway,runner,simulation,all}`（buildless 角色
  SKIP 容器检查）；Python ≥3.12 硬门槛。
- **D11**（11 项）：assert→显式检查、注释 401→403、死校验器移除、Host 剥
  端口括号感知（IPv6）、plan baseline 口径归一（Runner 侧为准）、token/
  successful_releases 文档化、**unraisable 消除**（根因为 ProcessManager
  子进程 transport 未确定性 close，finally 显式 close 后全量 0 警告）、
  queue_expired 两路径补 `job_queue_expired` 审计、runner SIGTERM 优雅停机
  （取消在飞任务 → needs_attention + 审计，退出 0）、validators 预留注记。

### 2.15 计划 D：磁盘与超时（D15）✅

- retention 联动删除 `deploy_root/jobs/<job_id>/`（source.tar/解包源码/
  image.tar；只按删除清单，阻断现场自动保留；历史遗留一次性人工清理已写入
  OPERATIONS §5.3）；`BuildProfileConfig.import_timeout_seconds`
  （1–1800，默认 300）取代固定 120s 的 docker load 预算。

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

## 4. 核查中发现的偏差与缺陷（计划 D 处置结果）

以下为核查发现的"规格声明/文档承诺与实现"缺口及其在计划 D（2026-09-26 实施）中的
处置结果：

1. `ops_history` releases 游标不可用（A 遗留）→ **已修复**（D8，`bd737bc`）；
2. `project_list` 参数与 MVP §4 不一致 → **已对齐**（D8：cursor/limit 登记，
   非法 cursor 严格拒绝）；
3. `config_read` 的"可选 release_id"（MVP §4）→ **裁决为显式降级**（D8）：
   实现固定诊断根（910B 实机已验证该形态可用），历史 release 快照诊断不在
   首版范围；原始设计文档不改，偏差以本条为准；
4. 小项（401/403 注释、store 内 assert、死校验器等）→ **已清理**（D11）；
5. AGENTS.md"已知边界"两条：紧急维护标志矛盾 → **文档已修复**（D10，
   OPERATIONS §1 明示改库后不重启 Gateway）；`step_stop_initial` 归属核对
   缺失 → 仍然成立（保持登记，首次部署失败场景风险低）。

**计划 D 期间新增的登记偏差**（原始设计文档不改，以本节为准）：

- `output.job_log_hard_limit_bytes` / `step_log_soft_limit_bytes`
  **预留未生效**（D9 声明降级）：deploy_verify 至多 3 个 spool 步骤 × 20 MiB
  上界 = 60 MiB，永不触及 100 MiB job 上限；字段保留以维持 schema 稳定，
  排序校验仍生效（模型 docstring 与样例 YAML 已注明）；
- compose `--env-file` 路径由 MVP §3/§4 的 `/etc/drawbridge/compose/empty.env`
  字面量改为**按 `paths.config_dir` 解析**（D13）：语义不变（仍钉死插值、防
  项目目录 `.env` 隐式加载）；标准部署解析结果与字面量一致；
- `check_project_config.sh` 同样由固定 `/etc` 路径改为按 `paths.config_dir`
  解析（D13，同族修正）。

---

## 5. 当前状态

- 计划 D（D0–D16）**全部实施完毕**（2026-09-26，16 个独立提交
  `96a894f`…`1ad45d4`）；质量门三绿：352 passed / 4 skipped、ruff 零告警、
  mypy strict 零告警、**全量回归无警告输出**（unraisable 已消除）。
  逐任务验收留痕（命令/结果/未验收项）见
  [docs/VERIFICATION_RECORD.md](../docs/VERIFICATION_RECORD.md)。
- 910B 实机进度：simulation 运行时从零部署 + MCP 全链路验收（2026-09-22）；
  NPU 只读观测通过。**切生产操作尚未执行**：D1–D4/D12/D13/D16 的 910B
  实机验收条目（双账号 state.db、真实 BuildKit 构建、compose 生产部署、
  `/dev/davinci2` 挂载、模式切换、回滚/中断恢复）按 VERIFICATION_RECORD
  的未验收清单在切生产时逐项执行并回填——切生产前置代码条件已全部就位。
