# 验收证据记录（VERIFICATION_RECORD）

> **追加式文档**：每条记录只增不改（发现错误时追加更正条目）。
> 固定节格式：日期 / 环境 / commit / 命令 / 结果 / 证据路径。
> 环境取值：`Windows 逻辑验证` | `910B simulation` | `910B production`。
> 证据路径指向 `var/verification/<UTC 时间戳>/` 下的落盘输出（由
> `uv run scripts/verify.py` 生成），或手工记录的命令输出摘录。

---

## 2026-09-22 — 910B simulation 从零部署与 MCP 全链路验收

- 环境：`910B simulation`（实机 `192.168.73.99`，无 systemd/Docker/root，
  个人目录 `/home/ocr`，代码经 GitHub 同步，配置在 `~/drawbridge-run/etc`）
- commit：见当时运行目录（simulation 时代，早于 `dccc0c2` 基线）
- 命令：`drawbridge-simulate --config-dir ~/drawbridge-run/etc`；
  MCP 客户端按 docs/SIMULATION.md 接入
- 结果：全链路通过 —— initialize / tools_list（11 工具）/ catalog /
  status / git_status / plan / **apply → deploy succeeded**
  （release `70bb6408`）/ logs / compose_status
- 备注：原 `plans/DEBUG.md` 记录，已并入优化计划 D 的输入（§1.3）；
  421 根因（SDK 默认 localhost-only）当时已由 `6d01d97` 根修

## 2026-09-22 — 910B NPU 只读观测（现场事实）

- 环境：`910B production`（只读观测，非部署验收）
- 命令：`npu-smi info`（driver 24.1.rc2）
- 结果：8×910B2 全部 Health OK；卡 2 空闲（无进程，HBM 3396/65536 MB），
  卡 1/4/5/6 vLLM 常驻、卡 3 python、卡 7 paddlex、卡 0 轻载
  —— 切生产设备登记以"卡 2 空闲"为前提（卡 0/1/3/4/5/6/7 为现有负载不动）

## 2026-09-26 — 421 修复三关（历史回溯记录）

- 环境：`Windows 逻辑验证` + `910B simulation`
- commit：`6d01d97`（mirror edge host/origin allowlist into MCP transport
  security）
- 命令：`uv run pytest -q`（涉及 gateway 边界的聚焦用例）
- 结果：修复 SDK 默认 localhost-only 导致的 421；edge 与 MCP transport
  两层 host/origin 判定一致

## 2026-09-26 — 计划 D 实施基线

- 环境：`Windows 逻辑验证`
- commit：`dccc0c2`
- 命令：`uv run pytest -q && uv run ruff check src tests && uv run mypy`
- 结果：三绿（284 passed, 4 skipped；ruff 0 违规；mypy strict 0 错误）
- 证据路径：见本文件后续各任务条目（verify.py 自本次起为唯一留痕来源）

---

## 未验收项（截至本文件创建）

以下能力**尚无任何实机验收记录**，任何"已完成"声明必须等待对应条目
落入本文件：

1. 真实 BuildKit 构建（rootless buildkitd + buildctl 全链路）；
2. compose 生产部署（docker compose up + 健康门禁 + 测试容器）；
3. 设备挂载（`/dev/davinci2` 登记 + NPU DeviceAllow）；
4. 显式回滚（ops_release_rollback 生产路径）；
5. 中断恢复（kill Runner → needs_attention → 人工 reconcile）；
6. systemd 双账号部署（gateway/runner 分账号 + UMask 权限模型）。

---

<!-- 追加区：D0 之后每个任务（D1–D16）交付时在此追加
     “聚焦测试 + 质量门结果”条目。格式：

## <日期> — <任务编号> <标题>
- 环境：…
- commit：<完整或短 SHA>
- 命令：<聚焦测试命令 / verify.py>
- 结果：<通过情况>
- 证据路径：var/verification/<时间戳>/…（或“手工记录”）
-->

## 2026-09-26 — D1 systemd 单元与账号权限补齐

- 环境：`Windows 逻辑验证`（文档级变更；实机验收待 910B systemd 部署）
- commit：`c229a79`
- 命令：无代码路径变化（unit 文件 + 文档）
- 结果：两 unit 增加 `UMask=0007`、runner 增加 `SupplementaryGroups=docker`
  与 DeviceAllow 注释模板；DEPLOYMENT §2 / OPERATIONS §8 同步
