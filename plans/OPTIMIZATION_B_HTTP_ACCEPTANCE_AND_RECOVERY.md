# 优化计划 B：通信验收（真实 HTTP）与运行恢复自检

> 状态：已实施（见文末验收记录）。本文档进入 git 追踪。
> 来源：对比 drawbridge_ref 后的自主规划（对比文档 §10 第二阶段清单），
> 聚焦两个仍缺的闭环：**网络栈级别的通信验收**与**异常现场的自动标记**。

## 1. 背景与问题

### 1.1 通信测试停留在进程内

`drawbridge-simulate`（优化计划 A 之前引入）通过 GatewayService 方法调用驱动
全链路——验证了校验/准入/队列/执行语义，但**没有覆盖网络面**：EdgeMiddleware
的 CIDR/Host/Origin/token 判定、MCP Streamable HTTP 协议序列化、uvicorn 传输，
这些只有 `tests/integration/test_gateway_http.py` 的冒烟测试触及，日常无法用
一条命令验收。参考实现 docs/MCP_CLIENTS.md 的"7 步最小验证流程"是手工 curl，
等价自动化缺失。

### 1.2 Runner 崩溃后 running job 永久悬挂

MVP spec §8 要求"Runner 以认领与心跳恢复任务……不明现场进入
NeedsAttention"。当前实现：心跳只写不读；Runner 被 SIGKILL 后其 running
job 永远停在 `running`——`max_running_jobs` 语义上只约束认领，不影响（单
飞行靠 SQLite running 计数），但状态库与真实现场从此不一致，且没有任何
自动标记。对比 review 中这是 ref 的已知缺口（对比文档 §4"心跳/僵死恢复
未实现"），Drawbridge 同样只有心跳写入。

### 1.3 首次部署缺引导

ref 有 `drawbridge init-config`（生成 bootstrap 配置），Drawbridge 没有——
管理员要手工从 configs/ 拷贝并改路径。低风险但高频的摩擦点。

## 2. 方案一：`drawbridge-simulate --http`（真实网络栈验收）

在既有 fixture/场景之上增加 HTTP 模式：

```
drawbridge-simulate --http            # fixture + 真实 gateway + MCP 客户端
drawbridge-simulate --http --config-dir <dir>
```

- **进程内启动完整 Gateway 栈**：`MCPAppFactory` + `EdgeMiddleware` +
  uvicorn（`proxy_headers=False`，与生产一致），绑定 127.0.0.1 随机端口；
  服务器配置按需重写（allowed_cidrs=127.0.0.0/8、allowed_hosts=实际端口）。
- **客户端使用官方 MCP SDK** 的 `streamablehttp_client` + `ClientSession`：
  initialize → tools/list → tools/call，走真实 Streamable HTTP 协议序列化。
- **auth.mode=token 时完整验证**：fixture 生成随机 token 写入文件并改为
  token 模式，客户端带 `Authorization: Bearer`；并做一次**负例**断言——
  无 token 的请求被 401 拒绝（中间件真实生效的证据）。
- 场景为进程内场景的核心子集（catalog/history/plan/apply/status/logs），
  Runner 仍以并发任务消费队列；诊断等待语义与生产一致。
- 复用 `_StepRecorder` 报告结构，`mode: "http"` 标注。

不引入任何新依赖（mcp SDK 客户端已在既有依赖内）。

## 3. 方案二：stale running job 的启动自检（reconcile）

新增 `Store.reconcile_stale_running(...)`：

- 条件：`status='running'` 且（`heartbeat_at` 早于 `now - max_age` 或为空且
  `started_at` 早于同阈值）。
- 动作：翻转为 `needs_attention`（结果 error code `NEEDS_ATTENTION`，消息
  说明"runner 心跳超时，现场需人工核实"）+ `job_reconciled` 审计事件
  （append-only）。**不自动重跑、不自动接管**——与 spec 的"不能直接重跑
  副作用"一致。
- 触发：Runner 首个 tick 立即执行一次，此后与保留清理同节流周期复检。
- 阈值：`main.recovery.stale_running_job_seconds`（默认 900s ≥ 5×心跳间隔
  的 30 倍余量）。新配置段：

```yaml
recovery:
  stale_running_job_seconds: 900   # running 心跳超时 → needs_attention
```

**安全论证**：单实例 flock 保证同机只有一个 Runner；活跃 job 的心跳每 5s
刷新，900s 阈值下误判窗口只在"Runner 存活但某 job 心跳停更 15 分钟"——
这本身已是需要人工关注的异常。多 Runner 部署（远期）需将 owner 纳入判定，
文档标注该前提。

## 4. 方案三：`drawbridge init-config` 引导命令

```
drawbridge-init-config <target-dir> --root /srv/drawbridge [--host 192.168.18.7]
```

- 生成四份 YAML（compose 模式，路径锚定 `--root`）+ Compose 模板 +
  `check_project_config.sh` 占位说明；
- 复用 fixture 生成器的模板方法，但目标是**真实部署骨架**：origin、
  BuildKit socket、测试镜像 ID 留 `REPLACE_*` 占位并在输出中列出手工步骤；
- 幂等：目标目录非空且含任一同名文件时拒绝（不覆盖）。

## 5. 验收标准

1. `drawbridge-simulate --http` 在 Windows 开发机上全绿：真实 HTTP 往返、
   token 正负例、MCP 协议错误信封（isError）可见；`--json` 报告 `mode=http`；
2. stale reconcile：构造心跳超时的 running job → 首个 tick 后变为
   needs_attention、目标被阻断、审计事件存在；心跳新鲜/自有的 job 不受影响；
3. `init-config` 生成的 bundle 能通过 `load_config_from_dir` 加载（digest 可
   计算），覆盖已有文件时拒绝；
4. 全量 pytest / ruff check / mypy 通过（Windows 开发机）。

## 6. 实施与验收记录（2026-09-20）

- `--http` 模式：`entries/simulate_main.py::run_http_scenario`——uvicorn 与
  Runner 同一事件循环（避免跨 loop 锁），MCP SDK 客户端按版本兼容处理
  （2.2.0 的 `is_error`/二元组产出）。token 正负例、initialize/tools/list/
  catalog/history/双部署/logs/回滚 全部走真实 HTTP。CLI 新增 `--http` 与
  `--no-token`。
- stale reconcile：`Store.reconcile_stale_running`（心跳缺失回退 started_at，
  needs_attention + `job_reconciled` 事件）；`Runner._maybe_reconcile_stale`
  首个 tick 立即执行、此后随保留清理节流；新配置 `main.recovery.
  stale_running_job_seconds`（默认 900s）。
- `drawbridge-init-config`：`entries/initconfig_main.py`——operations/
  workflows 原样复制（与代码目录耦合），drawbridge/apps/compose 以 `--root`
  锚定生成，REPLACE_* 占位符与手工步骤打印在输出；目录非空即拒绝覆盖。
- 测试：`tests/unit/test_reconcile_and_initconfig.py`（5 例：翻转/阻断/
  终态豁免/骨架加载/覆盖拒绝）与 `tests/integration/test_http_scenario.py`
  （2 例：token 与无 token 全链路）。Windows 开发机全量 pytest 289 通过、
  ruff/mypy 零告警。
