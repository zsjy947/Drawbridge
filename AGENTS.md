# AGENTS.md

面向在本仓库中工作的 AI 编程智能体（Codex、Claude Code 等）的指导文件。

Drawbridge 是部署在目标 Linux 服务器（Ascend 910B，aarch64）上的 MCP 桥：
通过 Streamable HTTP 向 MCP 客户端暴露一小组"任务型"工具，把 Git、容器编排、
健康检查与日志分页隐藏在受控执行层之后。**它不提供任意 Shell、任意 Docker
命令、任意 Git URL、任意文件路径**——这是项目的存在理由，任何改动都不得削弱。

## 必读文档

| 文档 | 内容 |
|---|---|
| `plans/TECHNICAL_DESIGN.md` | 架构、威胁模型、发布状态机、并发与锁的设计依据 |
| `plans/MVP_IMPLEMENTATION_SPEC.md` | 首版可编码基线；与技术设计冲突时**以本文件为准** |
| `docs/DEPLOYMENT.md` | 910B 安装、systemd、自检、客户端接入 |
| `docs/OPERATIONS.md` | 维护模式、错误码处置、reconcile、审计 |
| `docs/PROFILES.md` | 执行 profile 与 rootless BuildKit 接入 |
| `docs/SIMULATION.md` | simulation 适配器与无 systemd 的命令行通信测试 |

## 常用命令

```bash
uv sync                                        # 依赖锁定（uv.lock 为准）
uv run pytest -q                               # 全部测试（Windows/Linux 均须通过）
uv run pytest tests/unit/test_x.py -q          # 单文件
uv run ruff check src tests                    # lint（零容忍）
uv run mypy                                    # strict 模式（零容忍）
uv run python -m drawbridge.entries.selfcheck_main --config-dir configs
uv run drawbridge-simulate                     # 无 systemd 全链路通信测试（docs/SIMULATION.md）
```

提交前三项检查必须全绿。测试使用 `asyncio_mode = "auto"`，无需装饰器。

## 代码结构

```
src/drawbridge/
  config/      models.py 严格 Pydantic 配置模型；loader.py YAML 装载 + 交叉校验 + digest
  policy/      params.py 请求参数校验引擎；compiler.py argv 模板编译（见下方"已知边界"）
  executor/    spec.py ExecutionSpec/环境构造；process.py 受控子进程执行器
  state/       schema/store/records SQLite 状态层；locking.py 两层锁
  gateway/     service.py 工具编排与准入；mcp_app.py MCP 协议绑定；middleware.py 边缘
  runner/      loop.py 队列消费；deploy.py 工作流编排与恢复；runtime.py 生产步骤执行器；
               simulation.py simulation 适配器 + RuntimeSelector（按环境选择 adapter）；
               handlers.py 诊断/变更 handler 注册表；builtin.py 纯 Python 诊断；
               health.py 健康门禁；logpage.py 日志快照分页
  fsops.py     安全 tar 解包、no-follow 文件打开
  gitops.py    受控 Git 客户端（统一 git_safe 环境）
  entries/     gateway_main / runner_main / selfcheck_main / simulate_main
configs/       四份 YAML 示例 + Compose 模板 + 固定诊断脚本
plans/ docs/   设计文档与运维手册
```

## 不可破坏的不变量（违反即 review 拒绝）

1. **子进程只用 `asyncio.create_subprocess_exec` + 固定 argv 数组**。任何请求
   字符串不得进入 shell、不得拼接进命令；新增能力走"登记操作 + 代码实现"，
   不走通用解释器/通配放行。
2. **参数只能经 `drawbridge.policy.params` 校验**，且只校验一次于 Gateway 边缘；
   Runner 对到达的参数做防御性边界复查。禁止隐式类型转换、trim、URL decode。
3. **配置只经严格模型**（`extra="forbid"`、strict、safe_load 拒绝重复 key）；
   配置是管理员权威，请求路径上的代码不得放宽模型。改动四份 YAML 的 schema
   必须同步 `config/models.py`、`loader.py` 交叉校验与本文件、docs。
4. **准入是单个 `BEGIN IMMEDIATE` 事务**：幂等去重 → 目标阻断 → 容量 → 冷却 →
   插入。`UNIQUE(plan_id)`、`UNIQUE(idempotency_key)` 是数据库属性，不得绕过。