- 未验收：双账号 state.db 写权限、`sudo -u drawbridge-runner docker version`、
  systemd 下 `npu_status`——待 910B 切 systemd 时逐项执行并回填

## 2026-09-26 — D2 Dockerfile `# syntax=` 指令防护

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: dockerfile syntax guard）
- 命令：`uv run pytest tests/unit/test_deploy_runtime.py -q`
- 结果：24 passed——表驱动扫描（首行/中部 syntax 识别、escape/check 识别、
  注释误匹配不识别）；拒绝路径断言 buildctl 零调用；escape/check 放行且
  记入步骤结果
- 未验收：910B 上正常 Dockerfile 真实构建成功、含 syntax 构建返回结构化
  错误——待首次真实构建时回填

## 2026-09-26 — D3 Compose 模板指纹冻结 + 结构校验 + plan schema v2

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: compose template fingerprint and plan schema v2）
- 命令：`uv run pytest tests/unit/test_compose_template.py
  tests/unit/test_schema_migration.py tests/unit/test_gateway_service.py -q`；
  全量 `uv run pytest -q`
- 结果：17 项新增聚焦测试通过（digest 注释/键序稳定、语义变化即变、
  token 0/2 与服务集漂移在加载期拒绝、v1→v2 迁移含 simulation 回填、
  未知版本拒绝启动）；apply 五条件 STALE_PLAN（模板变化/存量 NULL 指纹）
  有正反例；全量 314 passed
- 未验收：910B simulation 全链路重跑、改模板（登记设备挂载）后旧 plan
  STALE_PLAN → 重新 plan 执行——待切生产操作时回填

## 2026-09-26 — D4 部署成功路径原子完成

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: atomic success completion for deploy and rollback）
- 命令：`uv run pytest tests/unit/test_deploy_workflow.py -q`；
  全量 `uv run pytest -q`（含 simulate 全链路 plan→apply→ops_test→回滚）
- 结果：注入式回滚测试通过（完成事务内 events 写入失败 → release/事件/
  job 终态全不存在）；正常路径三表落库且事件序不变（release_recorded 先于
  job_finished）；全量 315 passed
- 未验收：910B 正常部署与显式回滚各跑一轮核对 releases/events/jobs 记录
  完整——待切生产操作时回填

## 2026-09-26 — D5 Git 本地 config 危险键扫描 + gc.auto=0

- 环境：`Windows 逻辑验证`（真实 git 子进程，隔离 origin，无网络）
- commit：见本条目对应提交（feat: repo-local git config guard and gc.auto=0）
- 命令：`uv run pytest tests/integration/test_gitops.py -q`；全量
  `uv run pytest -q`
- 结果：25 passed——include/insteadOf 仓库 ls-remote/fetch 被拒且
  RecordingPM 断言零子进程调用；二进制 config 拒绝；扫描表驱动
  （含 includeIf/sshCommand/credential/submodule.update/extraHeader 正例、
  同节良性键负例）；正常仓库全流程通过；`gc.auto=0` 出现在每次实际
  git argv 断言；全量 320 passed
- 未验收：910B 真实仓库正常 fetch——待切生产操作时回填

## 2026-09-26 — D6 selfcheck --role 与 Python 硬门槛

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: selfcheck role scoping and python hard gate）
- 命令：`uv run pytest tests/unit/test_selfcheck_roles.py -q`；
  `uv run python -m drawbridge.entries.selfcheck_main --config-dir configs
  --role simulation`（实跑：5 passed / 1 failed[git 路径，dev 主机预期] /
  3 skipped，容器类检查全部 SKIP 不再 WARN）
- 结果：6 passed——四角色快照差异断言（gateway/simulation 的 skip 计数 >
  all、runner == all）、simulation/gateway 角色退出码 0、Python 基线行
- 未验收：910B 以 simulation 角色自检 0 failed——待切生产操作时回填

## 2026-09-26 — D7 诊断通道准入上限（max_read_requests 落地）

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: diagnostic channel admission cap）
- 命令：`uv run pytest tests/unit/test_gateway_service.py -q`
- 结果：26 passed——灌满 max_read_requests 后新诊断请求 BUSY（retryable、
  retry_after≥1）且不建 job；job 终态后额度回收；诊断积压 5 个不再挤占
  变更容量判定（per_target=2 仍可接纳变更）；既有维护/阻断豁免回归通过
- 实现注记：额度以库内 queued+running 诊断 job 计数（终态即回收，跨
  Gateway 重启保持），优于纯进程内计数器（避免 pending 超时后额度泄漏）

