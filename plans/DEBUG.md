# Drawbridge 模拟部署调试记录（910B / Windows MCP）

> 会话日期：2026-09-22。
> 背景：在昇腾 910B 服务器（aarch64，`192.168.73.99`）上以 **simulation 运行时**从零部署
> Drawbridge——不使用 systemd / Docker / BuildKit / root，全程在个人目录 `/home/ocr` 完成；
> Windows 开发机通过 MCP（Streamable HTTP）远程访问。服务器 22 端口被封，代码经 GitHub 同步。

## 环境快照

| 项 | 值 |
|---|---|
| 服务器 | `192.168.73.99:8787`，代码 `~/Drawbridge`，配置 `~/drawbridge-run/etc` |
| 开发机 | Windows（`E:\project\Drawbridge`），uv + Python 3.12 |
| MCP 注册 | ZCode 用户作用域 `connection` → 910B 网关；`drawbridge`（127.0.0.1，另一项目遗留）未动 |
| 代码版本 | `main @ 6d01d97`（fix: mirror edge host/origin allowlist into MCP transport security） |

---

## 一、Simulation 部署流程（无 systemd，从零到 MCP 可用）

### 1. 前置条件

只需 **Python 3.12+ 与 git**；simulation 模式不需要 Docker、BuildKit、systemd，也不需要
root。系统 Python 过老时在个人目录装 uv（`curl -LsSf https://astral.sh/uv/install.sh | sh`）。

### 2. clone 代码到个人目录

```bash
git clone <repo-url> ~/Drawbridge
cd ~/Drawbridge && uv sync --frozen
```

### 3. 生成配置骨架（不要手改 configs/ 的样例）

`drawbridge-init-config` 按 `--root` 锚定生成全部路径；目标目录必须是 `<root>/etc`，与生成的
`config_dir` 字段自洽；它拒绝覆盖已有文件。**`--host` 必须填服务器真实 IP**（模板默认
`192.168.18.7` 是坑）。

```bash
uv run drawbridge-init-config ~/drawbridge-run/etc \
    --root /home/ocr/drawbridge-run \
    --host 192.168.73.99 --port 8787 --cidr 192.168.0.0/16
```

### 4. 建运行目录 + 准备被部署仓库

配置引用的路径需自己 `mkdir`（个别会自动建，全建最稳妥）。`repo_path` 必须指向
**含 `.git` 的仓库根**，不要在 demo 下再套一层。

```bash
mkdir -p ~/drawbridge-run/{var/lib/home,run/locks,var/log,apps/demo/staging,repos}
git clone <其他项目> ~/drawbridge-run/repos/demo
git -C ~/drawbridge-run/repos/demo remote get-url origin   # 输出填进 apps.yaml 的 origin
```

### 5. apps.yaml 必改处（模拟模式）

- `origin` = 仓库真实 remote URL；
- `runtime: simulation`；
- `buildkit_socket: ""`；
- `test_runner.image_id` 填 `sha256:` + 64 个 0；
- `diagnostics.root` 指向仓库本身（模板默认指向部署 current 目录，首次部署前不存在）。

### 6. drawbridge.yaml 必改处

- `min_deploy_interval_seconds: 0`（模拟连发部署；切生产改回 60）；
- `allowed_hosts` / `allowed_origins` 填真实 `IP:8787`；
- `allowed_cidrs` 加客户端实际到达网段（本次为 `111.111.111.0/24` 与 `10.203.177.0/24`）；
- `toolchain` 仅 `git` 必须真实，docker/buildctl 占位即可（selfcheck 只 warn）。

### 7. 自检 + 链路验收

selfcheck 的 buildkit WARN 是 simulation 预期形态（退出码只看 failed）；
simulate 退出码 0 且 `"ok": true` 即链路通。

```bash
uv run drawbridge-selfcheck --config-dir ~/drawbridge-run/etc
uv run drawbridge-simulate --config-dir ~/drawbridge-run/etc
```

