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
```

## 组件

| 组件 | 说明 |
|---|---|
| `drawbridge-gateway` | 无状态 MCP HTTP 网关：IP 白名单、可选 token、参数校验、排队 |
| `drawbridge-runner`  | 唯一的执行主体：操作白名单、流程模板、隔离构建/测试、恢复 |
| SQLite | 同机状态库：plans/jobs/steps/releases/artifacts/idempotency/events |

状态：开发中。当前实现进度见 `plans/MVP_IMPLEMENTATION_SPEC.md` §10。
