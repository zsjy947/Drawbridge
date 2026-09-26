# Drawbridge 运维手册

面向在 910B 服务器上运维 Drawbridge 的管理员。所有命令假定配置位于
`/etc/drawbridge`。

> 无 systemd 环境或开发机上，Runner 可用 `--once`（单步）/`--drain`（排空）
> 前台运行；通信链路的命令行验证见 [SIMULATION.md](SIMULATION.md)。
>
> Runner 启动时会自动把心跳超时（`recovery.stale_running_job_seconds`，默认
> 900s）的 running job 标记为 `needs_attention` 并写 `job_reconciled` 审计
> 事件——崩溃现场从不自动重跑，按 §3 reconcile。

## 1. 日常操作

### 查看状态

```bash
systemctl status drawbridge-gateway drawbridge-runner
journalctl -u drawbridge-runner -f          # 结构化 JSON 日志
sqlite3 /var/lib/drawbridge/state.db "SELECT job_id,kind,status,queued_at,finished_at FROM jobs ORDER BY queued_at DESC LIMIT 20;"
```

### 维护模式

维护模式同时约束 Gateway 准入与 Runner 派发；只读工具不受影响。
**推荐流程是配置驱动**（与 `drawbridge.yaml` 的 `maintenance.enabled` 一致）：

```bash
# 进入维护：改配置后重启 Gateway，控制记录随启动同步
sudo sed -i 's/^  enabled: .*/  enabled: true/' /etc/drawbridge/drawbridge.yaml   # maintenance 段
sudo systemctl restart drawbridge-gateway
# 退出维护：置回 false 并重启 Gateway
```

Gateway 启动时会把 `maintenance.enabled` 写入 `control_state` 控制记录；
Runner 每次派发前重查该记录。紧急情况下也可以直接改库（等效于老流程）：

```bash
sudo -u drawbridge-gateway sqlite3 /var/lib/drawbridge/state.db \
  "UPDATE control_state SET value='true' WHERE key='maintenance';"
sudo systemctl restart drawbridge-gateway
```

若控制记录不可读（IO 错误），Runner 停止一切派发（fail closed）；排队任务
保留但 `queue_timeout` 继续计时，超时后进入 `queue_expired`，未执行任何变更。

### 配置变更

1. `maintenance=true`，排空运行中任务（含恢复逻辑完成）；
2. 修改 `/etc/drawbridge/*.yaml`；
3. `drawbridge-selfcheck` 通过后重启 Gateway 与 Runner；
4. 配置摘要（digest）变化后，**旧的 plan 全部失效**（apply 返回
   `STALE_PLAN`），客户端需重新 `ops_release_plan`。

**Compose 模板同权**（plan D3）：模板是四份 YAML 之外的第五个配置权威。
修改 Compose 模板（如登记设备挂载）后，**已排队的旧 plan 同样失效**
（`STALE_PLAN`，模板指纹不符）；且模板结构在配置加载、plan、apply、执行前
四处校验——顶层 `services` 非空、恰好一个 `REPLACE_BY_DRAWBRIDGE` image
token、服务名集合与 apps.yaml 登记一致，违反即 `CONFIG_INVALID`。模板指纹
对注释/空白/键序不敏感（规范化 JSON + SHA-256），只对语义变化生效。升级到
schema v2 前的存量 plan（无指纹）一律 `STALE_PLAN`，需重新 plan→apply。

## 2. 错误码与处置

