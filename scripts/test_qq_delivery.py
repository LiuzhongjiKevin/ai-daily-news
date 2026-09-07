"""Send one explicitly requested, clearly labelled connectivity test email."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ai_daily.mail import MailError, QQMailer
from ai_daily.render import RenderedDigest


def main() -> int:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    rendered = RenderedDigest(
        subject=f"[AI 新闻日报 · 邮件测试] {today}",
        text="这是一封邮件测试。\nQQ 邮箱发送 → Outlook 接收。\n"
             "本次未采集新闻、未调用 AI，模型费用为零。\n收到此邮件即可确认本次投递成功。",
        html="<h1>AI 新闻日报 · 邮件测试</h1><p>QQ 邮箱发送 → Outlook 接收。</p>"
             "<p>本次未采集新闻、未调用 AI，模型费用为零。</p>"
             "<p>收到此邮件即可确认本次投递成功。</p>",
        markdown="",
    )
    try:
        QQMailer.from_environment().send(rendered)
    except MailError as error:
        print(str(error))
        return 1
    print("QQ SMTP accepted the test email. Check the recipient inbox and spam folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
