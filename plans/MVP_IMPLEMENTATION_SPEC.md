# Drawbridge MVP 实施规格

本文件将 [技术设计](TECHNICAL_DESIGN.md) 收敛为首版可编码、可测试的操作目录。
状态：拟定实施基线；其中示例应用路径、NPU 型号及测试镜像需要管理员在接入时填写。
不开放通用命令执行，不引入外部 CI/CD；HTTP、Python 3.12+、SQLite、单机 Compose。
若与技术设计中的示例操作有差异，以本文件的 MVP 规则为准。

## 1. MVP 范围与执行主体

- 首批支持单个 staging 应用，结构允许登记多个应用；全局变更并发默认 1。
- 提供查询、配置诊断、计划、部署验证、登记测试、重启和历史制品回滚。
- 不提供 push、pull、checkout、reset、clean、exec 进入业务容器、动态管道或任意脚本。
- 子进程仅由 Runner 管理。Gateway 不执行主机命令；只读命令也经 Runner 的有界诊断通道。
  诊断通道最多并发 16，不占变更队列槽位，诊断请求保留 24 小时，客户端断开不产生变更。
- 执行 profile 不能通过请求选择；低权限工作必须由实际独立账号/隔离执行环境完成，
  不能只在同一个高权限进程里换一个环境变量。受控代理只接收类型化任务，不能接收任意 argv。

| profile | 能力 | 禁止 |
|---|---|---|
| host_observe | 独立低权限账号；ps、主机聚合指标、登记 NPU 查询 | Docker socket、Git 凭据、业务密钥 |
| source_manage | 受控 Git 操作、固定 origin 只读凭据、快照准备 | 执行业务脚本、修改系统配置 |
| project_diagnostic | 隔离诊断；源码只读、专用临时目录、默认无网络 | 部署权限、socket、密钥 |
| image_build | 独立 rootless BuildKit；专用 socket、目录和账户 | 宿主机 Engine socket、部署凭据、危险 entitlement |
| runtime_manage | 可信 Runner 的固定 Engine/Compose 操作 | 接受请求提供的 Compose、挂载、容器命令 |
| isolated_test | 固定测试镜像与入口、应用专用网络、资源限制 | 部署凭据、socket、宿主机目录、生产密钥 |

Runner 本身仍是可信计算基础。上述 profiles 隔离不构成对 Runner 被攻陷的防御承诺。

## 2. 参数与校验规范

统一采用 Pydantic strict、禁止额外字段；不隐式把字符串转整数，不把 bool 当整数。
不自动 trim、URL decode、大小写转换或 shell 拆词；拒绝 NUL、控制字符（reason 也不允许换行）。
管理员正则由 `regex.fullmatch` 执行，单次预算 100ms，规则最长 1024 字符；超时拒绝。
以下正则均为 ASCII。正则是形状检查，不能替代登记范围及权限检查。