## 2026-09-26 — D8 分页收口（releases 游标 + project_list 参数对齐）

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: pagination closure for history and project_list）
- 命令：`uv run pytest tests/unit/test_retention_and_history.py
  tests/unit/test_builtin_handlers.py -q`；全量 `uv run pytest -q`
- 结果：releases 两页遍历完整性（5 条 limit=2 恰好遍历、newest first、
  无重复）、非法/越界 cursor 拒绝；project_list 以 limit=100 三页遍历 230
  条目（全局有序无重叠）、非数字 cursor 严格拒绝（不再静默回到首页）、
  limit 0/201/-1 拒绝；全量 333 passed
- 参数面：project_list 登记 `cursor`（opaque_cursor，默认 "0"=首页）与
  `limit`（1–200，默认 100），对齐 MVP §4；`config_read` 可选 release_id
  裁决为显式降级（偏差登记于 UPGRADED_ARCHITECTURE §4）

## 2026-09-26 — D11 小项清理批（11 项）

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（chore: review cleanup batch）
- 命令：`uv run scripts/verify.py`（三绿，report.json 落盘
  var/verification/20260926T144348Z/）
- 结果：335 passed，**0 warnings**（unraisable 消除）；逐项 diff：
  ① store 两处 assert → 显式 rowcount/None 检查（INTERNAL）；
  ② simulate_main 注释 401→403；③ SEMANTIC_VALIDATORS 移除
  uuid_record_exists（登记即拒，测试覆盖）；④ Host 解析括号感知剥端口
  （IPv6 字面量正反例 4 条）；⑤ ops_release_plan baseline 以 Runner 侧
  plan 记录为准（gateway 不再二次读取覆盖）；⑥⑦ token 生命周期与
  successful_releases"人工对照参数"落 OPERATIONS/DEPLOYMENT/模型 docstring/
  YAML 注释；⑧ unraisable 根因为 ProcessManager 子进程 transport 未确定性
  close（非 aiosqlite）——finally 中显式 close 后全量回归 0 警告；
  ⑨ queue_expired 两条路径（expire_stale_queue/claim 过期分支）补
  job_queue_expired 审计事件（单事务内）；⑩ runner_main 注册 SIGTERM/
  SIGINT → 停止消费 + 取消在飞任务（CancelledError → needs_attention +
  审计）+ 退出 0；⑪ ValidatorConfig/示例 YAML 注记"登记的 validators 当前
  不执行（预留插件路径）"


## 2026-09-26 — D13 compose empty.env：按 paths.config_dir 解析 + 交付与前置校验

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: resolve compose env pin from config_dir）
- 命令：`uv run pytest tests/unit/test_deploy_runtime.py
  tests/unit/test_reconcile_and_initconfig.py tests/unit/test_selfcheck_roles.py
  -q`；全量 `uv run pytest -q`
- 结果：337 passed——compose_env_file 三种 config_dir 解析断言（/etc 标准
  部署零行为变化）；三处 --env-file 字面量归一（runtime/handlers 两处）；
  check_project_config.sh 路径同源解析；preflight 缺 empty.env → 干净
  CONFIG_INVALID（变更前失败而非恢复失败），交付后通过；init-config 骨架
  含 compose/empty.env；selfcheck runner 视角缺失 FAIL、gateway/simulation
  SKIP、gitconfig/ssh-wrapper/脚本缺失 WARN；模板注释补多服务单镜像锚点写法
- 注记：模板注释中不可出现完整 token 字面量（计数含注释，已用占位描述）；
  simulation preflight 覆写为仅磁盘/基线检查（适配器不触 compose）


## 2026-09-26 — D12 运行时模式切换的基线语义（simulation→compose 迁移）

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: cross-runtime baseline identity）
- 命令：`uv run pytest tests/unit/test_mode_switch.py -q`；全量
  `uv run pytest -q`
- 结果：6 passed + 全量 343 passed——存在 sim release 时切 compose：plan
  基线为 None、恢复走 stop_initial（FAILED_NO_BASELINE 而非
  ROLLBACK_FAILED）；显式回滚 sim release 被拒（INVALID_PARAMETER，信息
  说明异源）；history 中 sim release 不再标 rollback_eligible 且带
  simulated 标记；simulation 目标视图不回归；restore_previous 前的
  Engine 镜像探针快失败（仅 baseline_image_check 执行，无 compose up），
  错误信息指向人工 reconcile
