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