| 参数类型/字段 | JSON 类型、默认/上限 | 正则或枚举 | 额外语义规则 |
|---|---|---|---|
| app、operation、workflow、suite、validator、file | string，1–64 | `[a-z][a-z0-9_-]{0,63}` | 必须存在于对应登记表；file 是别名，不是路径 |
| environment | string | MVP 仅 `staging` | 必须属于 app |
| service | string，1–64 | `[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}` | 必须属于 app；重启还须属于 restartable_services |
| source_mode | string，默认 fetch | `fetch`、`local` | local 仍校验可达性，不使用未提交文件 |
| git_ref | string，1–200 | 见下文 | 不接受短分支名、短 SHA、表达式、URL |
| commit_sha | string，40 | `[0-9a-f]{40}` | 仅由解析器产生；MVP 只接入 SHA-1 仓库，SHA-256 仓库启动自检拒绝 |
| count | integer，默认 20 | 1–100 | git_log 条数 |
| limit | integer，默认 100 | 1–200 | 每页结果条数 |
| tail | integer，默认 200 | 1–1000 | 底层日志扫描行数，与返回 limit 分开 |
| since_seconds | integer，默认 300 | 1–86400 | 生成服务端时间戳，不接受任意 Docker 时间字符串 |
| query | string，默认空，最多 128 | 无正则功能 | 字面量过滤，不能下传为 shell/grep 参数 |
| subdir | string，1–200，默认 `.` | `\.` 或 `[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*` | 拒绝 .、.. 路径分段及任何符号链接；必须位于登记的诊断目录 |
| plan_id、job_id、release_id | string，36 | `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` | UUID 解析后查记录、类型、目标与状态，不因格式正确就授权 |
| idempotency_key | string，8–128 | `[A-Za-z0-9][A-Za-z0-9._:-]{7,127}` | 所有非 read 操作必填；同键不同规范化请求返回冲突 |
| reason | string，1–256 | 禁止控制字符 | 重启/回滚必填，审计摘要脱敏 |
| agent_id、parent_task_id | string，可选，1–128 | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` | 仅追踪，不是身份 |
| cursor | string，可选，最多 512 | 不透明服务端生成值 | 服务端绑定查询和快照、过期 10 分钟；不是文件位置或用户路径 |

git_ref 允许的形状（取其一，再与 app.allowed_ref_patterns 做完整匹配）：

```text
refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*
refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*
[0-9a-f]{40}
```

分支/tag 必须额外通过 `git check-ref-format`。SHA 必须为 commit，且可从当前允许的
分支/tag 到达。MVP 默认只允许 `refs/heads/main` 和 `refs/heads/agent/...`；tag 按应用显式开启。
fetch 模式分支映射到 `refs/remotes/origin/<分支>`，local 模式映射到 `refs/heads/<分支>`；
tag 映射到 `refs/tags/<标签>`。可达性检查只用当前模式的允许来源，不混用旧远程跟踪 ref。

## 3. argv 渲染与公共规则

- 程序绝对路径通过管理员 toolchain 登记，启动时验证文件、所属权限、版本及所需 flags。
  表中的 `/usr/bin/git`、`/usr/bin/docker`、`/usr/bin/ps`、`/usr/local/bin/buildctl` 是 Linux 示例。
- 表中 `{param.x}` 来自已验证请求；`{app.x}` 来自管理员配置；`{job.x}`、`{release.x}`
  来自经过 Runner 重验的类型化状态。三者不得互相覆盖。
- 每个模板数组元素产生一个 argv；允许固定前后缀插值，不拆词。禁止任意 list splice；
  多 service 操作由 handler 按登记表生成，不能接收请求中的 argv 数组。
- cwd 只能是登记 repo、服务端 release/job 目录或固定 `/`；请求不传 cwd/env/stdin。
- 默认 accepted_exit_codes=[0]、stdin 关闭、TERM 宽限 5 秒后 KILL 并回收整组进程。
  容器和 BuildKit 任务须同步停止实际隔离任务，不能只杀 CLI。
- 查询输出摘要最多 64 KiB；日志结果单次最多 200 行/256 KiB。
  变更命令 spool 到受保护目录：摘要 64 KiB，单步骤硬日志预算 20 MiB、单 job 100 MiB；
  超硬预算终止并按是否触及运行时恢复。原始归档不通过 MCP 下载。
- 环境从空集合构造；固定 PATH、LANG=C.UTF-8、TZ=UTC。profile 另登记 HOME/socket 等。
  清除 BASH_ENV、ENV、LD_PRELOAD、PYTHONPATH、用户 Git/Docker 覆盖变量。
  Docker 固定 DOCKER_HOST 和 DOCKER_CONFIG，不继承用户 context；Compose 不隐式读取项目 .env。

所有 Git 命令统一前缀，以下表仅列其后缀：

```yaml
executable: /usr/bin/git
argv_prefix:
  - --no-pager
  - -c
  - core.hooksPath=/etc/drawbridge/empty-hooks
  - -c
  - core.fsmonitor=false
  - -c
  - protocol.allow=never
  - -c
  - protocol.ssh.allow=always
  - -c
  - protocol.https.allow=always
  - -c
  - credential.helper=
