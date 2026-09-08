# AI 新闻日报

用 GitHub Actions 自动收集 AI 新闻和全领域 GitHub 热门项目，通过 QQ 邮箱发送日报。每份邮件同时发到 QQ 发件邮箱和指定的另一收件邮箱。当前不调用 AI，无模型费用；部分来源仍在完善。

## 部署前准备

准备一个 GitHub 账户、一个可用的 QQ 邮箱，以及另一个收件邮箱（例如 Outlook）。下面所有仓库设置都在**你自己的仓库**中操作。

## 第一步：复制项目并启用 Actions

1. 点击本项目右上角 **Fork**，复制到自己的 GitHub 账户。
2. 打开复制后的仓库，进入 **Actions**，按页面提示启用工作流。
3. 在 **Settings → General → Default branch** 查看默认分支名称，记下是 `master`、`main` 还是其他名称，后面会用到。
4. 进入 **Settings → Actions → General → Workflow permissions**，选择 **Read repository contents and packages** 并保存。正式发送时的记录写入由下一步创建的专用令牌负责。

仓库可以公开，但密码、授权码和令牌只填写在 Secrets 中，不要提交进代码。不要向不受信任的人开放仓库写入权限。

## 第二步：获取 QQ 邮箱授权码

按 [QQ 邮箱官方 SMTP 开启教程](https://help.mail.qq.com/detail/0/1087) 开启服务并获取授权码。

这是发信程序使用的授权码，**不是 QQ 登录密码**。收件邮箱不需要提供密码，也不需要登录 Azure。

## 第三步：获取 GitHub 令牌

这个令牌用于保存发送记录，避免每天重复发送，**与 AI 无关**。

1. 打开 [GitHub 创建细粒度令牌页面](https://github.com/settings/personal-access-tokens/new)。
2. **Token name** 填写 `ai-daily-state`。
3. **Expiration** 设置到期时间，例如 90 天；到期前需要更新令牌。
4. **Resource owner** 选择自己的 GitHub 账户。
5. **Repository access** 选择 **Only select repositories**，只勾选刚复制的日报仓库。
6. 在 **Repository permissions** 中，将 **Contents** 设置为 **Read and write**。其他权限保持默认；自动附带的 Metadata 读取权限正常保留。
7. 点击 **Generate token**，复制生成的令牌。

令牌只会显示一次，下一步直接保存到 GitHub。不要发到聊天、截图或公开文件中。

## 第四步：保存四项配置

1. 打开仓库 **Settings → Environments → New environment**。
2. 环境名称填写 `ai-daily-production`。
3. 在 **Deployment branches and tags** 中选择 **Selected branches and tags**，只添加第一步记下的默认分支，类型选择分支（Branch）。
4. 在该环境的 **Environment secrets → Add environment secret** 中，逐个添加：

| Name（照抄） | Value（填写自己的信息） |
| --- | --- |
| `SMTP_USERNAME` | 完整 QQ 邮箱地址 |
| `SMTP_PASSWORD` | 第二步获取的 QQ 邮箱授权码 |
| `MAIL_TO` | 另一个收件邮箱地址，例如 Outlook |
| `AI_DAILY_STATE_TOKEN` | 第三步生成的 GitHub 令牌 |

这些配置应放在上述 **Environment secrets**，不是 Variables，也不是普通仓库级 Secrets。QQ 发件邮箱会自动收到一份，相同地址不会重复发送。

不需要填写微软客户端 ID、微软加密密钥或 AI 密钥。

## 第五步：测试邮箱并预览日报

目前人工操作使用 GitHub CLI，尚未提供网页上的“一键发送”按钮。

安装 [GitHub CLI](https://cli.github.com/)，在终端执行并按提示登录：

```text
gh auth login
```

下列命令中的 `OWNER/REPO` 换成自己的仓库，例如 `your-name/ai-daily-news`。命令中的 `master` 也要换成第一步确认的默认分支名。

先发一封简单测试邮件：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-test-mail
```

在 **Actions → Test QQ Email** 查看进度，再检查两个邮箱及垃圾邮件文件夹。此测试只验证邮箱，不验证自动发送所需的记录写入权限。

接着生成真实日报预览，不发送邮件：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/master' -f 'client_payload[mode]=off' -F 'client_payload[send]=false' -F 'client_payload[force]=false'
```

等待 **AI Daily Request** 完成，在对应运行页面的 **Artifacts** 中下载 `ai-daily-safe-preview`，检查日报内容。采集可能需要几分钟。

## 第六步：验证正式发送

预览无误后，执行：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=ai-daily-request -f 'client_payload[target_ref]=refs/heads/master' -f 'client_payload[mode]=off' -F 'client_payload[send]=true' -F 'client_payload[force]=false'
```

这会先运行 **AI Daily Request**，再进入 **AI Daily Production**。请确认后者的 `daily` 任务实际运行成功，而不是被跳过，并检查两个邮箱收到日报。

这一步同时验证新闻采集、发送记录保存和邮件投递。若仓库要求所有更改必须走拉取请求，直接保存发送记录可能被分支规则阻止；请按 [维护说明](docs/maintenance.md) 核对规则，不要盲目扩大令牌权限。

同日正式发送成功后，再次运行通常会跳过。失败时先检查日志和收件箱，不要连续重发；“运行成功”也不等于邮件一定进入收件箱。

## 第七步：开启每天自动发送

1. 打开仓库 **Settings → Secrets and variables → Actions → Variables**。
2. 点击 **New repository variable**。
3. **Name** 填写 `AI_DAILY_SCHEDULE_ENABLED`。
4. **Value** 填写小写的 `true`，保存。

默认未配置此变量时不会自动发送。开启后，计划每天北京时间 **06:47** 开始，**07:22** 进行补偿检查。目标是早上 8 点前完成，但 GitHub 排队和来源响应可能导致延迟。

开启开关不会立即补发当天日报。需要立即发送时使用第六步的命令。

## 日常维护

- **暂停自动发送**：将 `AI_DAILY_SCHEDULE_ENABLED` 改为 `false`。
- **更换收件邮箱**：更新 `MAIL_TO`，QQ 发件邮箱仍会收到一份。
- **QQ 授权码失效**：重新获取并更新 `SMTP_PASSWORD`。
- **GitHub 令牌到期**：按第三步生成新令牌，更新 `AI_DAILY_STATE_TOKEN`。
- **没有收到日报**：检查开关、四项配置，以及 **AI Daily Production** 中 `daily` 任务是否运行；再查看垃圾邮件。
- **新闻少于 12 条**：按实际可用数量发送，不凑数。来源错误已从邮件屏蔽，详细信息保留在运行记录和 Markdown 存档中。

当前 AI 仍关闭，填写状态令牌不会开启 AI 或产生模型费用。GitHub Actions 的使用额度以自己的账户为准。

## 仍待解决的内容

以下按 2026-09-07 的验证情况记录，避免把“没有抓到”误认为“当天没有新闻”。

| 待解决项 | 当前情况与后续方向 |
| --- | --- |
| GLM 旧博客 | 原地址返回 404，需寻找替代入口；独立的 GLM 更新日志已经修复。 |
| Qwen 新发布渠道 | 旧博客可以读取，但内容较旧，需验证新的官方发布渠道。 |
| Google DeepMind | 新闻列表只显示月份，需从文章页取得准确日期，不能直接当作每日新闻。 |
| Google AI、Runway、文心、Intel | 原地址发生跳转，需核对新入口及其新闻内容，不能只替换网址。 |
| 豆包、混元、阶跃、百川、讯飞、魔搭 | 当前产品入口未直接提供可用的带日期新闻列表，需要寻找官方新闻或更新日志。 |
| Meta AI、Cohere、Apple 机器学习 | 页面可访问，但尚未完成可靠的标题、日期、链接提取。 |
| 零一万物、Microsoft、AMD | 本次连接失败或此前访问受限，需复核可用入口和运行环境。 |
| xAI、Perplexity、VentureBeat | 访问被拒绝或限流，尚未找到可靠的获取方式。 |
| 路透及国内媒体发现通道 | 路透、机器之心、量子位、36氪、新智元、IT之家、财联社、虎嗅、钛媒体共用的 GDELT 接口频繁限流或返回异常内容，需要改善通道或寻找替代来源。 |
| 来源覆盖情况 | 单个来源的技术报错已从邮件屏蔽，仍保留在运行记录和 Markdown 存档中；来源覆盖仍需继续完善。 |
| 定时发送与运行稳定性 | 部署者需按下文配置令牌；正式定时流程和 GitHub Actions 环境中的持续抓取效果仍需验证。 |
| AI 摘要、翻译与筛选 | 暂未启用，后续再评估效果和费用。 |

以上问题不会阻止已成功采集的内容生成邮件，但会影响覆盖面。邮件不再显示单个来源的报错，不代表所有来源都已恢复。

详细排错和历史 Outlook 配置见 [维护说明](docs/maintenance.md)。
