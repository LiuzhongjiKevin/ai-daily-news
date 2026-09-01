# AI 新闻日报

这是一个面向个人 Outlook 账户的私有仓库自动化：采集已配置的 AI 新闻与 GitHub 热门项目，生成中文日报，并可通过 Microsoft Graph 提交邮件。默认不应把它当成“已交付确认”：本地记录的消息 ID **仅表示 Microsoft Graph 已接受/排队**，不能证明收件箱已经送达。

## 上线前准备

1. 创建**私有仓库**并推送此项目。确定唯一默认分支（以下以 `main` 为例），启用分支保护，至少禁止直接绕过审阅修改生产工作流。然后在 **Settings → Actions → General → Workflow permissions** 中把默认权限设为 **Read repository contents and packages**，并关闭 “Allow GitHub Actions to create and approve pull requests”，作为纵深防御。这个仓库设置只是新工作流的默认权限，不是不可突破的权限上限：工作流仍可能显式申请更高权限。所有人工控制都通过只从默认分支读取工作流定义的 `repository_dispatch` 进入，并且每个 job/step 继续显式最小授权；这保护已批准的人工控制路径，但不是对同仓库写入者的通用沙箱。`GITHUB_TOKEN` 由 GitHub Actions 内建提供，无须建立为自定义 Secret。
2. 在 **Settings → Environments** 创建 `ai-daily-production`。在它的 **Deployment branches and tags** 中只允许受保护的默认分支：优先选择 “Selected branches and tags” 并只加入精确的 `main` 分支，不加入 tag 或通配分支；同时启用所需审批/保护规则。生产任务还会校验事件类型、仓库、完整默认分支 ref、branch 类型、大小写和触发 SHA，但 Environment 分支策略仍是必须的第二道边界。
3. 在 Microsoft Entra 管理中心注册“仅个人 Microsoft 账户”可用的应用，记录应用（客户端）ID，并为委托权限添加 `Mail.Send`。在“身份验证”中**允许公共客户端流**，以启用设备代码登录。只给个人账户授权，不使用 Outlook 密码。
4. 在本机安全生成 Fernet 密钥（输出是一行 URL-safe ASCII 文本，复制到 `MS_TOKEN_KEY`，不要粘贴到日志、聊天或仓库）：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。设置 `MS_CLIENT_ID` 与该 `MS_TOKEN_KEY` 后执行 `python scripts/setup_outlook.py`。它会走设备代码登录，并只写入 `data/microsoft-token.enc`。只提交这个加密缓存；绝不要提交明文 token、`.env` 或密钥。
5. 为状态提交创建独立的 `AI_DAILY_STATE_TOKEN`：优先使用短有效期、仅限这个私有仓库的 fine-grained personal access token；Repository permissions 只给 **Contents: Read and write**（隐含的 Metadata: Read 除外），不给 Actions、Workflows、Pull requests、Administration 或其他权限。令牌身份必须能在不绕过审阅规则的前提下向受保护默认分支推送生产状态提交。只在 `ai-daily-production` 的 **Environment secrets** 中保存 `AI_DAILY_STATE_TOKEN`、`DEEPSEEK_API_KEY`、`MS_CLIENT_ID`、`MS_TOKEN_KEY`、`OUTLOOK_SENDER`、`MAIL_TO`；不得建立任何同名 repository-level Actions secret，也不得把状态令牌放入 Variable。Environment 必须按第 2 步只允许受保护默认分支。`DEEPSEEK_API_KEY` 可以留空：`full`/`economy` 会自动降级为确定性中文摘要并保持零付费调用；四项 Microsoft/邮件配置仍是发送的必要条件。
6. 从旧版本迁移时，先停用 `AI_DAILY_SCHEDULE_ENABLED`，从受审阅的本地身份推送本版本并确认仓库已没有 `workflow_dispatch`；再把仓库 Workflow permissions 收紧为只读作为纵深防御，创建 `AI_DAILY_STATE_TOKEN` 和其余 Environment secrets，删除所有同名 repository secrets，最后完成默认分支成本审阅和一次有意的生产投递后才重新启用计划。轮换 `AI_DAILY_STATE_TOKEN` 时同样先停用计划，创建权限相同且短有效期的新令牌、替换 Environment secret、在下一次受控生产写入中确认状态 push 成功，再撤销旧令牌并重新启用计划；不要在日志、Artifact 或 Actions Summary 中粘贴令牌。
7. 先运行离线 CI；它只执行 Ruff 和排除 `live` 标记的 pytest，不会采集真实来源、调用模型或发送邮件。