```

empty-hooks 是管理员维护的空目录。设置 GIT_TERMINAL_PROMPT=0、GIT_CONFIG_NOSYSTEM=1、
GIT_CONFIG_GLOBAL=/etc/drawbridge/gitconfig；SSH 使用固定管理员 wrapper、严格 known_hosts，
不允许请求修改 SSH 选项。HTTPS 需要凭据时另登记管理员固定 helper，不能开放自定义 helper。
预克隆仓库的 `.git/config` 亦属于可信配置：接入时检查 origin URL、禁止额外 URL、
insteadOf、include、代理、外部程序和自定义协议覆盖；不允许业务提交/MCP 修改。
查询 Git log 不开启签名验证、外部 diff、textconv；不递归获取 submodule。

## 4. 可直接调用的操作

这些操作由 ops_catalog 展示；别名工具必须走同一校验和执行路径。

| operation | 参数 | executable / argv（不含程序本身） | profile、cwd | 超时 / access |
|---|---|---|---|---|
| git_status | 无 | Git `["status","--porcelain=v1","--untracked-files=no"]` | source_manage，app.repo_path | 10s / read |
| git_log | git_ref、count | Git `["log","--no-show-signature","--format=%H%x09%ct%x09%s","--max-count={param.count}","{job.resolved_sha}","--"]` | source_manage，app.repo_path | 10s / read |
| process_list | 无 | `/usr/bin/ps` `["-eo","pid,ppid,user,comm,pcpu,pmem","--sort=-pcpu"]` | host_observe，/ | 10s / read |
| npu_status | 无 | 按 §9 登记固定 executable/argv；默认禁用 | host_observe，/ | 10s / read |
| compose_status | 无 | Docker `C + ["ps","--all","--format","json"]` | runtime_manage，release.dir | 15s / read |
| compose_logs | service、tail、since_seconds、limit、query、cursor | Docker `C + ["logs","--no-color","--timestamps","--tail","{param.tail}","--since","{job.since_rfc3339}","{param.service}"]` | runtime_manage，release.dir | 15s / read |
| config_read | file、可选 release_id | 内置 handler；不调用 cat | project_diagnostic，固定源码快照 | 10s / read |
| project_list | subdir、limit、cursor、可选 release_id | 内置 handler；不调用 ls/find | project_diagnostic，固定源码快照 | 10s / read |
| config_validate | validator、可选 release_id | 登记固定校验器；JSON/TOML 首批用内置解析器 | project_diagnostic，固定源码快照 | 15s / read |
| check_project_config | 可选 release_id；无自由参数 | `/bin/bash` `["--noprofile","--norc","/etc/drawbridge/scripts/check_project_config.sh"]` | project_diagnostic，固定源码快照 | 15s / read |
| service_restart | service、reason、idempotency_key | handler：Docker `C + ["restart","--timeout","10","{param.service}"]`，之后健康门禁 | runtime_manage，release.dir | 总计 120s / runtime_write |

所有 app 操作另需要 app/environment。process_list、npu_status 为主机操作，不接受 app；
输出不含完整命令行或环境。主机聚合 CPU/内存/磁盘通过内置只读 handler 获取，不开放自由路径。
git_log 先做本地 ref 解析与允许来源校验，不隐式 fetch；git_status 只说明服务器工作区，
其结果不是部署源码。配置诊断默认针对当前成功 release 的 source snapshot；未部署时
必须指定已存在快照，否则返回 NO_BASELINE。响应始终带 SHA/观测时间。

`C` 是由 handler 生成的固定 Compose 前缀，不是可配置任意参数列表：

```text
["compose", "--ansi", "never", "--project-name", "{app.project_name}",
 "--project-directory", "{release.dir}",
 "--env-file", "/etc/drawbridge/compose/empty.env", "-f", "{release.compose_file}"]
