# AI 新闻日报（GitHub Actions 版）设计说明

日期：2026-08-24  
状态：已完成讨论，待用户书面审阅  
目标版本：V1

## 1. 项目目标

本项目每天自动采集国内外 AI 行业新闻和 GitHub 全领域热门项目，在北京时间 08:00 前通过 Outlook 个人邮箱发送一封中文日报。

V1 必须做到：

- 运行于 GitHub Actions，不依赖群晖或其他长期在线服务器。
- 新闻既覆盖主要 AI 公司的官方动态，也通过可信媒体发现融资、上市、并购、政策、安全、版权和小团队产品发布等事件。
- 每日最多形成 12 个新闻候选事件，由 AI 决定实际推送数量；没有重大新闻时不强行凑数。
- GitHub 榜单覆盖全领域，而非仅限 AI 项目，按滚动 7 日新增 Star 排出 Top 10。
- 默认使用 DeepSeek，但 AI 接口保持 OpenAI 兼容，可切换模型或完全关闭。
- 首次手动试运行显示实际 Token 用量、估算费用和 30 天费用投影。
- AI、部分新闻源或 GitHub API 异常时，仍尽可能生成并发送降级日报。
- 所有密钥和微软授权材料均不得以明文进入仓库或日志。

## 2. V1 不包含的内容

以下内容明确留给后续版本：

- X/Twitter、微博、知乎、小红书等需要登录、Cookie 或容易触发反爬的平台。
- Web Dashboard、NAS 服务、PostgreSQL、向量数据库或 RAG 问答。
- 微信、Telegram、飞书等其他推送渠道。
- 对全球全部 GitHub 仓库进行绝对完备的 Star 事件统计。
- 自动交易、投资建议或未经核实的传闻推送。

## 3. 总体架构

系统采用“仓库内有状态、外部服务最少”的方案：

```text
GitHub Actions（定时 / 补偿 / 手动）
        |
        +-- 官方博客、RSS、API、GitHub Release、模型平台
        +-- 国内外可信媒体
        +-- GitHub Trending 与 GitHub REST API
        |
        v
规范化 -> 去重 -> 事件聚类 -> 来源核验 -> 规则评分
        |
        +-- 最多 12 个新闻候选
        +-- GitHub 7 日增长 Top 10
        |
        v
DeepSeek（full / economy）或模板（off）
        |
        v
HTML + 纯文本 + Markdown 日报
        |
        v
Microsoft Graph -> Outlook 邮件
        |
        v
快照、状态和 Markdown 日报提交回私有仓库
```

各模块通过结构化数据通信。采集器只负责获取与解析；新闻管线只负责规范化、聚类和评分；AI 层只接收已经裁剪的候选数据；邮件层只负责渲染与发送。任一模块的内部实现可独立替换。

## 4. 新闻覆盖范围

### 4.1 官方和一手来源

首批监控主体覆盖但不限于：

- 国内：Kimi/月之暗面、GLM/智谱、DeepSeek、通义千问、豆包、文心、腾讯混元、MiniMax、阶跃星辰、百川、零一万物、讯飞星火、商汤、ModelScope。
- 国外：OpenAI、Anthropic、Google DeepMind、Google AI、Meta AI、Microsoft AI、xAI、Mistral、Cohere、Perplexity、Amazon、Apple、NVIDIA、AMD、Intel、Hugging Face、Databricks、Runway、Stability AI、ElevenLabs。
- 开发生态：GitHub Blog、GitHub Changelog，以及上述公司的官方 GitHub Organization、Release、Hugging Face 或 ModelScope 组织页。

Kimi 使用 Kimi 官方博客、API 文档及 MoonshotAI 官方 GitHub 等渠道。GLM 使用智谱/Z.ai 官方博客、模型发布记录、官方 GitHub 和模型平台等渠道。

采集优先级为：

1. 官方 RSS/Atom；
2. 官方公开 API、GitHub Release、模型平台接口；
3. 没有订阅接口时使用独立的官网页面适配器。

单个页面适配器失效不得影响其他来源。

### 4.2 可信媒体发现层

国内首批来源：机器之心、量子位、36氪、新智元、IT之家 AI、财联社科技/公司、虎嗅、钛媒体。