### 8. 无 systemd 启动两个进程

gateway 接 MCP 请求并入队，runner 消费队列执行部署——**不启动 runner 则任务永远 queued**。
`--dev` 仅是日志开关；`--once`/`--drain` 可 cron 式使用；`runner.lock` flock 保证单实例。

```bash
nohup uv run drawbridge-gateway --config-dir ~/drawbridge-run/etc \
      > ~/drawbridge-run/var/log/gateway.out 2>&1 &
uv run drawbridge-runner --config-dir ~/drawbridge-run/etc --dev
```

### 9. 服务器代码更新（22 端口被封）

Windows 提交推送 GitHub，服务器 `git pull`（服务器仓库树保持干净，配置都在仓库外）。

```bash
cd ~/Drawbridge && git pull
pkill -f drawbridge-gateway; sleep 1
nohup uv run drawbridge-gateway --config-dir ~/drawbridge-run/etc \
      > ~/drawbridge-run/var/log/gateway.out 2>&1 &
tail ~/drawbridge-run/var/log/gateway.out   # 看 config_digest 变化确认新配置已加载
```

### 10. 启用 npu_status（只读可见性）

`npu.enabled` 默认 false（返回 UNSUPPORTED 是刻意设计）。先在服务器核实 `npu-smi` 只读
用法，再登记精确 executable+argv（**禁止通配**）；enabled 时 vendor/model/executable/argv
全部必填。配置是 per-app 的，gateway 与 runner 都要重启。

```bash
ls /usr/local/Ascend/driver/tools/npu-smi /usr/local/sbin/npu-smi 2>/dev/null
npu-smi info
```

```yaml
# apps.yaml 对应 app 下：
npu:
  enabled: true
  vendor: huawei
  model: "910B"
  driver: "24.1.rc2"                                     # npu-smi info 顶部版本
  executable: /usr/local/Ascend/driver/tools/npu-smi     # 实际路径
  argv: ["info"]
```

### 11. MCP 客户端接入

ZCode 用户作用域 `~/.zcode/cli/config.json` 的 `mcp.servers` 加
`connection → http://192.168.73.99:8787/mcp`；与另一项目的 `drawbridge`（127.0.0.1）命名隔离。
**同名时用户作用域覆盖工作区作用域，改名是必须而非偏好。**

---

## 二、遇到的问题与手动解决措施

### P1：simulate 全场景 ok:false —— config_read 报 FileNotFoundError

- **现象**：`INTERNAL: No such file or directory: '.../apps/demo/staging/current'`（首次部署之前）。
- **根因**：模板的 `diagnostics.root` 指向部署 current 目录，首次部署成功前不存在；且登记的
  `config/app.json` 在 clone 的真实项目里也不存在。**JSON 不是系统要求**：`config_files`
  只是可选的文件别名白名单，`raw: true` 可登记任意文本文件，不登记只是该诊断不可用。
- **解决**：`diagnostics.root` 改指仓库本身；登记项目里真实存在的文件；或接受场景里这一步
  失败（不影响部署链路）。

### P2：第二次部署被拒 —— RATE_LIMITED "deploy cooldown has not elapsed"

- **根因**：模板默认 `min_deploy_interval_seconds: 60`，场景 1 秒内连发两次。恰好证明冷却
  准入在工作，不是故障。
- **解决**：改 `min_deploy_interval_seconds: 0`（模拟调参）。

### P3：Windows 直连网关 403 Forbidden

- **根因**：开发机实际 IP 为 `10.203.177.57` / `111.111.111.244`（另有 172.23 虚拟网卡），
  均不在 `allowed_cidrs` 默认的 `192.168.0.0/16` 内，边缘中间件按 socket 对端地址拒绝。
- **解决**：`allowed_cidrs` 增加客户端真实网段；服务器上 `ss -tn` 可核对对端地址后收紧。