```

empty.env 是管理员控制的空文件，仅防止默认 .env 插值；业务 env_file 仍在可信模板中固定引用。
不执行 `compose config` 并把解析结果回传：它可能解析业务 env_file。
compose_status 输出字段白名单仅服务名、容器 ID、状态、健康、端口；剔除命令字段。
日志先拉取有界快照，再服务端脱敏、字面量过滤和分页，cursor 固定该快照，避免重复重扫造成漏行。
测试/构建原始输出也不保证完全脱敏，不含密钥只是设计目标，不作为强保证。
重启失败不自动重试、不自动发布新镜像；报告 VERIFY_FAILED，记录实际现场。

config_read 单文件原始大小上限 64 KiB；禁止 .env/密钥及未登记文件，普通文件、无符号链接，
通过 dirfd/no-follow 逐段打开，不能仅 realpath 后再普通 open。结构化配置用登记字段白名单展示；
原始文本只允许管理员声明无敏感内容的文件。project_list 仅登记诊断根目录、不递归、单目录
最多扫描 1000 项，超限标记 truncated。业务插件型校验器按 isolated_test 执行，不属于内置 read。

## 5. 仅流程可调用的内部命令

以下 public=false。请求不能直接调用；只有 plan handler 或冻结 workflow 能使用。
repo 锁覆盖 fetch/解析/快照和对允许来源的可达性检查。

| 内部操作 | 固定 argv / handler | 参数来源与效果 | 超时 |
|---|---|---|---|
| source_fetch | Git `["fetch","--no-tags","--no-recurse-submodules","origin","{job.fetch_refspec}"]` | refspec 由允许的完整 ref 生成；分支 `+refs/heads/X:refs/remotes/origin/X`，tag `refs/tags/X:refs/tags/X`；SHA 请求只刷新登记来源集合 | 总计 120s |
| enumerate_remote_refs | Git `["ls-remote","--refs","origin"]` | SHA/fetch 模式先枚举真实远程 refs，完整匹配登记规则后逐项 fetch；只使用本次成功刷新的 tips，不能让已删除分支的残留 ref 授权旧 SHA | 30s |
| ref_format | Git `["check-ref-format","{job.full_ref}"]` | 分支/tag 格式校验，不调用于 SHA | 5s |
| resolve_commit | Git `["rev-parse","--verify","--end-of-options","{job.mapped_ref}^{commit}"]` | 输出必须唯一完整 SHA | 5s |
| check_reachable | Git `["merge-base","--is-ancestor","{job.sha}","{job.allowed_tip_sha}"]` | 至少一个当前允许 tip 返回 0；1 代表不可达，其他码为错误 | 每次 5s，总计 30s |
| source_snapshot | Git `["archive","--format=tar","--output={job.archive_path}","{job.sha}"]` + 安全解包 handler | 只使用服务器生成路径；不 checkout、不执行工作区 hooks/filter | 30s |
| image_build | buildctl，见下文 | 冻结 SHA 的快照、登记 Dockerfile/profile | 900s |
| image_import | Docker `["image","load","--input","{job.image_archive}"]` | 文件由受控构建输出；记录 ID；不信任 CLI 文本为唯一证据 | 120s |
| image_identify | Docker `["image","inspect","--format","{{.Id}}","{job.unique_image_tag}"]` | 唯一 tag 由 app/job 生成，确认导入前不存在、导入后唯一匹配 | 10s |
| compose_deploy | Docker `C + ["up","--detach","--no-build","--pull","never","--wait","--wait-timeout","90"]` | 固定项目和管理员模板；不 down、不 remove-orphans | 120s |
| health_check | 内置 HTTP handler | 固定 URL、无重定向、状态/结构判定 | 总计 90s |
| test_suite | 固定 Docker create/start/wait/logs/stop/rm handler，见下文 | 镜像 ID、suite、专用网络与资源固定 | 300s |
| restore_previous | 历史制品 compose_deploy + health_check + 固定 smoke | 不 rebuild；需要历史模板与镜像均存在 | 独立预算 300s |
| stop_initial | Docker `C + ["stop","--timeout","10"]` | 仅首次无基线，确认项目此前为空且容器属于该 job；不删除卷 | 30s |
| release_preflight / finalize | 内置 handler | 漂移检测、预算检查、事务记录 | 各 15s |

枚举远程 refs 最多 10000 项/2 MiB，允许来源最多 100 个；超限拒绝并要求收紧登记规则，
不静默截断后继续授权。source_fetch 的 120s 是全部 fetch 共享预算，不是每个 ref 120s。
计划期间不运行自动 Git GC；人工仓库清理也须遵守维护约定。构建前重新检查冻结 SHA
仍存在，缺失则拒绝，不能改用分支当前 tip。恢复步骤同样取各步骤预算与剩余 300s 的较小值。

MVP 一个 build profile 产出一个业务镜像；api/worker 可以复用镜像。多个独立业务镜像的
构建图不在首版。submodule、Git LFS 先拒绝接入，不静默构建不完整源码。
Git archive 会应用 export-ignore/export-subst；项目必须确认这种快照语义，记录产物摘要。
解包不得直接使用不受限 extractall：限制成员数 100000、展开总量 1 GiB、文件 100 MiB；
拒绝绝对路径、..、设备/FIFO、硬链接、符号链接和重复覆盖，创建全新 job 专属目录，
不保留可提权权限。MVP 拒绝含源码符号链接的项目，不假装支持。

固定 BuildKit 模板（独立 rootless buildkitd 已由管理员配置）：

```text
executable: /usr/local/bin/buildctl
argv: ["--addr", "{app.buildkit_socket}", "build",
       "--frontend", "dockerfile.v0",
       "--local", "context={job.source_dir}",
       "--local", "dockerfile={job.dockerfile_dir}",
       "--opt", "filename={app.dockerfile_basename}",
       "--opt", "platform={app.platform}",
       "--output", "type=docker,name={job.unique_image_tag},dest={job.image_archive}"]
