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

# 目录
sudo mkdir -p /etc/drawbridge/{compose,scripts,empty-hooks} \
              /var/lib/drawbridge/home/{source,runtime,observe,diagnostic} \
              /run/drawbridge/locks /var/log/drawbridge \
              /srv/drawbridge/{repos,origin,apps} /run/drawbridge/buildkit
sudo chown -R root:drawbridge /etc/drawbridge
sudo chmod 2770 /var/lib/drawbridge /run/drawbridge /var/log/drawbridge
# empty-hooks 是管理员维护的空目录（Git 统一前缀引用）
```

> **不要把 Gateway 加入 docker 组。** Docker socket 等价主机 root；只有 Runner
> （`drawbridge-runner` 账号）需要访问 `/var/run/docker.sock` 与 BuildKit socket。

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
  路径引用（密钥由管理员手工放置，Drawbridge 不读取不返回）。

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
```

自检真实验证：Python 版本、四份配置加载、状态目录可写（SQLite WAL 需本地
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
`/etc/drawbridge/token`，权限 600）：

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