### P4：改完白名单仍 421 "Invalid Host header"（本次会话最深的坑）

- **根因**：421 不是 Drawbridge 产生的（全仓库搜不到该响应串），而是 **MCP SDK 的 DNS
  rebinding 防护**：`build_asgi_app()` 未传 `transport_security` 时，SDK 因默认
  `host="127.0.0.1"` 自动启用 localhost-only Host 校验，所有局域网 Host 在边缘中间件之前
  就被 421（mcp 2.2.0 `lowlevel/server.py` 自动启用逻辑 + `transport_security.py`）。
  文档承诺的内网直连在旧代码下从未走通。曾误判为 allowed_hosts 配置错误，后以
  "全仓搜不到该响应串"证伪并更正。
- **解决**：代码根修（commit `6d01d97`）：`mcp_app.py` 新增 `_transport_security()` 把边缘
  白名单（allowed_hosts 小写化、allowed_origins 原样）镜像进 `TransportSecuritySettings`，
  并传 `host=bind_address`——**保留 SDK 层防护而非关闭它**；新增 `tests/unit/test_mcp_app.py`。
  三关全绿：pytest 284 passed / ruff / mypy strict。
- **为什么不绕行**：SSH 隧道（Host 变 127.0.0.1）本可绕过但 22 端口被封；反向代理改写 Host
  被设计禁止（`proxy_headers=False`，CIDR 判 socket 对端，代理会掩盖真实客户端地址）。

### P5：配置改了但不生效

- **根因**：配置只在进程启动时加载一次，无热加载；旧进程继续用旧白名单。
- **解决**：`pkill -f drawbridge-gateway` 后重启；用启动日志的 `config_digest` 确认新配置
  被加载。注意确认旧进程真没了再启动，避免端口占用。

### P6：流程性教训 —— "已删除"的声明与实际不符

会话中曾声称已删除临时脚本 `tmp_mcp_acceptance.py`，但删除命令未真正执行，事后
`git status` 发现残留。**教训：声称动作前先有命令佐证，收尾用 git status 复核工作区。**

---

## 三、会话中提出的问题与结论

**Q1：不用 systemd 怎么部署？模拟涉及哪些操作？是否涉及系统层面？**
两层：`drawbridge-simulate` 一键通信验收（进程内起完整生产栈）+ 手工 nohup gateway 与
runner `--dev` 常驻。**真实保留**：参数校验/准入/幂等/队列/锁、git ref 解析、`git archive`
源码快照、compose 渲染契约、全部 SQLite 状态机与漂移检测；**仅容器四件套**换成合成镜像
ID、manifest 文件、`simulation.log` 与 simulation 标记证据。系统层面仅用户态：git 子进程、
SQLite WAL（需本地盘）、flock、文件写入、TCP 监听；host 观测类诊断在 Linux 上真实执行。

**Q2：从零开始，代码能 clone 到个人目录 /home/xxx 吗？**
能。simulation 全用户态，`/etc/drawbridge` 与专用账号是生产路径的要求；唯一硬性条件是配置
内路径全部为运行账号可写的绝对 POSIX 路径（state_dir 须在本地文件系统，NFS 家目录时挪走
state/locks）。

**Q3：state/ locks/ logs/ deploy/ 与 compose/demo.staging.yaml 是什么？要自己建吗？**
它们是配置引用的路径：state_dir（SQLite）、lock_dir（flock）、log_dir、deploy_root（release
快照）、compose_file（管理员维护的部署模板，部署时仅替换 image token，模拟模式也会真实读取
渲染）。自己 mkdir 一次最稳妥；配置骨架用 `drawbridge-init-config` 生成。

**Q4：clone 其他项目作为仓库放哪？demo 下开子目录吗？**
直接作为 `repos/demo` 本身：repo_path 必须是含 `.git` 的仓库根。clone 自带真实 origin 直接填
apps.yaml；默认分支非 main 时要在 `allowed_ref_patterns` 加对应模式；910B 无外网时 Windows
clone 后整目录（含 `.git`）拷过去即可。