## 工作流权限与信任边界

### 适用的威胁模型（个人账户/单一受信维护者）

- 本项目按个人账户/单一受信维护者设计：任何 same-repository write access 对内建 `GITHUB_TOKEN` 是受信任的。GitHub 工作流无法在仓库内部阻止已经拥有同仓库写权限的人新增 `push` 工作流并显式申请内建 token scopes，因此绝不要把同一仓库 Write 权限授予不受信任的人。
- 不受信任的贡献必须来自 fork，不能使用同一仓库的功能分支。保持 GitHub Actions 的 fork pull request 设置不向 fork PR 发送写 token 或 Secrets：关闭 **Send write tokens to workflows from pull requests** 和 **Send secrets to workflows from pull requests**；审批策略优先选择 **Require approval for all outside collaborators**，若账户界面不提供该选项，至少选择 **Require approval for first-time contributors**。默认 Workflow permissions 设为 **Read repository contents and packages** 只是纵深防御，不是权限上限。
- 默认分支必须受保护，`ai-daily-production` Environment 必须只允许该默认分支，并把所有生产 Secret 和状态提交凭据仅放在这个 Environment 中。在这一模型和设置下，功能分支/标签目标代码拿不到生产 Secrets 和 `AI_DAILY_STATE_TOKEN`，也不能通过已批准的工作流改变 AI Daily 生产状态。
- `repository_dispatch` 的 `target_ref` 只让目标分支/标签代码进入无 Secret、无持久凭据且强制 `off` 的预览路径，因此对于生产 Secrets 和 AI Daily 状态变更是安全的；但它不是对已经获得同仓库 Write 权限者的仓库级沙箱，此类受信写入者仍能在仓库中添加自己的工作流并申请内建 `GITHUB_TOKEN` scopes。

### 已批准工作流的边界

- 人工入口 **AI Daily Request** 只接受 `repository_dispatch`，因此 GitHub 只从默认分支加载并执行这份工作流。严格解析 payload 后，它可以把 `target_ref` 的分支或 tag 检出到独立 `target/` 目录作为无 Secret、无持久 Git 凭据的预览输入；仅凭选择 `target_ref` 不能替换正在运行的默认分支工作流、取得生产 Secret 或写入 AI Daily 状态。目标代码只执行强制 `off` 的确定性安全预览并上传 `ai-daily-safe-preview` Artifact。
- 独立的 **AI Daily Production** 只从默认分支上的受信工作流代码执行。它由 `workflow_run` 接收请求元数据，但把元数据当作不可信数据：必须来自同仓库、精确默认分支、同一默认分支 SHA、成功的 `repository_dispatch`，并精确匹配预期工作流显示名、`.github/workflows/request.yml` 路径和格式正确的 workflow ID；固定正则只解析 `mode`、`send`、`force`。它不下载或执行请求工作流的 Artifact，也不检出 payload 中的目标 ref。
- 默认分支的 `send=false` 且 `mode=full|economy` 请求进入只读 `cost_preview`：只显式注入 `DEEPSEEK_API_KEY` 和只读内建 `GITHUB_TOKEN`，不注入 Microsoft、状态提交令牌、令牌缓存密钥、发件人或收件人 Secret。所有检出都使用内建只读 token 且设置 `persist-credentials: false`。`AI_DAILY_STATE_TOKEN` 只在四个独立 push step 的进程环境中存在，由辅助脚本通过进程级 `GIT_CONFIG_*` 临时传给单次 `git push`；它不写入 checkout/git 配置，也不传给安装、日报、预览、Artifact 或 Summary。远端 tip 检查同样只在该检查 step 中接收内建只读 token。生产检出固定到已验证 SHA；分支若推进或出现非 fast-forward 就失败并保留保守状态。
- 手动歧义恢复采用同样的 **Resolve AI Daily Delivery Request → Resolve AI Daily Delivery** 两段式边界：前者由默认分支的 `repository_dispatch` 工作流严格验证并记录只读、无 Secret 请求；后者只从默认分支受信代码再次解析允许字段、绑定生产 Environment，并只修改 `data/state.json`。