```

不允许请求附加 build args、frontend、secret、SSH agent、entitlement；profile 需要的非敏感
build args 由管理员登记。daemon 禁止 security.insecure/network.host entitlement，不启用
no-process-sandbox 捷径。联网构建按管理员的基础镜像/依赖出口策略限制；若目标机器无法
提供合适的 rootless 隔离，安装自检失败，不退回宿主机高权限 `docker build`。
构建目录采用低权限账户，制品经固定交接目录复制到 Runner 控制路径后校验大小/摘要；
确认构建会话和子进程已停止，防止检查后文件仍被修改。导入只接受 Docker image archive。
Compose 最终引用实际不可变 image ID，唯一 tag 只用于导入识别与保留，不信任可漂移标签。

固定测试容器生命周期（下列均 `/usr/bin/docker`，内部变量不可由请求覆盖）：

```text
create ["create","--name","{job.test_container_name}",
        "--label","io.drawbridge.job={job.id}",
        "--network","{app.test_network}","--read-only",
        "--tmpfs","/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop","ALL","--security-opt","no-new-privileges:true",
        "--user","65532:65532","--cpus","1","--memory","512m","--pids-limit","128",
        "--entrypoint","{app.suite_entrypoint}","{app.test_image_id}"]
start  ["start","{job.test_container_id}"]
wait   ["wait","{job.test_container_id}"]
logs   ["logs","--timestamps","{job.test_container_id}"]
stop   ["stop","--time","5","{job.test_container_id}"]
remove ["rm","{job.test_container_id}"]
```

test image 预置本地并冻结 ID，入口为管理员登记的绝对容器路径，无请求 argv。
suite 通过专用测试镜像的固定配置连接 service DNS，不使用宿主机网络；测试网络需管理员
预建并验证可达性/出口限制。CLI wait 返回 0 不代表测试成功：必须解析容器退出码，并核对
容器标签/ID。日志读取与等待并发且受限，超时先 stop，确认已停止后 rm；清理失败进入
NeedsAttention。读取日志不执行镜像中的代码，测试不挂源码或密钥；需要项目代码测试时
另登记隔离镜像，仍不开放 exec。首版 NPU 测试不启用。

## 6. 操作配置格式与示例

schema_version=1。operation 只能选择 executable+argv 或预置 handler，不能同时存在。
配置禁止 YAML 自定义对象、重复 key、未知字段。handler 是代码中固定枚举，不是 Python
模块路径。模板占位符只能访问已声明参数/允许的配置与内部结果字段，禁止表达式求值。
管理员可收紧参数；放宽固定执行能力需要新增操作并审核，不能通过 YAML 加通用解释器。

```yaml
schema_version: 1
operations:
  git_log:
    executable: git # 引用管理员 toolchain，编译后为绝对路径
    argv_prefix: git_safe
    argv: [log, --no-show-signature, "--format=%H%x09%ct%x09%s",
           "--max-count={param.count}", "{job.resolved_sha}", "--"]
    prepare: resolve_allowed_local_ref # 固定预置 prepare，不接受代码
    parameters:
      git_ref:
        type: string
        min_length: 1
        max_length: 200
        pattern: 'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*|refs/tags/[A-Za-z0-9][A-Za-z0-9._/-]*|[0-9a-f]{40}'
        validators: [allowed_app_ref, git_ref_or_commit]
      count: {type: integer, default: 20, minimum: 1, maximum: 100}
    cwd_from: app.repo_path
    execution_profile: source_manage
    public: true
    access: read
    timeout_seconds: 10
    accepted_exit_codes: [0]
    output: {policy: terminate, max_bytes: 65536}
  service_restart:
    handler: service_restart_and_verify
    parameters:
      service:
        type: string
        max_length: 64
        pattern: '[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}'
        validators: [registered_restartable_service]
      reason: {type: string, min_length: 1, max_length: 256}
    execution_profile: runtime_manage
    public: true
    access: runtime_write
    lock: app_environment
    timeout_seconds: 120