| 错误码 | 含义 | 处置 |
|---|---|---|
| `INVALID_PARAMETER` | 参数形状/语义不通过 | 客户端修正；无需运维 |
| `UNKNOWN_OPERATION` / `FORBIDDEN_OPERATION` | 未登记或内部操作 | 检查操作目录 |
| `IDEMPOTENCY_CONFLICT` | 同键不同内容 | 客户端换新幂等键 |
| `BUSY` / `RATE_LIMITED` | 队列满 / 部署冷却未到 | 按 retry_after 等待 |
| `QUEUE_TIMEOUT` | 排队超时未执行 | 重新提交（新 plan 若过期） |
| `STALE_PLAN` | 计划过期/基线变化/配置变化/模板指纹变化 | 重新 plan |
| `DRIFT_DETECTED` | 人工改动与登记基线不一致 | 核实现场后 reconcile |
| `BUILD_FAILED` | 构建失败 | 看 job 日志（spool 目录） |
| `BUILD_UNSUPPORTED_FRONTEND` | Dockerfile 含 `# syntax=` 自定义前端指令 | 移除该指令后重新提交（离线目标无法拉取自定义 frontend；不可重试） |
| `REPO_CONFIG_REJECTED` | 登记仓库的 `.git/config` 含危险键（include/insteadOf/代理等） | 以 `drawbridge` 组内账号清理 `<repo>/.git/config` 后重试；错误信息列出命中键 |
| `VERIFY_FAILED` | 健康门禁失败 | 自动恢复后检查恢复结果 |
| `ROLLBACK_FAILED` / `NEEDS_ATTENTION` | 恢复失败 / 现场不明 | **人工核实现场**，见下节 |
| `MAINTENANCE` | 维护模式 | 结束维护后重试 |
| `UNSUPPORTED` | 功能在当前主机不可用（如 NPU 未登记） | 按需登记或忽略 |

**构建约束（受控构建契约）**：业务仓库的 Dockerfile 只允许默认
`dockerfile.v0` 前端。`# syntax=<image>` 指令会使 buildkitd 从 registry 拉取
自定义前端——离线目标必然失败且报错误导，可达时也构成绕过受控构建的供应链
入口，因此在 Runner 侧、任何 buildctl 调用之前直接拒绝
（`BUILD_UNSUPPORTED_FRONTEND`）。`# escape=` / `# check=` 为前端内建行为，
放行并记入构建步骤结果。

## 3. NeedsAttention / RollbackFailed 的现场核实与 reconcile

出现这两类状态时，`admit` 会**拒绝该目标的一切新变更**（部署/测试/重启/
回滚均返回 `NEEDS_ATTENTION`；只读查询与诊断不受影响），没有远程
"强制忽略"开关。处理流程：

1. 查明阻塞 job：`sqlite3 /var/lib/drawbridge/state.db "SELECT job_id,kind,status,finished_at FROM jobs WHERE status IN ('needs_attention','rollback_failed');"`
2. 人工核实容器现场：`docker compose --project-name <project> ps`、
   `docker inspect`；比对当前运行镜像与 `releases` 表记录；
3. 修复现场到已知状态（例如手工恢复到期望镜像，或重新渲染
   `deploy_root/compose.rendered.yaml` 指向期望镜像后 `compose up`）；
4. 在本机（服务器上）执行受控 reconcile——把核实结论落到状态库，解除阻塞：
   ```bash
   sudo -u drawbridge-runner sqlite3 /var/lib/drawbridge/state.db \
     "UPDATE jobs SET status='failed' WHERE job_id='<id>' AND status IN ('needs_attention','rollback_failed');"
   # 同时追加审计事件（replace <id>/<结论>）：
   sudo -u drawbridge-runner sqlite3 /var/lib/drawbridge/state.db \
     "INSERT INTO events(ts,kind,job_id,detail_json) VALUES(strftime('%s','now'),'manual_reconcile','<id>','{\"conclusion\":\"<核实结论>\"}');"
   ```
   解除后如需登记"当前实际运行版本"为新基线，用新的 `ops_release_plan` +
   `ops_release_apply` 正常发布一次（它会如实记录镜像与验证证据）；
   不提供任何绕过健康检查的远程参数。
5. `events` 表是 append-only 的全过程审计线索。

## 4. Runner 重启约定

Runner 异常退出后 OS 锁自动释放，但**锁释放不等于旧任务已停止**：

- 重启前核实遗留子进程、BuildKit 会话与测试容器；
- 启动后 Runner 只消费队列，不重放已运行的 job；上次中断且
  `runtime_change_started=1` 的 job 会以 `needs_attention` 呈现，
  要求人工核实现场（compose up 边界已持久化）。