- 未验收：910B 切模式场景（sim 时代 release 不作为 compose 基线/current，
  首部署失败走 failed_no_baseline）——待切生产操作时回填


## 2026-09-26 — D14 失败证据完整化与诊断根陷阱

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: full failure evidence and diagnostics root fix）
- 命令：`uv run pytest tests/unit/test_deploy_workflow.py
  tests/unit/test_builtin_handlers.py -q`；全量 `uv run pytest -q`
- 结果：346 passed——构建失败用例断言 details 含完整 stderr 环（5000 字符
  全量进 step record 与 job result，message 保持 ≤500 单行截断）；诊断根
  不存在时 config_read/project_list 返回结构化 NO_BASELINE（含"首次部署前
  诊断根未就绪"指引）而非 INTERNAL(FileNotFoundError)；骨架与示例
  apps.yaml 的 diagnostics.root 默认改为 {root}/repos/demo（不再指向永不
  创建的 deploy_root/current）
- 未验收：910B 切生产排障演练（失败 details 远程可读）——待切生产操作回填


## 2026-09-26 — D15 job 工作目录回收与镜像导入超时

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: reclaim job work directories, configurable import timeout）
- 命令：`uv run pytest tests/unit/test_retention_and_history.py
  tests/unit/test_deploy_runtime.py -q`；全量 `uv run pytest -q`
- 结果：349 passed——retention 清单联动删除 `deploy_root/jobs/<id>/`
  （source.tar/解包源码/image.tar），阻断 job 工作目录保留，
  compose.rendered.yaml/simulation.log 不触碰；import_timeout_seconds
  模型校验（1–1800，默认 300 取代固定 120）与实际 spec.timeout_seconds
  断言（1700 生效、argv 指向 image.tar）
- 未验收：910B 连续多次部署后确认 jobs/ 仅存未回收项——待切生产操作回填


## 2026-09-26 — D16 rootless BuildKit 产物交接与可访问性探测

- 环境：`Windows 逻辑验证`
- commit：见本条目对应提交（feat: rootless buildkit artifact handover and reachability probe）
- 命令：`uv run pytest tests/unit/test_deploy_runtime.py -q`；全量
  `uv run pytest -q`
- 结果：352 passed——buildctl dest 指向 build_output_dir/<job_id>.tar
  （posix 路径断言）；交接校验链（存在/非空 → 复制 → 大小核对 →
  SHA-256 记入步骤结果与 release evidence → 交接副本清理）有正例；空/缺失
  归档拒绝为 BUILD_FAILED（无运行时副作用）；selfcheck runner 角色以
  buildctl du 真实探测（缺失 socket/探测失败均 FAIL，gateway/simulation
  SKIP）；build_output_dir 必填（缺项 CONFIG_INVALID，apps.yaml/initconfig/
  simulate 三处模板已同步）；PROFILES §2 补 socket 权限与交接属主约定、
  setgid 备选仅文档化；DEPLOYMENT §2 补交接目录创建步骤
- 未验收：910B 真实 rootless 构建一次通过且摘要核对成功（§5 切生产清单
  第 8 步前置）——待实机执行后回填


## 2026-09-26 — D9/D10 补录（纯文档任务）

- 环境：`Windows 逻辑验证`
- commit：`b5658b1`（D9：job 级日志预算声明降级——模型 docstring/样例/
  initconfig 注明"预留未生效"，schema 校验保留）；`4cede2d`（D10：
  OPERATIONS §1 紧急维护路径改为"改库后不重启 Gateway"）
- 命令：`uv run pytest tests/unit/test_config_models.py -q`（39 passed）
- 结果：文档级变更，与代码行为逐句核对通过

## 2026-09-26 — 更正：D1 条目中"OPERATIONS §8"引用