```

envelope 的 app/environment/idempotency_key/追踪字段不重复放在 parameters。
示例只展示 schema 用法；其余操作必须按 §§4–5 完整登记，缺失则启动校验失败。

## 7. MCP interface 与任务契约

外部工具沿用技术设计。ops_operation_run 仅接受 public=true；内部操作一律 FORBIDDEN_OPERATION。
部署、测试、重启、回滚都异步返回 job_id；查询返回受限结果，诊断超出 HTTP 等待预算时返回
诊断 job_id，统一由 ops_release_status 查询。ops_test 等便捷工具与通用入口不能各写一套执行逻辑。
workflow 首版仅 deploy_verify；测试和回滚使用内置任务型 handler，不开放动态 workflow 步骤。

统一结果包含 request_id、status、data、error（code/message/retryable/retry_after_seconds），
以及适用的 job_id/release_id/next_cursor/truncated/observed_at。未发生变更的拒绝明确标记。
HTTP/IP/token 拒绝使用 403/401；工具业务错误按 MCP SDK 工具错误机制返回，不模拟 HTTP 状态。

新增明确错误：UNKNOWN_OPERATION、FORBIDDEN_OPERATION、INVALID_PARAMETER、NO_BASELINE、
IDEMPOTENCY_CONFLICT、QUEUE_TIMEOUT、STALE_PLAN、DRIFT_DETECTED、OUTPUT_LIMIT、
DISK_BUDGET_EXCEEDED；其余使用技术设计列出的错误。切生产审查补充错误码：
BUILD_UNSUPPORTED_FRONTEND（Dockerfile `# syntax=` 自定义前端拒绝，不可重试）。
TIMEOUT/VERIFY_FAILED 不表示恢复成功，
必须另返回 recovery.status 和 recovery.release_id。

幂等键全局命名空间，绑定 action/app/environment/规范化参数摘要；追踪字段不进入摘要。
同 plan 只有一个部署 job；去重先于维护、容量、冷却检查，返回已有任务不是新接纳。
plan 默认有效 15 分钟，冻结 SHA、source 摘要、配置摘要、基线、workflow、测试镜像 ID。
配置变更、计划过期或基线变化拒绝执行；客户端失联不取消持久化任务。

