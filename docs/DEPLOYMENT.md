# Drawbridge 部署手册（目标平台：Linux 服务器 / Ascend 910B，aarch64）

本手册覆盖在 910B 服务器上的完整安装步骤。开发机（Windows，同一内网）只运行
MCP 客户端（Codex / Claude Code），不运行 Drawbridge。

## 1. 前置条件

| 项 | 要求 |
|---|---|
| OS | Linux（aarch64），systemd 可用 |
| Python | 3.12+（安装时用 `python3 --version` 核实） |
| Docker Engine | 已安装并运行；Compose v2（`docker compose version` 验证） |
| Git | 任意近期版本；`git rev-parse --end-of-options` 可用 |
| Rootless BuildKit | 独立 `buildkitd`（rootless 模式），**没有它部署功能整体被阻止**，不能退回宿主机 `docker build` |
| 磁盘 | 至少应用登记的 `disk_budget_bytes` 空闲（含镜像 tar 与 BuildKit cache） |

## 2. 账号与目录规划

```bash
# 专用组与账号：Gateway 无特权，Runner 是唯一部署主体
sudo groupadd --system drawbridge
sudo useradd --system --gid drawbridge -m -d /var/lib/drawbridge-home/gateway drawbridge-gateway
sudo useradd --system --gid drawbridge -m -d /var/lib/drawbridge-home/runtime drawbridge-runner

# Docker Engine socket 组访问：Runner 账号必须加入 docker 组
# （systemd 单元同时设置了 SupplementaryGroups=docker）
sudo usermod -aG docker drawbridge-runner
sudo -u drawbridge-runner docker version   # 必须成功，否则首次 load/ps/up 全部 permission denied

# 目录
sudo mkdir -p /etc/drawbridge/{compose,scripts,empty-hooks} \
              /var/lib/drawbridge/home/{source,runtime,observe,diagnostic} \
              /run/drawbridge/locks /var/log/drawbridge \
              /srv/drawbridge/{repos,origin,apps} /run/drawbridge/buildkit
sudo chown -R root:drawbridge /etc/drawbridge
sudo chmod 2770 /var/lib/drawbridge /run/drawbridge /var/log/drawbridge
# empty-hooks 是管理员维护的空目录（Git 统一前缀引用）

# 构建产物交接目录（plan D16，必填项 build_output_dir 的实体）：
# builder 账号可写、runner 组可读；每个登记环境一个
# （drawbridge-builder 账号的创建见 docs/PROFILES.md §2）
sudo mkdir -p /srv/drawbridge/build-output/demo/staging
sudo chown drawbridge-builder:drawbridge /srv/drawbridge/build-output/demo/staging
sudo chmod 2750 /srv/drawbridge/build-output/demo/staging

# 受控环境文件族（plan D13 交付）：
# compose 插值钉死文件（必交付，缺失时 selfcheck FAIL、preflight CONFIG_INVALID）
sudo cp configs/compose/empty.env /etc/drawbridge/compose/empty.env
# git 全局加固 stub / SSH wrapper / 固定诊断脚本（防御纵深，缺失仅 WARN）
sudo cp configs/scripts/check_project_config.sh /etc/drawbridge/scripts/
sudo install -m 644 /dev/null /etc/drawbridge/gitconfig   # 或拷贝你的加固版本
sudo install -m 755 /dev/null /etc/drawbridge/ssh-wrapper # 仅 ssh origin 需要
```

> **不要把 Gateway 加入 docker 组。** Docker socket 等价主机 root；只有 Runner
> （`drawbridge-runner` 账号）需要访问 `/var/run/docker.sock` 与 BuildKit socket。

> **共享状态目录的文件权限**：`/var/lib/drawbridge/state.db` 及其 WAL/SHM 由
> 先连接的进程创建，Gateway 与 Runner 是共组 `drawbridge` 的两个不同账号——
> 两个 systemd 单元均设置了 `UMask=0007`（文件 0660 / 目录 2770），保证任一
> 服务创建的 db/WAL/SHM 对组可写。手工 `sqlite3` reconcile 时以组内账号执行
> （如既有惯例 `sudo -u drawbridge-runner sqlite3 ...`），避免以 root 生成
> 组不可写的新文件。

## 3. 安装应用

```bash
sudo mkdir -p /opt/drawbridge
# 将仓库同步到服务器后：
cd /opt/drawbridge/Drawbridge
python3.12 -m venv /opt/drawbridge/venv
/opt/drawbridge/venv/bin/pip install .
# 或使用 uv：
# uv sync --frozen && uv pip install --python /opt/drawbridge/venv/bin/python .
```

## 4. 配置

把 `configs/` 下四份 YAML 复制到 `/etc/drawbridge/` 并按环境修改：

```bash
sudo cp configs/{drawbridge,apps,operations,workflows}.yaml /etc/drawbridge/
sudo cp configs/compose/{demo.staging.yaml,empty.env} /etc/drawbridge/compose/
sudo chmod 640 /etc/drawbridge/*.yaml /etc/drawbridge/compose/*
```