国外首批来源：Reuters、TechCrunch AI、The Verge AI、Ars Technica、VentureBeat AI、MIT Technology Review、Hacker News。

媒体层负责发现以下事件：

- 模型发布、产品更新、API 或价格变化；
- 开源项目和重要研究成果；
- 融资、IPO、并购和公司重大变动；
- 芯片、算力与基础设施；
- 监管政策、版权诉讼和安全事件。

来源均由 `config/sources.yaml` 管理。新增常规 RSS、API 或同类页面时只修改配置；只有出现新页面结构时才新增适配器。

### 4.3 可信度标签

同一事件的多篇文章聚合为一个新闻簇，并分为：

- A 级（官方确认）：公司公告、监管文件、官方项目、官方文档或当事方声明。
- B 级（多源确认）：至少两个可信媒体独立报道，且核心事实一致。
- C 级（单源报道）：只有一个可信媒体来源。仅在重要性足够高时推送，并明确标注。

AI 不得提升可信度等级。可信度由可复现的来源规则确定。

## 5. 新闻处理流程

每条原始信息规范化为：

```text
source_id
source_name
source_type
title
published_at
canonical_url
excerpt
language
category
content_fingerprint
```

每日读取上次成功运行后的新内容，并最多回看 36 小时，防止时区差异和单次运行失败造成漏报。

处理顺序：

1. 规范化时间、链接和文本。
2. 使用规范化 URL、标题相似度和内容指纹去重。
3. 将描述同一事件的不同来源聚合成新闻簇。
4. 按最近性、来源可信度、事件类别和影响范围进行规则评分。
5. 选出最多 12 个候选事件。
6. `full` 模式由 AI 决定最终推送数量并生成中文摘要；`economy` 和 `off` 按各自规则处理。

AI 可以从 0 到 12 条中选择。若没有达到阈值的新闻，邮件明确显示“今日无重大 AI 官方动态”，但仍包含 GitHub Top 10。

每条最终新闻包含：

- 发生了什么；
- 为什么值得关注；
- A/B/C 可信度标签；
- 官方来源和/或媒体原文链接；
- 存在分歧时列出不同说法，不强行合并成确定结论。

## 6. GitHub 全领域 Top 10

### 6.1 候选发现

候选池来自：

- GitHub Trending 全语言日榜和周榜，用于发现突然走红的项目；
- GitHub REST Search API，用于补充新建、近期活跃和快速获得关注的项目；
- 已进入候选池的历史项目，持续跟踪至少 7 天。

GitHub Trending 只用于发现候选。最终总 Star、仓库状态和元数据通过 GitHub API 获取；7 日增量由本项目自己的每日快照计算。

### 6.2 快照与排名

每日快照保存：

```text
repository
description
primary_language
stars
forks
updated_at
archived
is_fork
collected_at
```

排名以当前 Star 减去 7 天前快照所得的增量为主排序字段。排名前移前需排除 Fork、归档项目、明显镜像、重复项目和可识别的异常刷榜项目。

前 7 天历史不足时，结合 GitHub 周榜与已积累增量生成“试运行排名”，并在邮件中明确标注；从第 8 天起使用完整滚动 7 日数据。

V1 的榜单是对候选池的高质量近似，不声称覆盖 GitHub 上每一个仓库。候选池通过日榜、周榜、搜索和历史跟踪共同降低漏掉爆发项目的概率。

AI 不参与名次计算，只对确定后的 Top 10 批量生成简短中文说明：项目用途、走红原因和适合关注的人群。

## 7. AI 模式与成本控制

系统支持：

- `full`：AI 处理新闻聚类后的选择和摘要，并解释 GitHub Top 10。
- `economy`：规则完成新闻选择，AI 只摘要最终新闻和 GitHub Top 10。
- `off`：完全不调用 AI，使用原始标题、官方简介和固定模板。

切换模式只修改 `config/settings.yaml`，不改程序。

为控制 Token：