## 8. deploy_verify、恢复与运行约定

顺序：preflight → source_snapshot → image_build → image_import/identify → compose_deploy →
health_check → smoke → finalize。总预算 1800s；步骤预算取登记值与剩余总预算的较小值。
恢复另有 300s，不消耗已到期的发布预算。构建前失败无运行时恢复；开始 compose up 前
持久化 runtime_change_started=true，此后即使 CLI 失败/被杀也检查现场并恢复。

健康检查默认：固定 HTTP URL、单次 3s、间隔 2s、90s 内连续 3 次成功，失败重置计数；
状态码 200，可登记固定 JSON 条件，无重定向。Compose --wait 只提供容器运行/健康信号，
不能替代业务门禁。smoke 成功码默认 0。

自动恢复：当前 job 失败但成功恢复时状态 RolledBack，发布仍算失败；恢复生成新 release，
记录恢复原因和目标历史制品。显式回滚创建新 job/release，不修改旧成功 job 的状态。
无基线仅停止该首次发布创建的应用容器；预先存在未登记容器时 preflight 拒绝接管。
RollbackFailed/NeedsAttention 阻止该目标进一步变更，只读查询仍可用；管理员处理现场后
通过本机受控 reconcile 流程重新登记证据，不提供远程“强制忽略”参数。

运行默认值：read=16、running mutations=1、queue=50、每目标 queue=5、queue timeout=600s、
部署冷却=60s。冷却入队和实际派发双检查；排队等待不占运行槽位。
锁顺序固定为 app_environment → repo → build → device；不持 SQLite 事务等待锁。
首版无 NPU 变更任务；只读 NPU 查询不申请设备独占。

维护模式同时约束 Gateway 新准入和 Runner 新派发，不能仅拦住新请求。
Gateway 将维护状态持久化到控制记录；Runner 每次派发读取，记录不可读则停止派发。
进行中的任务完成（包括恢复），排队任务不启动但过期时钟继续。退出维护后重查计划。
切换维护只需重启 Gateway，不重启部署中的 Runner；普通操作配置更新先维护、排空，
再重启双方。Runner 重启先核实遗留进程/构建/容器，不盲目重复副作用。

SQLite 最小表：plans、jobs、steps、releases、artifacts、idempotency_keys、events、
control_state；job 保存 owner/heartbeat、目标、deadline、配置摘要、恢复结果，steps 保存
开始/结束、退出码、termination_reason 和日志引用。UNIQUE(plan_id) 用于部署 job；
UNIQUE(idempotency_key) 保证所有入口一致去重。诊断 job 不计入变更队列容量。
事件只追加，有限历史清理属于人工/受控保留任务，不对抗同权限账户篡改。

## 9. 应用接入与安装自检

管理员必须填写：repo_path、origin、允许 refs、platform、Dockerfile/context、单镜像 profile、
rootless BuildKit socket、Compose 模板/project/services、固定端口/网络/卷、健康 URL、
本地测试镜像 ID/入口、配置文件别名/字段、磁盘预算及可写目录。
Dockerfile/context 必须是快照内无链接相对路径；Compose 和 shell 脚本必须位于业务仓库外。
secret env_file 由管理员维护，工具不直接读取或返回；staging 使用专用低权限凭据。
保留当前、上一成功和回滚引用制品，成功历史默认 5、日志/幂等记录 7 天；清理不删除卷。
磁盘预算至少包含源码、镜像 tar、Engine 镜像与 BuildKit cache，安装时必须显式填写，
preflight 低于保留空间拒绝构建，运行期间监测硬预算；不能只限制日志目录。

NPU 默认 enabled=false，npu_status 返回 UNSUPPORTED。启用前填写厂商、型号、驱动、
固定程序绝对路径、固定只读 argv、所需设备权限、输出解析和超时；不得允许 AI 传入 vendor flags。
例如某型号的管理工具是否提供纯查询模式必须在实际服务器验证，不能把 `npu-smi .*` 放行。

