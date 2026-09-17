# Drawbridge 运维手册

面向在 910B 服务器上运维 Drawbridge 的管理员。所有命令假定配置位于
`/etc/drawbridge`。

## 1. 日常操作

### 查看状态

```bash
systemctl status drawbridge-gateway drawbridge-runner
journalctl -u drawbridge-runner -f          # 结构化 JSON 日志
sqlite3 /var/lib/drawbridge/state.db "SELECT job_id,kind,status,queued_at,finished_at FROM jobs ORDER BY queued_at DESC LIMIT 20;"
```

### 维护模式

维护模式同时约束 Gateway 准入与 Runner 派发；只读工具不受影响：

```bash
sqlite3 /var/lib/drawbridge/state.db "INSERT OR REPLACE INTO control_state VALUES('maintenance','true',strftime('%s','now'));"
# 只需重启 Gateway 即可让新请求立即被拒；Runner 每次派发前重查控制记录。
sudo systemctl restart drawbridge-gateway
# 恢复：
sqlite3 /var/lib/drawbridge/state.db "UPDATE control_state SET value='false' WHERE key='maintenance';"
```

若控制记录不可读（IO 错误），Runner 停止一切派发（fail closed）；排队任务
保留但 `queue_timeout` 继续计时，超时后进入 `queue_expired`，未执行任何变更。

### 配置变更

1. `maintenance=true`，排空运行中任务（含恢复逻辑完成）；
2. 修改 `/etc/drawbridge/*.yaml`；
3. `drawbridge-selfcheck` 通过后重启 Gateway 与 Runner；
4. 配置摘要（digest）变化后，**旧的 plan 全部失效**（apply 返回
   `STALE_PLAN`），客户端需重新 `ops_release_plan`。

## 2. 错误码与处置

| 错误码 | 含义 | 处置 |
|---|---|---|
| `INVALID_PARAMETER` | 参数形状/语义不通过 | 客户端修正；无需运维 |
| `UNKNOWN_OPERATION` / `FORBIDDEN_OPERATION` | 未登记或内部操作 | 检查操作目录 |
| `IDEMPOTENCY_CONFLICT` | 同键不同内容 | 客户端换新幂等键 |
| `BUSY` / `RATE_LIMITED` | 队列满 / 部署冷却未到 | 按 retry_after 等待 |
| `QUEUE_TIMEOUT` | 排队超时未执行 | 重新提交（新 plan 若过期） |
| `STALE_PLAN` | 计划过期/基线变化/配置变化 | 重新 plan |
| `DRIFT_DETECTED` | 人工改动与登记基线不一致 | 核实现场后 reconcile |
| `BUILD_FAILED` | 构建失败 | 看 job 日志（spool 目录） |
| `VERIFY_FAILED` | 健康门禁失败 | 自动恢复后检查恢复结果 |
| `ROLLBACK_FAILED` / `NEEDS_ATTENTION` | 恢复失败 / 现场不明 | **人工核实现场**，见下节 |
| `MAINTENANCE` | 维护模式 | 结束维护后重试 |
| `UNSUPPORTED` | 功能在当前主机不可用（如 NPU 未登记） | 按需登记或忽略 |

## 3. NeedsAttention / RollbackFailed 的现场核实与 reconcile

出现这两类状态时，该目标的**后续变更被阻止**（只读查询仍可用），没有远程
"强制忽略"开关。处理流程：

1. 人工核实容器现场：`docker compose --project-name <project> ps`、
   `docker inspect`；比对当前运行镜像与 `releases` 表记录；
2. 修复现场到已知状态（例如手工恢复到期望镜像）；
3. 在本机（服务器上）执行受控 reconcile——把核实后的真实版本登记为基线：
   ```bash
   sudo -u drawbridge-runner sqlite3 /var/lib/drawbridge/state.db \
     "UPDATE jobs SET status='needs_attention' WHERE job_id='<id>';"
   # 记录审计事件；随后解除目标封锁只能通过人工核对 + 新 plan 覆盖，
   # 不提供任何绕过健康检查的远程参数。
   ```
4. 事件表中记录了全过程的审计线索（append-only）。

## 4. Runner 重启约定

Runner 异常退出后 OS 锁自动释放，但**锁释放不等于旧任务已停止**：

- 重启前核实遗留子进程、BuildKit 会话与测试容器；
- 启动后 Runner 只消费队列，不重放已运行的 job；上次中断且
  `runtime_change_started=1` 的 job 会以 `needs_attention` 呈现，
  要求人工核实现场（compose up 边界已持久化）。

## 5. 保留与清理

- 保留：当前 release、上一成功 release、回滚引用制品，永不清理；
- 可清理：`retention.successful_releases`（默认 5）之外的历史制品与
  `job_logs_days`（默认 7 天）前的 job 日志；清理只作用于登记目录与未引用
  制品，**不删除卷，不执行全局 prune**；
- 幂等键保留 7 天，过期后同键请求视为新请求；
- 磁盘达到 `disk_budget_bytes` 时拒绝新构建（`DISK_BUDGET_EXCEEDED`），
  preflight 也会在低于保留空间时拒绝构建。

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