5. **审计事件只追加**：新的状态转移（job 终态、release、恢复、requeue、计划）
   必须配套 `append_event`；密钥、完整环境变量、原始 stdin 不落审计。
6. **`rollback_failed` / `needs_attention` 阻断目标后续变更**——不存在远程
   "强制忽略"参数；恢复现场只能由管理员按 OPERATIONS.md §3 reconcile。
7. **错误码是外部契约**（`errors.py::ErrorCode`）：新增错误码须同步设计文档
   §12/规格 §7 的清单，工具错误经 MCP tool-error 机制返回，不模拟 HTTP 状态。
8. **部署/恢复的副作用边界**：`runtime_change_started` 持久化先于第一次运行时
   变更；恢复使用独立预算与历史制品，不重新构建旧源码；取消/崩溃的部署进入
   `needs_attention`，不盲目重放。
9. **最小暴露面**：`public=false` 的操作永不能被 `ops_operation_run` 直接调用；
   workflow 步骤只能引用内部操作（loader 校验）；新 public 写操作必须在
   `gateway/service.py::_WRITE_KINDS` 登记显式 job kind（缺失即 CONFIG_INVALID）。
10. **simulation 适配器不是旁路**：`runner/simulation.py` 只替换容器类步骤的
   证据来源，准入/锁/预算/审计/漂移契约与生产同路径；所有模拟证据必须带
   `simulated` / `validation_level: simulation` 标记，不得去掉标记或将模拟
   结果当作生产验证证据。runtime adapter 的选择只来自配置
   （`environments.<env>.runtime`），永远不来自请求参数。

## 平台约定

- **开发/测试机是 Windows**，**部署目标是 Linux（910B）**。跨平台测试必须
  全绿；Linux 专属路径用 `_RUNTIME_LINUX`（或 `sys.platform`）门控并以
  `UNSUPPORTED` 拒绝，绝不假跑。
- **argv 构造写成纯函数**（参照 `runner/runtime.py` 的 `compose_prefix`、
  `buildctl_argv` 等）——纯函数在本机单测断言完整 argv，执行路径在目标机验收。
- 集成测试优先真实子进程（执行器、Git）；不要用 mock 验证超时清理与输出预算。
- 输出/日志预算按原始字节计数；摘要可以容忍无效 UTF-8，解析 Git 输出必须用
  完整预算缓冲（`summary_bytes=max_output_bytes`），不得解析 head+tail 环。

## 已知边界（勿假设其存在）

- `policy/compiler.py` 的 argv 模板渲染当前仅用于 schema 声明与配置摘要冻结；
  实际 argv 在代码中固定。把渲染管线接回执行路径，或删除模板机制，都属于需要
  同步修改 `plans/` 两份文档的显式决策，不要悄悄改一半。
- 低权限 profile（`docs/PROFILES.md` 的独立账号）是目标机部署要求，代码层
  尚无强制；不要在文档之外声称"已隔离"。
- 依赖版本下限以实际使用的 SDK API 为准（当前 `mcp>=2.2`），升级前先在
  staging 跑全量 pytest 与自检。
- `runtime.py::step_stop_initial` 在无已渲染 compose 文件时直接返回
  `{"stopped": False}`，未做规格 §5 要求的"确认容器属于该 job"核对——首次
  部署失败场景下风险低（项目名固定），但严格说核对缺失；补齐前不要声称
  该路径已完成验收。
- 维护模式是配置驱动：Gateway 启动会把 `maintenance.enabled` 覆盖进
  控制记录。运维走 OPERATIONS.md §2 的"紧急改库"路径后，若 Gateway 意外
  重启，紧急标志会被配置值清掉；在紧急路径文档补注"Gateway 重启会覆盖
  此值"之前，救援操作不要依赖跨重启的紧急标志。

## 提交约定

- 信息格式：`feat: ...` / `fix: ...` / `docs: ...`，英文小写祈使句，正文可用
  中文说明动机；一次提交一个完整可验证的变更。
- 涉及行为/语义变化的提交必须同步更新 `docs/` 与（若涉及外部契约）
  `plans/MVP_IMPLEMENTATION_SPEC.md`。
