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
