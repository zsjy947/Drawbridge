# Simulation 模式与命令行通信测试

本手册覆盖两件事：

1. **simulation 运行时适配器**——`environments.<env>.runtime: simulation`，让
   deploy_verify 全流程（plan → 队列 → 步骤 → release → 回滚）在没有
   Docker/BuildKit/systemd 的机器上真实走完；
2. **不采用 systemd 的命令行操作与测试方式**——`drawbridge-simulate`、
   `drawbridge-runner --once/--drain`。

设计依据：技术设计 §4 的 Runtime Module seam（可替换 adapter）。该功能借鉴了
参考实现 drawbridge_ref 的 simulation 部署模式，并按本仓库的配置驱动架构重新
实现。

## 1. 什么的真实、什么是模拟的

| 环节 | simulation 模式下的行为 |
|---|---|
| 参数校验 / 准入 / 幂等 / 队列 / 锁 / 维护 | **真实**（与生产完全同一代码路径） |
| Git ref 解析与可达性（plan 阶段） | **真实**（需要本机 git） |
| 源码快照（`git archive` + 安全解包） | **真实**——release 目录里确实有冻结 commit 的文件 |
| Compose 模板渲染 | **真实**（单 token 替换为合成镜像 ID，同一渲染契约） |
| SQLite 状态机（plans/jobs/steps/releases/events） | **真实** |
| 构建 / 导入 / 识别（buildctl、docker load） | **模拟**：合成 `sha256:…` 镜像 ID（commit+job 派生）+ manifest 文件 |
| compose up / restart | **模拟**：不创建容器；写 `simulation.log` 合成应用日志 |
| 健康检查 / 测试容器 | **模拟**：证据带 `validation_level: simulation` |
| 漂移检测（finalize） | **真实**：核对渲染后的 compose 文件确实引用本 release 的镜像 ID |
| host_metrics / process_list / check_project_config | 不受 runtime 影响——仍为 Linux 目标机专属（`UNSUPPORTED`） |

**红线**：simulation 产物（release 记录、health 证据、日志）一律带
`simulated: true` / `validation_level: simulation` 标记，**不得作为生产验证
证据**；它验证的是通信链路、状态机与准入语义，不是应用本身。

## 2. drawbridge-simulate：一键通信测试

```bash
# 生成隔离 fixture（临时 git 仓库 + simulation 配置）并跑完整场景，任何有
# Python + git 的机器可用（含 Windows 开发机）：
uv run drawbridge-simulate

# 常用参数：
uv run drawbridge-simulate --keep                 # 保留 fixture 目录便于检查
uv run drawbridge-simulate --workdir ./sim-run    # 指定 fixture 目录
uv run drawbridge-simulate --config-dir <dir>     # 对既有配置运行（见下）
```

fixture 模式从仓库 `configs/` 复制 operations/workflows 两份目录（可用
`--source-config-dir` 覆盖），自动生成 `drawbridge.yaml` / `apps.yaml`（含一个
`runtime: simulation` 的 demo 应用、`min_deploy_interval_seconds: 0`）与 Compose
模板，并 `git init` 一个最小 demo 仓库。

场景覆盖（每步都是对 GatewayService 的真实调用，Runner 作为并发任务在同一
进程内消费共享 SQLite 队列，等价于服务器上的两个 systemd 进程）：

```
ops_catalog → ops_status → ops_logs → git_status → config_read
→ plan → apply → deploy#1 → ops_logs(有日志) → compose_status
→ plan → apply → deploy#2 → ops_test → service_restart → rollback
```

stdout 输出最终 JSON 报告（`ok` / 每步状态 / releases / 当前 release），进度
日志走 stderr；退出码 0 表示全部关键步骤达到预期。非 Linux 主机上
`ops_status` 的 host_metrics 返回 `UNSUPPORTED` 属预期行为，报告会标注
"UNSUPPORTED envelope verified"。

集成测试 `tests/integration/test_simulate_flow.py` 复用同一入口做 CI 回归。

## 3. 对既有配置运行

`--config-dir` 模式要求所选 app/environment 声明 `runtime: simulation`：

```yaml
# apps.yaml
environments:
  staging:
    runtime: simulation      # 默认 compose；simulation 不触碰 Docker
```

其余配置（操作目录、workflow、诊断别名）与生产一致——这就是它的价值：
同一套登记配置可以在沙箱环境先验证语义，再切回 `compose` 上目标机。

## 4. 无 systemd 的手动操作

Runner 本身不依赖 systemd，以下方式在任何裸机上可用：

```bash
# 单步消费队列一次（类似 cron 调度）
drawbridge-runner --config-dir /etc/drawbridge --once

# 排空队列后退出（有限时，默认 1800s）
drawbridge-runner --config-dir /etc/drawbridge --drain

# 前台常驻（调试时替代 systemd）
drawbridge-runner --config-dir /etc/drawbridge --dev
```

`--once/--drain` 与 systemd 常驻实例可以共存：跨进程目标锁与 SQLite 单飞行
约束仍然生效，重复实例由 `runner.lock` flock 拒绝。

## 5. 开发机（Windows）注意事项

- 配置模型接受 Windows 盘符绝对路径（`D:/...`），用于本地 simulation 配置；
  生产路径仍以 POSIX 绝对路径为准。
- fixture 的 git 仓库带 `.gitattributes`（`* -text`），避免 CRLF 导致
  `git_status` 误报 dirty。
- simulation 路径不需要 Docker Desktop；`toolchain.docker` 等路径不会被调用。