## 首次运行顺序

先执行 `gh auth status` 并把下列命令中的 `OWNER/REPO`、分支和日期替换为实际值。命令使用类型化 `-F` 传递布尔值/整数，不能把 `false`、`true` 或 `80` 改成带引号的 JSON 字符串。

1. 对功能分支做零成本安全预览（也可把 `refs/heads/feature/example` 换成完整 tag ref）；请求工作流只生成无 Secret 的 `preview/` 与 Markdown 预览，绝不会发邮件。它是零成本的功能检查，**不是**全量模型成本证据：

   ```text
   gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/feature/example' -f 'client_payload[mode]=off' -F 'client_payload[send]=false' -F 'client_payload[force]=false'
   ```

2. 对**默认分支**提交一次 `mode=full`、`send=false` 的成本预览（或在安全本地环境执行 `ai-daily preview --ai-mode full`），再审阅受信 `ai-daily-cost-preview` 中 `cost-report.json` 的实际输入、缓存命中、缓存未命中、输出 Token、当前成本与 30 天投影；这一步才是**首次运行成本审阅**：

   ```text
   gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/main' -f 'client_payload[mode]=full' -F 'client_payload[send]=false' -F 'client_payload[force]=false'
   ```

   `full` 有完整 AI 选择/总结，`economy` 保留规则选定候选并减少工作，`off` 不调用模型且成本为零。若 Environment 中没有 AI key，这次请求会明确显示有效模式为 `off`、零 AI 调用和确定性内容，不能作为付费成本证据。

3. 只有完整模式的无发送预览、内容和成本均审核通过后，才对默认分支提交一次有意发送。受信生产工作流会检查权限后再执行投递：

   ```text
   gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/main' -f 'client_payload[mode]=full' -F 'client_payload[send]=true' -F 'client_payload[force]=false'
   ```

   检查 Graph 接受结果、邮箱、`digests/`、`data/` 和加密缓存更新。在已批准的 `repository_dispatch` 路径中，功能分支和 tag 目标只得到强制 `off` 的安全预览，不接收生产 Secret，也不能改变共享的 sent/intent 状态；这不改变“同仓库写入者本身属于受信主体”的前提。
4. 同日再次运行将是 already-sent no-op；只在已知上一封已完成、且确实需要重发时使用下面的 `force=true` 请求。`force` 不能覆盖投递状态不明的 intent。主任务与 07:22 补偿任务由并发锁串行执行；补偿任务只会自动重试能够证明 Graph 尚未被调用的失败。

   ```text
   gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/main' -f 'client_payload[mode]=full' -F 'client_payload[send]=true' -F 'client_payload[force]=true'
   ```
5. 初次推送时，`AI_DAILY_SCHEDULE_ENABLED` 缺失即视为关闭，`06:47` 和 `07:22` 两个 Asia/Shanghai、08:00 前的定时入口不会发送。完成成本审阅后，在仓库 **Settings → Secrets and variables → Actions → Variables** 新建或编辑变量 `AI_DAILY_SCHEDULE_ENABLED`，值严格设为 `true`，才启用计划。要立即禁用，改为 `false` 或删除该变量；手动 Request 不受此门控影响，但仍受默认分支与 Environment 保护。

## 来源诊断与恢复

- 使用独立的只读 **Validate AI Daily Sources** `repository_dispatch`，或执行 `ai-daily validate-sources --minimum-success 80` 检查已启用来源。工作流请求命令如下；它只从默认分支加载代码、严格接受 `0..100` 的整数阈值、安装生产锁定依赖，且不持有发信或仓库写权限。Actions Summary 提供逐来源的脱敏诊断；低于阈值会失败，但单个来源失败不会阻断一份仍有可用内容的日报。

  ```text
  gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-validate-sources -F 'client_payload[minimum_success]=80'
  ```
