# 赛博日报 · 国际形势与财经

这是赛博日报的财经版本，每天收集国际形势、宏观经济与财经新闻，通过 QQ 邮箱发送。最多 12 条，两个栏目各最多 6 条；默认使用原文标题和短摘要，可选接入受月预算保护的阿里云通用中文翻译。当前不调用大模型，不提供投资建议。项目另有 [AI 日报版本](../../tree/master#readme)，关注 AI 新闻与全领域 GitHub 热门项目；两份日报可以同时使用，分别配置收件人。

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

此入口只有手动触发，不会定时执行。每次确认都会发送一封带“调试发送”标题的真实财经日报，不检查当天是否已发送，不读取或修改正式发送状态，不需要状态令牌。使用 `world-finance-production` 的邮箱配置，收件人名单与财经日报一致；此入口没有持久翻译预算，因此只发送原文。

每次运行（包括 Re-run jobs）都可能重复发邮件并消耗翻译额度。发送异常不会自动重试；请先检查邮箱和日志再决定是否重跑。运行页的 `world-finance-debug` 附件保留本次邮件正文和诊断信息。正式日报仍每天最多发送一次。

在 **Settings → Secrets and variables → Actions → Variables** 新建仓库变量：

- 名称：`WORLD_FINANCE_SCHEDULE_ENABLED`
- 值：`true`

未设置或设置为 `false` 时不自动发送。计划北京时间 06:37 首次运行，07:17 补位；当天已经成功发送则跳过。GitHub 可能排队延迟，无法保证 08:00 前送达。

代码推送只触发预览，不触发邮件。新日报不会读取旧日报的邮箱配置、发送状态，也不会收集 GitHub 热门项目。

## 来源与限制

### 可选：自动翻译为中文

财经正式投递改用阿里云 TranslateGeneral（2018-10-12，通用版 en→zh），默认关闭。只翻译标题和 220 字截断摘要；数字校验失败、接口失败、预算未知或耗尽时保留原文，不回退腾讯或其他收费供应商。普通预览和独立调试发送不调用翻译。

在 `world-finance-production` 的 **Environment secrets** 安全保存 `ALIBABA_CLOUD_ACCESS_KEY_ID` / `ALIBABA_CLOUD_ACCESS_KEY_SECRET`，不要写入仓库或聊天。只有密钥不会开启翻译；还需要核实免费余量、人工初始化月账本、保护分支并配置可信锚点，最后明确开启变量。详见 [阿里翻译运行说明](docs/world-alibaba-translation.md)。

程序月上限 300000 字符、单运行最多 12000 字符/40 请求/60 秒。按保守 UTF-16 单元计数，包括空格标点；完整清单额度与邮件预留一起持久化并确认普通 Git push 成功后才调用。失败、超时和取消不退额度，重跑不能复用旧预留，跨月暂停。原文标题和链接保留。

该上限不能约束同主账号其他程序、密钥泄露或计费变化，不保证绝对零扣费。免费额度由主账号与 RAM 共享，超额自动后付费；额度显示可能延迟。旧腾讯 Secrets 本轮不删除：AI 日报仍使用腾讯。财经调试入口为避免绕过月预算，改为原文发送。

邮件采用逐条新闻卡片，原标题、来源、时间和摘要分别显示；纯文本及 Markdown 版本用分隔线区分新闻。此排版同时适用于正式日报和调试发送。

当前使用 BBC、Guardian 的国际/财经频道，以及联合国、美联储、欧洲央行 RSS。部分报道有地区偏向，不等于全球全量覆盖。基于来源、主题和时效性规则筛选、去重；不保证覆盖所有重要事件，也不将排序分数当作可信度。

更多中文来源、更完整的事件聚类和额外地区覆盖可在后续验证后加入。X 来源未接入。

[设计与验收标准](docs/world-finance-design.md)

## 许可证与免责声明

本项目自行编写的源代码及配套文档采用 [MIT License](LICENSE) 开源。使用、复制、修改或分发时，请保留原版权声明和许可证文本。

MIT 许可不代表项目作者拥有或重新授权日报中引用的新闻标题、摘要、图片、商标、网站数据或其他第三方内容。相关内容仍归各自权利人所有，并受其来源网站、数据服务及 API 的条款约束。部署者和使用者应自行确认其采集、翻译、存储与发送方式符合适用规则。

本项目按“现状”提供，不对可用性、准确性、完整性或特定用途作保证。由来源变化、翻译偏差、邮件投递、API 费用或使用本项目造成的风险由使用者自行判断。本日报仅供信息参考，不构成投资、法律或其他专业建议。
