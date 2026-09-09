# 国际形势与财经日报

本分支每天收集国际形势、宏观经济与财经新闻，通过 QQ 邮箱发送。最多 12 条，两个栏目各最多 6 条；英文来源保留原文标题和短摘要。当前不调用 AI，不提供投资建议。

这是独立的 `world-finance-daily` 分支，不要合并整个分支到 `master`。AI 日报继续在主分支运行。主分支只需保留专用启动文件 `.github/workflows/world-finance.yml`。

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

安装并登录 GitHub CLI 后，将 OWNER/REPO 换成自己的仓库：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=world-finance-preview
```

在 Actions → World Finance Daily 中查看结果，下载 `world-finance-preview` 附件，打开 `digest.html`。此步骤不需要邮箱凭据，不发送邮件。

报告记录各来源的状态与最近 24 小时条目数。0 条可能只是该机构今天没有更新，并不一定是故障。至少两个独立发布机构有合格内容才算通过内容检查。

## 3. 手动发送

配置完成并确认名单后，执行：

```text
gh api --method POST repos/OWNER/REPO/dispatches -f event_type=world-finance-send
```

任务先做预览验证，再使用同一代码版本重新采集并发送。每个北京时间日期最多一次正式发送；同日再次触发不会重复发送。服务器接受邮件不等于邮箱一定收件，请检查垃圾邮件和实际收信。

如果任务发送结果不确定，会保留阻塞记录以避免重复发送。不要删除记录或盲目重试，应先查看任务日志，确定是否被服务器接受。

## 4. 开启每日发送

在 **Settings → Secrets and variables → Actions → Variables** 新建仓库变量：

- 名称：`WORLD_FINANCE_SCHEDULE_ENABLED`
- 值：`true`

未设置或设置为 `false` 时不自动发送。计划北京时间 06:37 首次运行，07:17 补位；当天已经成功发送则跳过。GitHub 可能排队延迟，无法保证 08:00 前送达。

代码推送只触发预览，不触发邮件。新日报不会读取旧日报的邮箱配置、发送状态，也不会收集 GitHub 热门项目。

## 来源与限制

当前使用 BBC、Guardian 的国际/财经频道，以及联合国、美联储、欧洲央行 RSS。部分报道有地区偏向，不等于全球全量覆盖。基于来源、主题和时效性规则筛选、去重；不保证覆盖所有重要事件，也不将排序分数当作可信度。

更多中文来源、自动翻译、更完整的事件聚类和额外地区覆盖可在后续验证后加入。X 来源未接入。

[设计与验收标准](docs/world-finance-design.md)
