# 赛博日报 · 国际形势与财经

这是赛博日报的财经版本，每天收集国际形势、宏观经济与财经新闻，通过 QQ 邮箱发送。最多 12 条，两个栏目各最多 6 条；默认使用原文标题和短摘要，可选接入腾讯云 TMT 中文翻译。当前不调用大模型，不提供投资建议。项目另有 [AI 日报版本](../../tree/master#readme)，关注 AI 新闻与全领域 GitHub 热门项目；两份日报可以同时使用，分别配置收件人。

这是独立的 `world-finance-daily` 分支，不要合并整个分支到 `master`。AI 日报继续在主分支运行，财经日报拥有独立的收件人名单和发送记录。日常使用 **World Finance Daily**，同日重复测试使用 **World Finance Debug Send**；这两个工作流均从主分支启动，再运行财经分支的代码。

## 1. 准备独立配置

在同一个仓库打开 **Settings → Environments → New environment**，创建：

`world-finance-production`

在 Deployment branches and tags 中只允许 **master**（工作流从 master 启动，再检出本分支）。不要改旧的 `ai-daily-production` 环境。

在新环境的 **Environment secrets** 中填写四项：

| 名称 | 内容 |
| --- | --- |
| `SMTP_USERNAME` | QQ 发件邮箱，可以与旧日报相同 |
| `SMTP_PASSWORD` | QQ SMTP 授权码，不是登录密码 |
| `MAIL_TO` | 新日报的完整收件人名单，多个邮箱用英文逗号分隔 |
| `WORLD_FINANCE_STATE_TOKEN` | 仅对本仓库有 Contents 读写权限的 GitHub 令牌 |

QQ 授权码参考 [QQ 官方 SMTP 教程](https://help.mail.qq.com/detail/0/1087)。

收件人示例：`reader1@example.com,reader2@outlook.com`。不自动抄送发件人；如果希望发件人也收到，请明确加入名单。收件人能在邮件 To 栏看到彼此的邮箱地址。

### 获取状态令牌

打开 [创建细粒度令牌](https://github.com/settings/personal-access-tokens/new)，选择自己的账户作为 Resource owner，在 Only select repositories 中仅选择本仓库，将 Contents 设为 Read and write，其他权限保持默认。选择合适的到期时间，创建后保存到上述新环境。

该令牌用于保存发送记录，与 AI 无关。令牌权限是仓库级，不是分支级；程序固定只向 `world-finance-daily` 推送，仍应限制谁能修改代码。不要将令牌写入代码或聊天。

## 2. 先验证预览

打开 **Actions → World Finance Daily → Run workflow**，Branch 选择 **master**，不要勾选发送，点击绿色 **Run workflow** 按钮。

这里选择 master 是为了从受信任的入口启动，实际采集和发送使用的仍是独立的 `world-finance-daily` 分支。

也可以安装并登录 GitHub CLI 后，将 OWNER/REPO 换成自己的仓库：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=world-finance-preview
```

在 Actions → World Finance Daily 中查看结果，下载 `world-finance-preview` 附件，打开 `digest.html`。此步骤不需要邮箱凭据，不发送邮件。

报告记录各来源的状态与最近 24 小时条目数。0 条可能只是该机构今天没有更新，并不一定是故障。至少两个独立发布机构有合格内容才算通过内容检查。

## 3. 手动发送

配置完成并确认名单后，打开 **Actions → World Finance Daily → Run workflow**，Branch 选择 **master**，勾选 **发送正式邮件**，再点击绿色 **Run workflow** 按钮。

也可以使用命令行：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=world-finance-send
```

任务先做预览验证，再使用同一代码版本重新采集并发送。每个北京时间日期最多一次正式发送；同日再次触发不会重复发送。服务器接受邮件不等于邮箱一定收件，请检查垃圾邮件和实际收信。

如果任务发送结果不确定，会保留阻塞记录以避免重复发送。不要删除记录或盲目重试，应先查看任务日志，确定是否被服务器接受。

## 4. 开启每日发送

### 需要同日重复测试？使用独立调试入口

打开 **Actions → World Finance Debug Send → Run workflow**，Branch 选择 **master**，勾选“确认发送一封调试日报”，点击绿色 **Run workflow**。

此入口只有手动触发，不会定时执行。每次确认都会发送一封带“调试发送”标题的真实财经日报，不检查当天是否已发送，不读取或修改正式发送状态，不需要状态令牌。使用 `world-finance-production` 的邮箱和可选 TMT 翻译配置，收件人名单与财经日报一致。

每次运行（包括 Re-run jobs）都可能重复发邮件并消耗翻译额度。发送异常不会自动重试；请先检查邮箱和日志再决定是否重跑。运行页的 `world-finance-debug` 附件保留本次邮件正文和诊断信息。正式日报仍每天最多发送一次。

在 **Settings → Secrets and variables → Actions → Variables** 新建仓库变量：

- 名称：`WORLD_FINANCE_SCHEDULE_ENABLED`
- 值：`true`

未设置或设置为 `false` 时不自动发送。计划北京时间 06:37 首次运行，07:17 补位；当天已经成功发送则跳过。GitHub 可能排队延迟，无法保证 08:00 前送达。

代码推送只触发预览，不触发邮件。新日报不会读取旧日报的邮箱配置、发送状态，也不会收集 GitHub 热门项目。

## 来源与限制

### 可选：自动翻译为中文

在 **Settings → Environments → world-finance-production → Environment secrets** 添加：

| 名称 | 内容 |
| --- | --- |
| `TENCENTCLOUD_SECRET_ID` | 腾讯云 API 密钥的 SecretId |
| `TENCENTCLOUD_SECRET_KEY` | 同一对密钥的 SecretKey |

建议使用只授权 TMT 文本翻译的子账号密钥，不要写入代码或发到聊天。两项齐全，下一次定时或手动正式发送就会尝试把入选英文标题和短摘要翻译为中文，默认广州地域、项目 0。原文标题和链接保留。缺少任一项、接口报错或超时，则保留原文继续发送；删除任一项即可停用。普通预览及其附件保持原文，不消耗翻译额度。

中文或中英混排、链接、超过 1000 字符的单段保持原文。每次最多提交 12000 字符、40 次请求，达到约一分钟处理预算后不再发起请求；同次运行复用重复文本，第一次接口故障后剩余内容用原文。当天已发送时不会再消耗翻译额度。日志仅显示调用数、提交字符数和回退状态，不代表实际账单。

**不保证免费：请在腾讯云机器翻译控制台 → 系统设置，确认后付费关闭。** 免费资源包耗尽或到期、服务拒绝调用时，日报将以原文发送，不需要为保持发信而充值。代码不会购买或续费资源包。字符限制不是账号总费用上限，其他应用、多次运行也会消耗额度；以 [腾讯云计费说明](https://cloud.tencent.cn/document/product/551/35017) 及账户账单为准。

机器翻译可能误译；数字校验不能保证币种、涨跌方向或政策措辞准确，请以原文为准。AI 日报需在 `ai-daily-production` 环境单独配置同名两项，彼此独立。

数字保护支持常见等价写法，例如 November 与 11月、$5,000 与 5000美元、$6bn 与 60亿美元；不支持或不一致的表达仍保留原文，不保证所有译文都通过。日志 `TMT decisions` 中，`numeric_mismatch` 表示数字或单位不一致，`provider_unchanged` 表示接口原样返回，`provider_error` 表示接口调用或响应错误，`circuit_open` 表示发生错误后停止后续调用，`character_limit` / `request_limit` / `time_limit` 表示达到本次预算。只记录原因计数，不记录密钥和新闻正文。

邮件采用逐条新闻卡片，原标题、来源、时间和摘要分别显示；纯文本及 Markdown 版本用分隔线区分新闻。此排版同时适用于正式日报和调试发送。

当前使用 BBC、Guardian 的国际/财经频道，以及联合国、美联储、欧洲央行 RSS。部分报道有地区偏向，不等于全球全量覆盖。基于来源、主题和时效性规则筛选、去重；不保证覆盖所有重要事件，也不将排序分数当作可信度。

更多中文来源、更完整的事件聚类和额外地区覆盖可在后续验证后加入。X 来源未接入。

[设计与验收标准](docs/world-finance-design.md)