**Q5：selfcheck 警告 buildkit socket missing 会阻塞吗？**
不会。WARN 不影响退出码（只看 failed）；"deployment features blocked" 指的是生产 compose
运行时的构建能力，simulation 从不调用 buildctl，这正是模拟部署的预期形态。

**Q6：为什么项目里必须有 JSON？为什么不能动态注册项目？测新项目必须停服重启吗？**
不必须有 JSON（见 P1）。动态注册被拒绝是项目的存在理由：**配置是管理员权威（不变量 3）**，
提供注册工具等于让 MCP 客户端自己扩任意 Git URL/路径白名单。注册新项目要改 YAML 并重启
gateway+runner，但状态全在 SQLite，重启不丢队列；在 `active_job: null` 时操作，秒级完成；
apps.yaml 可一次登记多个项目，日常换分支/换项目部署无需重启。

**Q7：`drawbridge-runner --dev` 是干嘛的？**
启动队列消费者 Runner：领 job、执行 deploy_verify 工作流（快照/模拟构建/落 release/健康
检查/测试/回滚）。gateway 只校验入队不执行；`--dev` 仅日志开关；默认前台常驻即 systemd 的
替代，另有 `--once`/`--drain` cron 式用法；flock 保证单实例（重复启动返回码 3）。

**Q8：421 问题不能通过转发避免吗？**
SSH 隧道可以（客户端连 127.0.0.1，Host 头即合法），但本服务器 22 端口被封；反向代理改写
Host 被架构禁止——代理改变 socket 对端地址，CIDR 白名单将看到代理 IP 而非真实客户端。
故修代码是正解（见 P4）。

**Q9：另一项目提示"无法读取 NPU 设备"，建议启用 npu_status 并注册支持卡选择的工作流？**
`npu_status` 本就是登记好的操作，UNSUPPORTED 仅因 `npu.enabled` 默认 false；按登记要求开启
即可（见流程第 10 步）。但要分清：**npu_status 只是只读可见性**；"部署到空闲卡"不存在请求级
选卡参数（也不该有）——选卡是管理员在 Compose 模板登记 device 挂载（`/dev/davinciN` + 驱动
挂载，不用 privileged），且 simulation 模式不部署任何实物；真部署须切 `runtime: compose`
生产路径（Docker + rootless BuildKit）。

**Q10：ZCode 里配置 MCP 如何与另一项目的 drawbridge 不混淆？**
同名服务用户作用域覆盖工作区作用域，因此不能重名：最终方案是把 910B 以新名 `connection`
注册到用户作用域（全局可用），删除临时的工作区级 `drawbridge-910b`；旧 `drawbridge`
（127.0.0.1）原样保留。工具前缀 `mcp__connection__ops_*` 一眼可辨。

---

## 四、会话结束时的状态

- **服务**：gateway(nohup) + runner(--dev) 运行于 192.168.73.99，配置 `~/drawbridge-run/etc`。
- **代码**：`main @ 6d01d97`（已推送 GitHub，服务器 git pull 同步）。
- **已验收**：MCP 全链路通过——initialize / tools_list(11 工具) / catalog / status /
  git_status / plan / apply / **deploy succeeded**（release `70bb6408`）/ logs / compose_status。
- **NPU**：npu-smi 24.1.rc2，8× 910B2 全部 Health OK。**卡 2 空闲**（无进程，HBM
  3396/65536 MB），卡 0 轻（python 2.7GB）；卡 1/4/5/6 vLLM 常驻、卡 3 python、卡 7
  paddlex——即"不能动的现有映射"。
- **下一步（切生产）**：真部署到空闲卡 2 —— 切 `runtime: compose`，装 Docker + rootless
  BuildKit，Compose 模板登记 `/dev/davinci2` 设备挂载并按约定新增服务实例。