- Kimi 至少使用 `www.kimi.com` 官方研究/新闻博客、`platform.kimi.com` API 平台更新和 `MoonshotAI/kimi-code` GitHub Releases；GLM 至少使用 `z.ai` 研究博客、`docs.z.ai` 发布说明和 `zai-org/GLM-5` GitHub Releases。三类通道彼此独立，某个页面结构失效只产生该来源警告。不要凭猜测增加 RSS；新增或更换端点后先运行离线 fixture/schema 测试，再单独执行人工 live 来源验证。
- Page 适配器默认只接收与配置页面完全同源的链接和最终响应；确需跳转到另一个官方 host 时，必须在来源配置中用 `allowed_link_hosts` 精确列出 HTTPS host，路径正则仍同时生效。GitHub 榜单会排除 API 明确标记或描述明确声明的镜像，并把存在完整七日基线且单周增长超过 1,000,000 Star 的数据作为极端异常排除；正常高增长项目仍保留。排除原因只以聚合、脱敏警告展示。
- 运行 `python scripts/check_secrets.py` 可检查已配置且非空的六项 Secret（包括 `AI_DAILY_STATE_TOKEN`）是否意外出现在已跟踪文件。输出只包含变量名与路径，不回显值。
- 若 Microsoft 授权失效，在 Microsoft 账户的“应用和服务”中**撤销**该应用同意，然后重新走设备授权。轮换 `MS_TOKEN_KEY` 时，先停用计划、生成新 Fernet 密钥，运行 `python scripts/setup_outlook.py --replace` 写入新加密缓存，再将新缓存和新 `MS_TOKEN_KEY` Secret 一起更新并手动无发送验证；若不同步，旧缓存无法用新密钥解密是预期现象，恢复方式是用新密钥再次 `--replace` 授权。
- 每次 `run`/`preview` 的 Actions Summary 会显示来源成功率、候选/最终新闻数、GitHub 当前或缓存日期、实际 AI 模式、调用次数、已知 Token、完整或成本下限状态、当前费用、30 天投影和邮件结果；警告会再次脱敏，不包含提示词、响应正文、Token、OAuth 字段或完整邮箱地址。

## 投递状态与歧义恢复

发信采用保守的两阶段状态：

- `reserved`：某个非敏感 attempt ID 已取得当天发信权。Actions 必须先把 `data/state.json` 中的 reservation 提交并推送，随后才允许调用 Graph。
- `ambiguous`：即将调用或已经调用过 Graph，但进程没有完成最终状态提交；传输中断、Graph 202 后磁盘失败、runner 丢失或最终 push 失败都可能属于此类。远端仓库在 runner 崩溃时也可能仍显示 `reserved`，但它同样代表未解决的投递意图。
- `sent`：Graph 已返回 HTTP 202（接受/排队），本地安全指纹写入 `sent_dates`，匹配 intent 在同一次原子状态写入中清除。它仍不代表收件箱最终送达。

任何未解决的 `reserved`/`ambiguous` intent 都会阻止主任务、补偿任务和 `force` 自动再发。这样会优先避免重复邮件；基础设施在关键时刻崩溃后，可能必须人工恢复。不要直接编辑 `state.json`，也不要仅凭失败标志重跑。

先停用计划任务，并确认拥有该 intent 的运行已经进入终态（`success`、`failure` 或 `cancelled`）；运行仍为 queued 或 in_progress 时绝不能执行恢复。然后在 Outlook 中按当天主题检查“已发送邮件”和收件结果。

首选在确认拥有该 intent 的运行已经进入终态后，通过下面二选一的 `repository_dispatch` 请求 **Resolve AI Daily Delivery Request**。运行仍为 queued 或 in_progress 时绝不能执行恢复。只读 Request 成功后，受信 **Resolve AI Daily Delivery** 自动处理。生产恢复与日常发信严格共享 `ai-daily-delivery` 并发组且不取消在先任务，因此恢复状态写入不会与发信任务重叠；它只暂存并非强制推送 `data/state.json`。非默认分支、大小写碰撞分支、同名 tag、不同 SHA 或外部仓库都不能执行生产恢复。

