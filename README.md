# Drawbridge

**Drawbridge — MCP bridge for server operations and deployment.**

面向 AI 编程工具（Codex、Claude Code 等）的服务器操作与部署桥梁。Drawbridge
通过 HTTP Streamable MCP 端点提供一小组"任务型"工具，把 Git、容器编排、健康
检查、日志分页隐藏在受控的 Deployment Module 之后 —— **不提供任意 Shell、
任意 Docker 命令、任意 Git URL 或任意文件路径**。

- 设计文档：[plans/TECHNICAL_DESIGN.md](plans/TECHNICAL_DESIGN.md)
- MVP 实施规格：[plans/MVP_IMPLEMENTATION_SPEC.md](plans/MVP_IMPLEMENTATION_SPEC.md)

## 目标平台

- 部署目标：Linux 服务器（Ascend 910B，aarch64），Python 3.12+，Docker Compose。
- 访问方：同一内网的 Windows/macOS 开发机上的 MCP 客户端。
- 本仓库的开发/测试在任意平台进行；仅逻辑与跨平台测试在非 Linux 上运行，
  Docker/Compose/BuildKit/systemd 相关集成测试需要在目标服务器执行。

## 本地开发

```bash
uv sync                                # 创建 .venv 并锁定依赖
uv run pytest                          # 单元与集成测试
uv run ruff check src tests
uv run mypy
uv run drawbridge-simulate             # 无 systemd 的全链路通信测试（见下）
```

## 组件

| 组件 | 说明 |
|---|---|
| `drawbridge-gateway` | 无状态 MCP HTTP 网关：IP 白名单、可选 token、参数校验、排队 |
| `drawbridge-runner`  | 唯一的执行主体：操作白名单、流程模板、隔离构建/测试、恢复 |
| `drawbridge-simulate` | 命令行通信测试：无 systemd/无容器跑完整 plan→apply→回滚场景 |
| SQLite | 同机状态库：plans/jobs/steps/releases/artifacts/idempotency/events |

## 文档

- [部署手册（910B）](docs/DEPLOYMENT.md) — 账号、目录、systemd、自检、客户端接入
- [运维手册](docs/OPERATIONS.md) — 维护模式、错误码处置、reconcile、清理、审计
- [执行 Profile 与 BuildKit](docs/PROFILES.md) — 低权限账号映射与 rootless 构建接入
- [Simulation 与命令行测试](docs/SIMULATION.md) — 无 systemd 的通信测试、runner 单步/排空模式
- 示例配置：[configs/](configs/)；示例应用：[examples/demo-app/](examples/demo-app/)

## 状态

开发中，按 `plans/MVP_IMPLEMENTATION_SPEC.md` §10 顺序推进：

1. ✅ 配置编译器与受控执行器（严格类型、fullmatch、argv/环境、输出预算、进程组收尾）
2. ✅ SQLite 任务/幂等/队列/锁/维护 + MCP HTTP 网关（IP 白名单、token、Origin/Host）
3. ✅ Git plan（ref 解析/可达性/安全快照）与 deploy_verify 编排（恢复语义）
4. ✅ 发布闭环步骤执行器已按 MVP §5 实现（rootless BuildKit 构建、镜像导入/识别、
   Compose 模板渲染更新、健康门禁、固定测试容器生命周期、恢复/显式回滚、
   ops_test 与审计事件）——**Linux 目标机（910B）专用**：argv 构造以纯函数单测
   覆盖，真实 BuildKit/Docker/Compose 执行需在目标服务器完成验收
5. ✅ Simulation 运行时适配器与命令行通信测试（`drawbridge-simulate`）——
   plan/准入/队列/状态机/快照/渲染全部真实走通，容器动作记录模拟证据；
   Windows 开发机与 910B 均可运行（见 [docs/SIMULATION.md](docs/SIMULATION.md)）
6. 🔶 待在 910B 上完成：真实示例应用的完整发布/回滚/漂移验收（MVP §10 清单）、
   低权限 profile 账号隔离的落地实施（见 docs/PROFILES.md）

注意：operations.yaml 中的 argv 模板目前仅用于参数 schema 声明与配置摘要冻结，
实际 argv 在代码中固定（与 MVP §4/§5 一致）；管理员调整执行行为需修改代码并
通过审核，而非仅改 YAML。

