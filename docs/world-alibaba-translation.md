# 财经日报阿里翻译：预算与上线步骤

本地实现日期：2026-11-17。默认关闭。本地测试不等于生产验证。AI 日报不在迁移范围；财经正式投递不回退腾讯，财经 debug/preview 保持原文。

## 安全边界

固定 TranslateGeneral 2018-10-12、FormatType=text、Scene=general、en→zh 和国内 endpoint https://mt.cn-hangzhou.aliyuncs.com/。使用官方 RPC v2 HMAC-SHA1 签名和已锁定 httpx==0.28.1，不新增依赖。无自动重试/重定向，业务 Code 必须为整数 200，译文结构及数字必须通过校验。HTTP debug 开启时拒绝调用。

清单仅标题与现有 220 字摘要，去重后最多 40 条/12000 UTF-16 单元，包含标点空格、非 BMP 计两个单元；每次 60 秒。实际调用上海月份计费，月底最后 65 秒不调用，跨月许可作废。字段保持原文不等于费用退款。

StateStore 显式保存 translation_budget；最多 300000/月。reserved 必须等于 baseline 加全部预留。账本只含月份、限额、预留量、reservation ID、run ID/attempt、清单 hash 和时间，无原文/凭据。邮件先去重，整批预算与邮件 reservation 同一状态文件提交，普通非强制 push 成功且远端 tip 可确认才允许请求。冲突、失败、结果不明不调用、不 rebase 后重用。预留永久保留，失败/超时/取消/崩溃均不退款；同 run ID 的新 attempt 仍不能再消费。

人工审查后的初始化提交是可信锚点。必须有完整历史、最新 remote tip；历史账本不能删除旧月份、降低预留或改变 baseline/limit/旧 reservation。旧 checkout、revert、浅历史、merge、损坏或缺账本均关闭翻译。还实时核验分支禁止强推/删除且 enforce_admins=true；保护无法读取即关闭。Git 子进程移除阿里/腾讯密钥及其他业务凭据，原始 token 不进子进程；仅远程操作使用临时 Git auth header。日志不输出响应/签名/文本/密钥。

拥有全部管理权的人仍可移除保护、改代码或改锚点。仓库内部无法抵御管理员移除所有保障；主账号其他程序、泄露密钥和计费变化也不受本程序控制，不保证绝对零扣费。

## 上线顺序

1. 用户在阿里主账号核实国内通用版**本月剩余免费字符数**、已发生/正在发生的用量以及所有 RAM/程序的共享用量，计入约一小时显示延迟。不知道剩余额度时不得按 0 已用量初始化。
2. RAM 只授予 alimt:TranslateGeneral 的必要权限，用户直接把两个密钥存到 world-finance-production Environment secrets，不经聊天。2026-11-17 已只读核实两个名字存在，没有读取值；同名 Secret 也在 ai-daily-production，本变更不使用后者。
3. 财经分支禁止强推/删除并 enforce_admins=true。状态 token 需 contents:write，以及读取 branch protection 所需权限（fine-grained token Administration:read）；不能把 GitHub 权限给翻译 RAM。先检查现有规则，确保普通状态 push 仍可成功。当前远端实际返回 Branch not protected，因此尚不满足门槛。
4. 审查并部署内容分支 world-finance-daily 与 master 上两份财经 launcher 的对应改动，保留 preview 精确 SHA、send 专用 environment。候选 workflow 的 send checkout 已设 fetch-depth=0，preview 无生产 Secret；只有显式变量 true 才开翻译。不要将整个财经内容分支合并到 AI master。
5. 在**最新远端** world-state/state.json 中保留 sent_dates/delivery_intents，仅添加经过审核的 translation_budget。示例只是结构，不能当作已核验余额：

```json
{
  "version": 1,
  "months": {
    "YYYY-MM": {
      "month": "YYYY-MM",
      "limit": 300000,
      "baseline": 0,
      "reserved": 0,
      "reservations": {}
    }
  }
}
```

limit 可低于 300000；baseline 是已消耗或需保守扣除的本程序预算额度，reserved 初值等于 baseline。只有已确认本程序没有旧调用且可分配免费余量充足时才允许 baseline=0。未知时保持关闭。不可删除/退款旧月份，新的月份也需人工核验后显式添加。

6. 初始化提交成功后的完整 40 位 SHA 存为 Environment variable WORLD_TRANSLATION_LEDGER_ANCHOR。待审查/小样完成，最后将 WORLD_ALIBABA_TRANSLATION_ENABLED 设为 true。变量不存在、只有密钥或账本不存在都不会调用阿里。
7. 用户已允许保护就绪后一次最小公开短句真实验证，本次尚未执行。小样也必须先走同一远程预算预留，建议 Hello.，保守预留 6 单元；不发送邮件，不用无账本的 curl/SDK 示例。当前未新增无保护 probe 入口；后续须使用受审查的单次无邮件入口，核对阿里控制台实际扣额。
8. 小样、账本和预览都通过后开启正式日报，不补发当天邮件。检查次日启动、翻译数量、持久状态、SMTP accepted；accepted 不等于独立验证收件箱送达。

## 故障处理与回退

budget/history/protection 不可核验：保留原文，不调用腾讯、不重置账本、不改锚点绕过。普通 push 失败/结果不明：保留邮件未决状态，停止发送，人工核对远端和 SMTP 后处理。阿里失败/数字变化：回退原文，不退额度、不重试。月底暂停等待新月核验。

上线回退优先关闭 WORLD_ALIBABA_TRANSLATION_ENABLED，继续原文邮件。不要回退到不保存 translation_budget 的旧 StateStore；代码回退仍须保留账本字段。

## 腾讯 Secret 依赖审计

已核查远端四个活跃分支：master、world-finance-daily、fix/sources-and-ranking、test/x-sources。master/fix 中 daily.yml、ai-debug.yml、mail-test.yml 使用 ai-daily-production 的腾讯 Secrets；两个财经 launcher 使用 world-finance-production 的同名 Secret。test/x-sources 较旧，无这两个名字。

两个 Environment 都存在腾讯 Secrets，仓库层级 Secret 为空。本地候选版移除财经两个 workflow 的腾讯映射及调用；远端还未部署，因此财经仍依赖腾讯。**现在不要删除腾讯 Secrets，也不要注销可能共享的腾讯云密钥。** 财经切换验证完成后才能另行评估仅删除财经 Environment 副本；AI Environment 继续保留。

## 官方依据与费用边界

- API：https://help.aliyun.com/en/machine-translation/developer-reference/api-alimt-2018-10-12-translategeneral
- RPC 签名与公开测试向量：https://help.aliyun.com/en/sdk/product-overview/rpc-mechanism
- 定价：https://help.aliyun.com/zh/machine-translation/product-overview/pricing-of-machine-translation-general-purpose-edition

国内通用版主账号每月 100 万免费字符，RAM 共享；超额自动后付费 50 元/百万，显示延迟约一小时。未找到可关闭后付费的官方硬开关，零余额也不能保证不欠费。程序的 300000 上限只是本程序消费控制。
