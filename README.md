# AI 新闻日报

这是一个面向个人 Outlook 账户的私有仓库自动化：采集已配置的 AI 新闻与 GitHub 热门项目，生成中文日报，并可通过 Microsoft Graph 提交邮件。默认不应把它当成“已交付确认”：本地记录的消息 ID **仅表示 Microsoft Graph 已接受/排队**，不能证明收件箱已经送达。

## 上线前准备

1. 创建**私有仓库**，推送此项目。`GITHUB_TOKEN` 由 GitHub Actions 内建提供，无须建立为自定义 Secret。
2. 在 Microsoft Entra 管理中心注册“仅个人 Microsoft 账户”可用的应用，记录应用（客户端）ID，并为委托权限添加 `Mail.Send`。只给个人账户授权，不使用 Outlook 密码。
3. 在本机设置 `MS_CLIENT_ID` 与新生成的 `MS_TOKEN_KEY`，然后执行 `python scripts/setup_outlook.py`。它会走设备代码登录，并只写入 `data/microsoft-token.enc`。只提交这个加密缓存；绝不要提交明文 token、`.env` 或密钥。
4. 在仓库 Secrets 中填入五项：`DEEPSEEK_API_KEY`、`MS_CLIENT_ID`、`MS_TOKEN_KEY`、`OUTLOOK_SENDER`、`MAIL_TO`。对于 `off` 模式的发送，`DEEPSEEK_API_KEY` 不会被使用，但仍建议保留统一的仓库配置。
5. 先运行离线 CI；它只执行 Ruff 和排除 `live` 标记的 pytest，不会采集真实来源、调用模型或发送邮件。

## 首次运行顺序

1. 在 Actions 的 **Run workflow** 中先选择 `mode=off`、`send=false`，或本机执行 `ai-daily preview --ai-mode off`。这只生成 `preview/` 与 Markdown 存档，绝不会发邮件。
2. 审阅 `preview/cost-report.json` 的实际 Token 与 30 天投影；这一步是**首次运行成本审阅**。`full` 有完整 AI 选择/总结，`economy` 保留更多固定候选并减少工作，`off` 不调用模型且成本为零。
3. 在已确认来源、内容和成本后，手动运行一次 `mode=full`、`send=true`。检查 Graph 接受结果、邮箱、`digests/`、`data/` 和加密缓存更新。
4. 同日再次运行将是 already-sent no-op；只在确实需要重发时使用 `force=true`。主任务与 07:22 补偿任务由并发锁串行执行，补偿运行可安全重试未成功的当天任务。
5. 只有第一封真实邮件与成本均审核通过后，才启用两个 Asia/Shanghai 定时任务。

## 来源诊断与恢复

- 使用 `ai-daily validate-sources --minimum-success 80` 检查已启用来源。Actions Summary 提供逐来源的脱敏诊断；低于阈值会失败，但单个来源失败不会阻断一份仍有可用内容的日报。
- 运行 `python scripts/check_secrets.py` 可检查已配置且非空的五项 Secret 是否意外出现在已跟踪文件。输出只包含变量名与路径，不回显值。
- 若 Microsoft 授权失效，在 Microsoft 账户的“应用和服务”中**撤销**该应用同意，然后重新走设备授权。轮换 `MS_TOKEN_KEY` 时，先用新密钥重新生成加密缓存，再同时更新仓库 Secret；旧缓存无法用新密钥解密是预期现象。
- 若发送失败，修复后可运行补偿任务；只有 Graph 接受邮件后，日期标记才会写入。不要手工编辑 sent marker 来掩盖未知的投递状态。

## 日常安全边界

工作流只提交 `data/`、`digests/` 和可能刷新的 `data/microsoft-token.enc`，并在提交前安全 rebase；预览工件用于 Actions Artifact，不应包含秘密。定时运行始终使用 `config/settings.yaml` 的模式并发送；只有手动运行会消费 `mode`、`send` 与 `force` 输入。