**成功路径的原子性（plan D4）**：部署/回滚成功时，release 行、制品、
`release_recorded` 与 `job_finished` 审计事件、job 终态在**单个事务**内落库
（`Store.complete_job_with_release`）——崩溃不会再产生"有 release 但 job 卡
running"的中间态。`reconcile_stale_running` 仍保留，覆盖的是另一窗口：运行时
变更已开始但完成事务未执行（此窗口内尚无 release 写入，语义不变）。

## 5. 保留与清理

### 5.1 自动保留任务（Runner 内置）

Runner 按 `drawbridge.yaml` 的 `retention.cleanup_interval_seconds`（默认
1 小时）执行一次保留清理，单事务完成并写 `retention_cleanup` 审计事件：

| 对象 | 保留期 | 说明 |
|---|---|---|
| 幂等键 | `retention.idempotency_key_days`（默认 7 天） | 过期后同键请求视为新请求 |
| 过期 plans | `retention.plan_days`（默认 7 天） | 终态且超期的计划记录 |
| 诊断 job 记录 | `diagnostics.retention_seconds`（默认 24h） | 含其 steps |
| 其他终态 job 记录 | `retention.job_record_days`（默认 7 天） | **被 release 行引用的永不清理**；`rollback_failed`/`needs_attention` 永不清理（目标阻断语义优先） |
| job spool 日志目录 | 随对应 job 记录删除 | `log_dir/<job_id>/` |

**永不自动清理**：releases 与 artifacts 行（审计与回滚链）、events（只追加）、
当前/上一成功/回滚引用的制品。镜像实体的回收仍属目标机人工操作——本任务不
触碰 Docker、不删除卷、不执行全局 prune。

### 5.2 历史查询

`ops_history(app, environment, what=releases|jobs|events)` 提供有界（≤50 行）
的目标历史：releases 视图带 `is_current` / `rollback_eligible` 标注，是回滚
选版的工具入口；jobs 与 events 视图支持游标翻页。审计完整性不变——该工具
只读取。

### 5.3 磁盘预算

- 磁盘达到 `disk_budget_bytes` 时拒绝新构建（`DISK_BUDGET_EXCEEDED`），
  preflight 也会在低于保留空间时拒绝构建；
- 可清理：`retention.successful_releases`（默认 5）之外的历史制品（人工，
  按目标机 Docker 流程）。

## 6. 审计

`events` 表只追加：request_id、job_id、release_id、app/environment、
agent_id、时间与脱敏摘要。token 只记哈希；密钥、完整环境变量与原始参数
不落审计。导出示例：

```bash
sqlite3 -json /var/lib/drawbridge/state.db \
  "SELECT * FROM events ORDER BY id DESC LIMIT 100;" > audit.json
```

## 7. 升级

- 锁定实际通过验收的 Git / Docker / Compose / BuildKit / MCP SDK 版本；
- 升级 Python 依赖前先在 staging 目标跑完整 pytest 与自检；
- state.db 使用 SQLite backup API 生成一致快照（不要只复制 db 文件而遗漏
  WAL）；schema 版本不匹配时拒绝启动，先完成迁移与备份。

## 8. NPU 只读观测（systemd 部署注意）

`npu_status` 是只读操作（host_observe profile），但 systemd 沙箱对设备节点
有额外限制：**启用 `npu.enabled` 时必须同步在 `drawbridge-runner.service`
的 DeviceAllow 列表放开登记的设备**（`/dev/davinci_manager` 与各
`/dev/davinciN`），否则 `npu-smi` 在单元内被静默拒绝——nohup/前台部署不暴露
此问题，切到 systemd 后才会失效。修改 unit 后 `systemctl daemon-reload &&
systemctl restart drawbridge-runner`，并以 `npu_status` 实际返回验证。
设备清单只放行 apps.yaml 登记的卡号，不做通配放行（见
[DEPLOYMENT.md](DEPLOYMENT.md) §9）。