必填项核对清单（缺一启动自检失败）：

- `server.allowed_hosts`：填服务器实际 `地址:端口`（如 `192.168.18.7:8787`）；
- `server.allowed_cidrs`：默认 `192.168.0.0/16`，建议收紧到实际内网段；
- `server.allowed_origins`：显式列表，不做通配；
- `apps.yaml`：repo_path / origin / allowed_ref_patterns / platform（910B 填
  `linux/arm64`）/ compose_file / 健康检查 URL / 服务名 / 测试镜像 ID（冻结）/
  buildkit_socket / disk_budget_bytes；
- `/etc/drawbridge/compose/demo.staging.yaml`：管理员模板，业务 env_file 以固定
  路径引用（密钥由管理员手工放置，Drawbridge 不读取不返回）。模板结构受
  强制校验（plan D3）：顶层 `services` 非空、恰好一个 `REPLACE_BY_DRAWBRIDGE`
  image token（多服务共用镜像用 YAML 锚点：`x-image: &img` + `image: *img`）、
  服务名集合与 apps.yaml 的 `services` 完全一致；修改模板会使已排队的 plan
  返回 `STALE_PLAN`（指纹冻结，注释/键序不敏感）。
- **升级注意**：state.db schema v1→v2 在启动时自动迁移（plans 增模板指纹列、
  releases 增 simulated 列）；迁移后存量 plan 一律 `STALE_PLAN`，选择无排队
  job 的窗口重启并在客户端重新 plan→apply。

## 5. 预克隆仓库与测试镜像

```bash
# 裸仓库作为 origin 镜像；业务仓库预克隆并登记 repo_path
sudo git clone --bare <upstream-url> /srv/drawbridge/origin/demo.git
sudo git clone /srv/drawbridge/origin/demo.git /srv/drawbridge/repos/demo
sudo chown -R drawbridge-runner:drawbridge /srv/drawbridge

# 测试镜像预置到本地 Engine 并冻结 ID（写入 apps.yaml 的 test_runner.image_id）
docker pull <test-image-registry>...
docker tag  <test-image> drawbridge-demo-tests:smoke
docker inspect --format '{{.Id}}' drawbridge-demo-tests:smoke
```

## 6. systemd 单元

```bash
sudo cp deploy/drawbridge-gateway.service deploy/drawbridge-runner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now drawbridge-gateway drawbridge-runner
systemd-analyze security drawbridge-gateway.service   # 验收沙箱强度
systemd-analyze security drawbridge-runner.service
```

Gateway 必须以 `proxy_headers=False` 运行（代码已固定），确保 IP 白名单基于
socket 对端地址而非 `X-Forwarded-For`。不要把 Gateway 放到反向代理之后——
那会改变客户端源地址，白名单会拒绝或误放行。

## 7. 安装自检

```bash
sudo /opt/drawbridge/venv/bin/drawbridge-selfcheck --config-dir /etc/drawbridge
# simulation / 仅网关部署（无 Docker/BuildKit 的环境）：
drawbridge-selfcheck --config-dir ~/drawbridge-run/etc --role simulation
```

`--role {gateway,runner,simulation,all}`（默认 all 保持既有行为，plan D6）：
gateway/simulation 角色把 buildkit socket、docker/compose 检查降为 SKIP 行
（不计入 failed——910B simulation 部署不再被无关 WARN/FAIL 干扰）；runner
保持全检。Python `>=3.12` 是硬门槛（输出 `runtime_baseline: Python >=3.12`，
版本不符直接 FAIL）。

自检真实验证：Python 版本门槛、四份配置加载、状态目录可写（SQLite WAL 需本地
文件系统）、git/docker/compose 可用且版本被记录、rootless BuildKit socket
存在、登记仓库已克隆、磁盘预算余量，以及**操作目录一致性**——operations.yaml
/workflows.yaml 中每个 handler 操作都必须有对应代码实现（executable 操作必须
引用已登记 toolchain），YAML 与代码漂移会在启动自检时直接失败。缺 NPU 工具
只禁用 `npu_status`；缺 BuildKit 阻止部署功能。

维护模式为配置驱动：`drawbridge.yaml` 的 `maintenance.enabled` 在 Gateway
启动时同步到 `control_state`，修改后重启 Gateway 即生效（见 OPERATIONS.md）。

## 8. 客户端接入（内网 Windows 开发机）

```powershell
codex mcp add drawbridge --url http://192.168.18.7:8787/mcp
# 或 Claude Code：
claude mcp add --transport http drawbridge http://192.168.18.7:8787/mcp
```

启用 token 时（`auth.mode: token`，管理员生成随机 token 写入
`/etc/drawbridge/token`，权限 600）。**token 生命周期**：仅在 Gateway 启动时
读取一次（哈希后驻留内存）——轮换 = 写入新文件后重启 Gateway；文件权限 600
是管理员责任，代码不强制校验：

```powershell
claude mcp add --transport http drawbridge http://192.168.18.7:8787/mcp --header "Authorization: Bearer <token>"
```