```text
# 已确认没有被接受/发送的当天邮件，允许下一次安全重试
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-resolve -f 'client_payload[target_ref]=refs/heads/main' -f 'client_payload[date]=2026-08-24' -f 'client_payload[resolution]=retry' -f 'client_payload[message_id]=' -F 'client_payload[confirm]=true'

# 已确认邮件存在，记录安全本地指纹并禁止重发
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-resolve -f 'client_payload[target_ref]=refs/heads/main' -f 'client_payload[date]=2026-08-24' -f 'client_payload[resolution]=sent' -f 'client_payload[message_id]=mailbox-confirmed-2026-08-24' -F 'client_payload[confirm]=true'
```

只有在无法使用该工作流时，才在默认分支的本地检出中执行下列一种命令。`--confirm-owner-terminal` 是对上述终态检查的显式确认，不得提前填写或作为常规绕过开关（示例日期请替换）：

```text
# 已确认没有被接受/发送的当天邮件，允许下一次安全重试
ai-daily resolve-delivery retry --date 2026-08-24 --confirm-owner-terminal

# 已确认邮件存在，记录一个不含邮箱或秘密的本地指纹并禁止重发
ai-daily resolve-delivery sent --date 2026-08-24 --message-id mailbox-confirmed-2026-08-24 --confirm-owner-terminal
```

本地 CLI 会获取与发信边界相同的操作锁；若仍有本机发送者持锁，它会安全失败而不会清除 intent。审阅 `data/state.json` 的差异后只提交并推送该文件，再重新启用计划任务。`retry` 只应在人工确认没有已接受邮件后使用；`sent` 的 `--message-id` 只能使用字母、数字、点、下划线、冒号或连字符，不要放邮箱、OAuth 值或其他秘密。已知完成的邮件若需要有意重发，使用手动工作流的 `send=true, force=true`；存在未解决 intent 时必须先按上述流程处理。

## 日常安全边界

受信生产工作流用 step-scoped 内建只读 token 确认远端默认分支仍精确指向获准 SHA，再单独提交并用 step-scoped `AI_DAILY_STATE_TOKEN` 推送 `data/state.json` 的 reservation，之后才运行发信步骤；成功后基于同一份已验证代码和本地 reservation 提交继续提交 `data/`、`digests/` 和可能刷新的 `data/microsoft-token.enc`，中途绝不 pull 或 rebase 到更新代码。所有 push 都是显式默认分支的非 force fast-forward，认证只存在于该 push 子进程的 `GIT_CONFIG_*`；分支推进会让 reservation 前的 push 安全失败，reservation 后的推进或最终 push 失败则保留未解决 intent，等待人工处理。若失败结果是 `not_attempted`，工作流只清除匹配且仍为 `reserved` 的 intent 并持久化；`ambiguous`、`accepted` 或 runner 输出缺失时均保留远端 intent。功能分支 Artifact 只是无 Secret 数据，生产工作流既不下载也不执行它。定时运行始终使用 `config/settings.yaml` 的模式并发送；人工 Request 只能通过默认分支 `repository_dispatch` 提交经过严格解析的 `target_ref`、`mode`、`send` 与 `force` 数据，且非默认 `target_ref` 只能影响安全预览。

## 安装与依赖锁定

本项目的受支持运行方式是**可编辑仓库检出**：在仓库根目录先执行 `python -m pip install --requirement requirements-prod.lock`（开发/CI 使用 `requirements-dev.lock`），再执行 `python -m pip install --no-deps --no-build-isolation -e .`。CLI 需要同级的 `config/` 与 `templates/`，普通 wheel 安装会立即给出可操作的检出提示，而不是在 site-packages 父目录中猜测路径。

更新依赖时，在受控环境中先更新 `pyproject.toml` 的允许范围，重新解析生产及开发完整闭包并将精确版本写入两个 `.lock` 文件，审阅差异后运行离线安装、`python -m pip check`、Ruff 和完整离线测试；不要直接在 Actions 中解析范围依赖。
