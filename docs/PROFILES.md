# 执行 Profile 与 Rootless BuildKit 接入说明

执行 profile 不是请求参数，而是**实际的独立账号/隔离执行环境**。仅在
同一个高权限进程里换一个环境变量不构成隔离（MVP 规格书 §1）。

## 1. Profile 一览与账号映射

| profile | 系统账号 | 能力 | 禁止 |
|---|---|---|---|
| host_observe | `drawbridge-observe` | ps、主机聚合指标、登记的 NPU 只读查询 | Docker socket、Git 凭据、业务密钥 |
| source_manage | `drawbridge-runner`（仓库目录属主） | 受控 Git 操作、快照准备 | 执行业务脚本、修改系统配置 |
| project_diagnostic | `drawbridge-diag` | 隔离诊断；源码只读、专用临时目录、默认无网络 | 部署权限、socket、密钥 |
| image_build | `drawbridge-builder`（rootless） | 独立 rootless BuildKit | 宿主机 Engine socket、部署凭据、危险 entitlement |
| runtime_manage | `drawbridge-runner` | 固定 Engine/Compose 操作 | 接受请求提供的 Compose、挂载、容器命令 |
| isolated_test | 容器内固定 uid 65532 | 固定测试镜像与入口 | 部署凭据、socket、宿主机目录、生产密钥 |

systemd 单元的 `ProtectSystem=strict + ReadWritePaths` 是账号之上的第二层：
即使应用层校验被绕过，进程可写范围也由内核强制。

## 2. Rootless BuildKit

```bash
# 以 drawbridge-builder 账号安装 rootless 依赖并启动独立 buildkitd
sudo useradd --system -m -d /var/lib/drawbridge-home/builder drawbridge-builder
sudo loginctl enable-linger drawbridge-builder
# 使用官方 rootless-setup 脚本或手动：
drawbridge-builder$ buildkitd --root ~/.local/share/buildkit \
    --addr unix:///run/drawbridge/buildkit/buildkitd.sock
```

要点（不可妥协，缺一则安装自检失败、部署功能禁用）：

- buildkitd 配置禁止 `security.insecure` 与 `network.host` entitlement；
- 不启用 `no-process-sandbox` 捷径；
- 构建目录采用低权限账户，产物经固定交接目录复制到 Runner 控制路径，
  复制后校验大小/摘要，并确认构建会话与子进程已停止；
- Compose 最终引用**实际不可变的 image ID**，唯一 tag 只用于导入识别，
  不信任可漂移标签；
- 联网构建按管理员的基础镜像/依赖出口策略限制（内网镜像代理优先）。

**交接目录与 socket 权限（plan D16 落地约定）**：

- apps.yaml 的 `environments.<env>.build_output_dir` 是**必填**的固定交接
  目录：属主 `drawbridge-builder`、组 `drawbridge`、模式 2750（builder 可写、
  runner 组可读）。`buildctl --output dest=` 写入 `<dir>/<job_id>.tar`，
  Runner 在 buildctl 成功后校验归档存在/非空 → 复制到
  `deploy_root/jobs/<job_id>/image.tar` → 大小与 SHA-256 校验（摘要记入
  release evidence）→ 删除交接副本 → 才进入 `docker load`；任一步失败即
  `BUILD_FAILED`（变更前路径，无运行时副作用）；
- buildkitd socket 需允许 runner 连接：socket 文件组设为 `drawbridge`、
  模式 660（`srw-rw----`）。selfcheck（runner 角色）以
  `buildctl --addr <socket> du` 真实探测可连接性，失败即 FAIL——文件存在
  但连不上（组/权限错配）会在部署前暴露；
- "共享组 + setgid 共享 BuildKit state"只作为文档化备选，**不作为默认
  实现**——它削弱 builder/runner 的存储隔离边界。

## 3. 隔离测试容器

测试容器生命周期由固定 argv 构成（create/start/wait/logs/stop/rm），
请求无法注入任何参数：`--read-only`、`--tmpfs /tmp:noexec,nosuid`、
`--cap-drop ALL`、`no-new-privileges`、uid 65532、CPU/内存/pids 限额、
专用测试网络。CLI `wait` 返回 0 不代表测试通过——必须解析容器退出码并
核对标签/ID。测试不挂载源码与密钥。

## 4. Git 安全配置

- `/etc/drawbridge/empty-hooks`：空 hooks 目录（`core.hooksPath` 指向）；
- `/etc/drawbridge/gitconfig`：管理员维护的全局 git config；
- `/etc/drawbridge/ssh-wrapper`：固定 SSH wrapper + 严格 known_hosts；
- 预克隆仓库的 `.git/config` 属于可信配置：**代码强制扫描（plan D5）**——
  每次 ls-remote / fetch 前做纯文本键扫描（不执行 git 子进程），命中
  `include`/`includeIf`、`url.<any>.insteadOf|pushInsteadOf`、
  `core.sshCommand`、`core.hooksPath`、`credential.*`、`http.proxy`/
  `https.proxy`/`http.extraheader`、`submodule.*.update` 即拒绝
  （`REPO_CONFIG_REJECTED`，错误信息指出命中键名）；二进制或超过 1 MiB 的
  config 直接拒绝。接入时的人工核对清单仍然保留（origin URL、额外 remote、
  自定义协议覆盖等扫描面之外的项），业务提交与 MCP 请求都不能修改它；
- 所有 Git 命令带统一前缀：`--no-pager`、hooks 关闭、fsmonitor 关闭、
  协议白名单（ssh/https 允许，其余 `protocol.allow=never`）、清空
  credential helper、**`gc.auto=0`**（大仓库 fetch 不允许被 auto-gc 阻塞
  到 120s 预算之外）。