- 不向模型发送完整网页，只发送标题、来源、日期、可信度和裁剪后的核心文本；
- 新闻和 GitHub 项目分别批量处理，避免逐条调用；
- 提示词使用固定前缀，利用 DeepSeek 上下文缓存；
- 限制结构化输出长度；
- AI 输出必须为可校验 JSON，校验失败时有限重试，随后降级。

DeepSeek 返回的实际 `usage` 字段是费用计算依据。每次运行记录输入 Token、缓存命中 Token、缓存未命中 Token、输出 Token、各阶段调用次数、估算费用和 30 天投影。价格放在 `config/pricing.yaml`，以便官方调价后更新，不在代码中写死。

首次通过 `workflow_dispatch` 手动试运行，可选择正常发送或仅生成预览。测试日报和 GitHub Actions Summary 均显示成本报告，并生成不含密钥的 `cost-report.json`。

参考：

- [DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing)
- [DeepSeek Token 用量](https://api-docs.deepseek.com/quick_start/token_usage/)

## 8. 定时、状态和重复发送保护

GitHub Actions 提供三个入口：

- 每天北京时间 06:47 主运行；
- 每天北京时间 07:22 补偿运行；
- 手动运行，用于首次试运行、预览和故障排查。

时区显式设置为 `Asia/Shanghai`。两个定时时间避开整点高峰，并为 08:00 前送达留出缓冲。

每日状态以北京时间日期为键，记录 `generated`、`sent`、邮件标识和关键数据版本。补偿运行发现当天已经成功发送后直接结束。工作流并发组防止主运行、补偿运行和人工运行互相重叠。

只有 Microsoft Graph 返回接受发送后才写入 `sent`。生成失败或发信失败不会错误标记成功。

GitHub 官方说明计划任务可能延迟，在高负载时甚至可能被丢弃；双触发可以提高准时率，但 V1 不承诺绝对 SLA。

参考：[GitHub Actions schedule 事件](https://docs.github.com/actions/using-workflows/events-that-trigger-workflows)

## 9. Outlook 个人账户发信

Outlook.com 要求现代身份验证。V1 使用 Microsoft Graph 委托授权，不使用普通邮箱密码 SMTP。

首次设置流程：

1. 注册支持个人 Microsoft 账户的应用。
2. 只申请委托权限 `Mail.Send` 和获得长期续期凭据所需的 `offline_access`。
3. 用户在本地运行设置脚本，登录个人 Outlook 并同意一次。
4. 脚本生成加密后的 MSAL 令牌缓存。
5. 加密缓存提交到私有仓库，解密密钥存入 GitHub Secret。
6. Actions 静默续期令牌，并在令牌缓存变化时重新加密、提交，支持刷新令牌轮换。

应用不申请读取、删除或管理邮箱的权限。普通 Microsoft 密码永不进入程序、仓库或 GitHub Secrets。

参考：

- [Microsoft Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)
- [代表用户获取访问权限](https://learn.microsoft.com/en-us/graph/auth-v2-user)
- [Outlook.com POP、IMAP 和 SMTP 设置](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com)

## 10. 日报格式

每次生成三种格式：HTML 邮件、纯文本邮件和长期保存的 Markdown。

主题格式：

```text
[AI Daily] 2026-08-24｜7 条 AI 要闻 + GitHub 周榜 Top 10
```

正文顺序：

1. 今日概览和一句话结论；
2. AI 重点新闻；
3. 其他值得关注的动态；
4. GitHub 全领域七日增长 Top 10；
5. 来源覆盖、可信度和降级说明；
6. 本次 AI Token 与费用报告；
7. 采集失败来源或数据更新时间。

HTML 使用内联样式和简单表格，优先保证 Outlook 桌面端和移动端可读；不使用 JavaScript、远程字体或依赖外部 CSS。

## 11. 容错与可观测性

- 网络请求设置明确超时、有限重试和指数退避。
- 单个来源失败只产生警告；其他来源继续运行。
- 全部新闻源失败时发送只含 GitHub 榜单和故障说明的降级邮件。
- DeepSeek 超时、响应无效或余额不足时自动进入 `off` 模式。
- GitHub API 失败时使用最近一次成功快照，并在邮件中显示数据时间。
- Outlook 发信失败时不写入成功状态，由补偿运行重试。
- 两次发信均失败时工作流保持失败状态，通过 GitHub 自身的 Actions 失败通知提醒用户。
- GitHub Actions Summary 展示来源成功率、候选数、最终新闻数、GitHub 快照状态、AI 模式、Token 与费用、邮件结果。
- 调试日志和报告作为短期 Artifact 保存，不提交原始网页全文或敏感信息。

## 12. 安全设计

GitHub Secrets：

- `DEEPSEEK_API_KEY`
- `MS_CLIENT_ID`
- `MS_TOKEN_KEY`
- `OUTLOOK_SENDER`
- `MAIL_TO`

仓库必须设为私有。工作流权限遵循最小权限原则，只允许读取内容并为状态、快照、日报和加密令牌缓存提交更新。日志输出对密钥、令牌和邮箱地址进行遮蔽。

依赖版本锁定，第三方 GitHub Action 固定到可信提交 SHA；能用 Python 标准库或项目代码完成的功能不引入不必要的外部 Action。

## 13. 项目结构

```text
.github/workflows/
  daily.yml
config/
  sources.yaml
  settings.yaml
  pricing.yaml
src/
  collectors/
  pipeline/
  ai/
  github_trending/
  digest/
  mail/
  state/
templates/
  daily.html
  daily.txt
data/
  github/
  state.json
  microsoft-token.enc
digests/
scripts/
  setup_outlook.py
  preview.py
tests/
  fixtures/
```

GitHub 快照保留最近 35 天；Markdown 日报长期保留；Actions Artifact 只用于短期日志、预览和诊断。

## 14. 测试策略

自动化测试覆盖：

- RSS、Atom、官方网页和媒体页面解析；
- URL 规范化、中英文标题去重、内容指纹和事件聚类；
- A/B/C 可信度规则；
- 12 条候选上限和无重大新闻场景；
- GitHub 七日 Star 增量、过滤和 Top 10 排名；
- 首周试运行排名；
- `full`、`economy`、`off` 三种模式；
- AI 超时、无效 JSON、余额不足和降级；
- Token 与费用计算；
- HTML、纯文本和 Markdown 渲染；
- 微软令牌缓存加解密与更新；
- 重复发送保护和补偿运行。

解析测试使用仓库中的固定样本，不要求测试时目标网站在线。真实联网、DeepSeek 和 Microsoft Graph 使用手动烟雾测试，避免在每次提交时产生费用或发送邮件。

## 15. V1 验收标准

1. 可以从 GitHub Actions 手动运行并向指定 Outlook 收件地址发送测试日报。
2. HTML 和纯文本邮件在 Outlook 桌面端与移动端可读。
3. 新闻覆盖已列出的国内外官方主体与可信媒体，显示可信度和原文链接。
4. 规则层最多输出 12 个候选，`full` 模式由 AI 决定实际推送数量。
5. GitHub 榜单覆盖全领域；首周明确标注试运行，第 8 天形成完整七日排名。
6. 测试日报和 Actions Summary 显示实际 Token 与估算费用。
7. 修改一个配置项即可切换 `full`、`economy` 或 `off`。
8. AI 不可用时仍可发送模板日报。
9. 单个来源失败不会中断整份日报。
10. 当天主运行和补偿运行不会产生重复邮件。
11. 仓库和日志中不存在明文 API Key、微软令牌或 Outlook 密码。
12. 自动化测试全部通过，手动联网烟雾测试记录成功。

## 16. 首次上线顺序

1. 创建私有 GitHub 仓库并配置 Secrets。
2. 完成 Outlook 一次性授权和加密令牌缓存初始化。
3. 使用固定样本运行全部离线测试。
4. 通过 `workflow_dispatch` 执行“只生成、不发信”预览。
5. 手动执行一次真实采集、AI 分析和 Outlook 发信。
6. 查看 Token、费用、来源成功率和邮件效果。
7. 根据费用选择保留 `full`、切换 `economy` 或关闭 AI。
8. 启用 06:47 主运行和 07:22 补偿运行。
9. 第 8 天检查完整滚动七日 GitHub 排名。

本设计完成后，下一阶段只编写实施计划；在实施计划获得确认前不开始程序开发。