HTTP 为明文；token 只防同网段误触，不防窃听。跨网段访问先扩大/调整
`allowed_cidrs` 并评估风险。

## 9. NPU（910B）接入约定

- 默认 `npu.enabled: false`，`npu_status` 返回 `UNSUPPORTED`；
- 启用前必须在服务器上验证厂商工具的**只读查询模式**，把确切的
  executable+argv 逐项登记（例如 `npu-smi info`），禁止 `npu-smi .*` 之类
  的通配放行；
- 业务容器使用 NPU 时，在 Compose 模板里登记准确 device 挂载，不使用
  privileged；
- 构建镜像按 `platform: linux/arm64` 进行。

## 10. 验收清单（真实示例应用）

用一个示例应用完成：成功发布 → 修改后再发布 → 健康失败自动恢复 → 测试失败
自动恢复 → 首次部署失败（无基线）→ 镜像缺失恢复失败 → 手工漂移检测拒绝。
参数层用非法输入回归：`;`、换行、`$(...)`、`-c`、URL、revision 表达式、
未登记 ref/service/file、额外字段、数字字符串、超长输入必须全部拒绝。

## 11. 910B 切生产操作清单（simulation → compose）

前置：切生产相关代码交付已全部合入（各项能力的实现核查见
[plans/UPGRADED_ARCHITECTURE.md](../plans/UPGRADED_ARCHITECTURE.md) §2.11–§2.15）。
每一步的实机验收证据按固定节格式追加进
[VERIFICATION_RECORD.md](VERIFICATION_RECORD.md)——未回填即视为未完成。

1. **代码与配置同步**：Windows 侧提交推送 → 服务器 `git pull`（既有同步
   路径）→ `uv sync --frozen` → `uv run scripts/verify.py` 三绿；
2. **权限验证**：state.db/WAL 由组可写；若沿用 nohup 前台运行维持现状，
   切 systemd 时按 `deploy/*.service`（含 UMask=0007）+ 本手册 §2 账号规划
   执行（含 `build_output_dir` 交接目录与 docker 组验证）；
3. **存量 plan 处置**：schema v2 上线后存量 plan（NULL 模板指纹）一律
   `STALE_PLAN`——选择无排队 job 的窗口重启 gateway/runner，客户端重新
   `ops_release_plan`→`ops_release_apply`；
4. **simulation 回归**：`drawbridge-simulate --config-dir ~/drawbridge-run/etc`
   退出码 0 且 `"ok": true`；
5. **参数回调**：`min_deploy_interval_seconds` 0→60；`allowed_cidrs` 收紧到
   实际客户端网段；`diagnostics.root` 按生产布局调整（指向已存在目录）；
   `runtime: simulation`→`compose`；`buildkit_socket` 指向 rootless
   buildkitd；`build_output_dir` 补填（必填项）；以启动日志 `config_digest`
   确认加载；
6. **基础设施**：安装 Docker Engine + Compose v2 + 独立 rootless buildkitd
   （禁 `security.insecure`/`network.host` entitlement、不启用
   no-process-sandbox，见 [PROFILES.md](PROFILES.md) §2）；预置冻结测试镜像
   并回填 `test_runner.image_id`；核对 `paths.config_dir/compose/empty.env`
   存在（selfcheck 已查）；确认 `drawbridge-runner` 已获 docker socket 组
   访问（`sudo -u drawbridge-runner docker version`）；
7. **模板与设备**：Compose 模板登记 `/dev/davinci2` 设备与驱动挂载（不用
   privileged；卡 2 为实机确认的空闲卡，卡 0/1/3/4/5/6/7 为现有负载不动）；
   `npu.enabled: true` 按实机登记 executable/argv（`npu-smi` + `["info"]`），
   并同步 runner unit 的 DeviceAllow（见
   [OPERATIONS.md](OPERATIONS.md) §9）；改模板会使已排队 plan 返回
   `STALE_PLAN`，属预期；
8. **真实构建验收**：首例 `image_build`（syntax 防护与产物交接在位）→
   import/identify → compose up → 健康门禁 → 测试容器 → finalize 漂移核对
   全链路成功，逐条记入 VERIFICATION_RECORD；核对 `compose ps --format json`
   的 `Image` 字段对 `sha256:` 引用原样返回——若被 compose 规范化，先修
   `parse_compose_ps_images` 再继续；
9. **故障路径验收**：健康失败自动恢复（rolled_back）、测试失败自动恢复、
   首次部署失败（failed_no_baseline）、显式回滚、kill Runner 中断后
   needs_attention 与人工 reconcile——逐条留痕；切模式场景必须验证 simulation
   时代 release 不作为 compose 目标的基线/current（首部署失败走
   `failed_no_baseline` 而非 `rollback_failed`，见 OPERATIONS §8）；构建失败
   排障演练确认失败 `details` 可远程读取；
10. **收尾**：`npu_status` 只读观测正常；`ops_history` 三视图与生产数据一致；
    审计事件导出抽查；本手册 §10 验收清单参数层回归（非法输入全拒）。