安装自检必须真实验证：Python 3.12+、SQLite WAL 本地盘、账户/目录权限、Git 安全配置、
Git --end-of-options、Compose --wait/--pull never、日志 JSON 解析、固定 Engine socket、
rootless BuildKit 隔离和网络、测试容器清理、Compose 模板权限及镜像 ID 可运行性。
锁定实际通过验收的 Git/Engine/Compose/BuildKit/MCP SDK 版本，不宣称所有旧版本兼容。
Gateway 显式 --no-proxy-headers，CIDR 默认 192.168.0.0/16 可收紧，Host/Origin 必须填写；
HTTP 无加密，可选 token 不防窃听。缺 NPU 工具只禁用该操作，缺隔离构建能力阻止部署功能。

## 10. 实施顺序与完成标准

1. 配置编译器和 ExecutionSpec→ExecutionResult：strict 类型、fullmatch、argv/environment、
   输出预算、进程组收尾；先完成 git_status/git_log/process_list/内置配置诊断。
2. SQLite 任务、幂等、队列、诊断通道、锁、维护和恢复状态；接入 MCP HTTP tools。
3. Git plan/安全快照/rootless 构建/制品导入与识别；不先用危险 fallback 打通演示。
4. Compose 更新、健康/smoke、重启、历史制品回滚、重启后的 reconcile；交付 systemd 与安装检查。

验收必须用一个真实示例应用，完成成功发布、修改后再发布、健康失败恢复、测试失败恢复、
首次部署失败、镜像缺失恢复失败，以及手工漂移检测。另覆盖：

- `;`、换行、`$(...)`、`-c`、URL、revision 表达式、未登记 ref/service/file、额外字段、
  数字字符串/bool、超长输入全部拒绝；合法 agent 分支和 count 边界通过。
- SHA 可达/不可达、fetch/local 来源区分；未提交改动不会进入构建；源码解包不能逃逸。
- 同 plan 多入口/多幂等键只建一个 job；同键不同内容冲突；并发不超容量、冷却不可绕过。
- 维护时已排队任务不派发；只读不中断；旧计划/旧基线不能覆盖新版本。
- 同时大量 stdout/stderr、无效 UTF-8、超时子孙进程、BuildKit/测试容器实际停止和失败清理。
- Gateway/诊断/构建/测试不能接触部署 socket/凭据，业务提交不能替换部署模板/固定脚本。
- 构建摘要截断不终止，硬输出/磁盘预算触发终止；原始日志/密钥不从工具文件读取返回。
- Runner 在 up 前后、验证中、finalize 写库失败时退出：启动后报告真实现场，不重放部署。
- 显式回滚生成新记录；自动恢复不把失败发布标记成功；无基线和恢复失败无虚假承诺。

交付物：Python 包、uv.lock、严格配置 schema/全套示例 YAML、两个 systemd 单元、
低权限 profile/BuildKit 接入说明、初始化与自检命令、示例应用、pytest/Ruff/mypy 验证和运维手册。
本文件是实施规格，不代表上述程序或环境已经实现/验证。

## 11. 命令行为参考

固定 ref 解析使用 Git 的验证模式和选项终止机制，快照采用 archive 而非工作区发布。
参见 [git rev-parse](https://git-scm.com/docs/git-rev-parse)、
[git archive](https://git-scm.com/docs/git-archive)。
Compose 更新与重启分开：restart 不应用配置改动，up 才用于更新。
参见 [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、
[Compose restart](https://docs.docker.com/reference/cli/docker/compose/restart/)、
[Compose logs](https://docs.docker.com/reference/cli/docker/compose/logs/)。
独立构建导出 Docker 镜像归档再导入 Engine，参见
[BuildKit 官方说明](https://github.com/moby/buildkit)及
[rootless 约束](https://github.com/moby/buildkit/blob/master/docs/rootless.md)。