- D1 条目所写"OPERATIONS §8 同步"在 D12 插入模式切换章节后已错位——
  UMask/DeviceAllow 相关说明现位于 **OPERATIONS §9（NPU 节）** 与
  deploy/*.service 本体。UPGRADED_ARCHITECTURE 已同步更正。

## 2026-09-26 — 全量代码审查（三路并行，计划 D 收尾）

- 环境：`Windows 逻辑验证`
- 基线：`dccc0c2..690208b`（计划 D 全部 20 个提交，61 文件 +4710/-1102）
- 方式：三个独立审查通道（状态/网关层、runner 执行层、配置/executor/文档
  一致性）+ 质量门实测；随后逐项核实并修复
- 审查结论：**无 BLOCKER**；AGENTS 十条不变量复核成立（exec-only 固定
  argv、单事务准入、审计只追加、最小暴露面、simulation 非旁路等）。
  发现 3 个 MAJOR + 8 个 MINOR + 若干 NIT，修复如下（commit 见下）：
  1. **派发期阻断缺失**（存量）：claim_next_job 在 BEGIN IMMEDIATE 内
     复查目标阻断状态，已排队变更在 reconcile 前不再派发（诊断豁免）；
  2. **终态覆盖竞态**（存量，D4 放大）：finish_job /
     complete_job_with_release 增加 `status=running` 前置条件——reconcile
     的 needs_attention 裁决不再被滞后 runner 完成覆盖；幻影
     job_finished 事件随 finish_job 布尔返回消除；
  3. **取消的诊断 job 变成阻断态**（D11⑩引入）：取消诊断 → FAILED（只读
     无现场）；诊断 kind 不再参与目标阻断判定；retention 诊断查询补
     阻断豁免；
  4. **外层 deadline 不含恢复预算**（存量）：apply 准入 deadline =
     workflow + recovery 预算；运行时变更已开始后的超时翻
     needs_attention 而非 FAILED；
  5. **spool 打开失败泄漏子进程**（存量）：spool 文件先于 spawn 打开，
     失败返回结构化 start_error，不再有孤儿进程/裸 OSError；
  6. **_execute_deploy 未守护原子完成**：拒绝时降级为日志 + reconcile，
     不再产生未处理任务异常；
  7. 其余：恢复路径取消不再被吞成 ROLLBACK_FAILED（重抛）、git_log count
     边界复查（去 int() 强转）、回滚 compose up 补 spool 日志、MCP 缺参
     KeyError → INVALID_PARAMETER 工具错误（不再协议层 internal error）、
     诊断 queue_expired → QUEUE_TIMEOUT、非 UTF-8 模板 → CONFIG_INVALID、
     artifact 行补 archive sha256/size、retention 条件化简、空 kinds
     守卫、DeviceAllow=char-uart 注释、DEPLOYMENT 补 builder 账号交叉
     引用、simulate fixture 与样例模板对齐 import_timeout_seconds、
     偏差登记扩至四个诊断操作的 release_id/validator
- 修复验证：`uv run pytest -q`（359 passed / 4 skipped）+ ruff + mypy
  strict 三绿；新增 tests/unit/test_review_remediation.py（6 项回归：
  派发期阻断延迟、终态覆盖拒绝、取消诊断非阻断、spool start_error、
  GBK 模板拒绝、MCP 缺参工具错误）
- 附注：uv.lock 在 D2 提交中出现 registry 镜像切换（pypi.org →
  mirrors.aliyun.com，本机 uv 配置所致）；版本与哈希完全一致，
  `uv sync --frozen` 行为不变，判定为无害噪音、保留
- 审查保留项（不修，已登记）：历史时间戳同值行的游标翻页理论跳行
  （keyset 分页固有，测试用 1ms 间隔规避）；诊断 job 无 job_admitted
  事件（与写路径不对称的设计取舍）；_step_budget 使用总预算而非剩余
  （外层 deadline 已界定位真上界）；v2 迁移 evidence LIKE 回填为有限
  启发式（仅存量行、影响面为标记而非安全属性）


## 2026-09-27 — 计划 D 文档归档（原文删除）

- 环境：`Windows 逻辑验证`
- 内容：本地规划文档 `plans/OPTIMIZATION_D_PRODUCTION_CUTOFF_AND_REVIEW_
  FIXES.md`（从未入 git）的全部有留存价值内容已沉淀至受追踪文档并核对了
  交叉引用：
  * 任务实现结论 → UPGRADED_ARCHITECTURE §1 总览表 / §2.11–§2.15 / §4
    偏差登记（前次完成）；
  * §3 非目标与 C8 预构建镜像裁决 → UPGRADED_ARCHITECTURE §6（本次新增）；
  * §5 910B 切生产操作清单 → DEPLOYMENT §11（本次新增，引用改为持久文档）；
  * §6 存量数据与升级语义 → OPERATIONS §7 汇总表（本次新增）；
  * 逐任务验收留痕 → 本文档（D0–D16 + 审查条目均已在档）。
- 处置：按计划 §7.3 约定删除原文（工作区不再保留）；`.git/info/exclude`
  条目同步移除；仓库内无任何指向原文的引用（grep 复核为空）
