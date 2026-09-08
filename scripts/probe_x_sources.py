"""One-shot public X diagnostics. Never imports the production runner or sends mail."""

import json
import re

import httpx
from bs4 import BeautifulSoup

ACCOUNTS = ("OpenAI", "OpenAIDevs", "claudeai", "AnthropicAI")
DATE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b"
)


def inspect_page(html, account):
    soup = BeautifulSoup(html, "html.parser")
    posts = {}
    for article in soup.select("article"):
        text = article.get_text(" ", strip=True)
        for link in article.select('a[href*="/status/"]'):
            match = re.fullmatch(
                rf"(?:https://(?:x|twitter)\.com)?/{re.escape(account)}/status/(\d+)/?",
                link.get("href", ""),
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            stamp = article.select_one("time[datetime]")
            month_day = DATE.search(text)
            date = stamp.get("datetime") if stamp else (
                month_day.group() if month_day else None
            )
            posts[match.group(1)] = {
                "url": f"https://x.com/{account}/status/{match.group(1)}",
                "display_date": date,
                "date_precision": "datetime" if stamp else "month-day-only",
                "card_text": text[:1200],
                "truncated": "Show more" in text,
            }
    return {
        "cards": len(soup.select("article")),
        "posts": list(posts.values()),
        "dated_posts": sum(bool(post["display_date"]) for post in posts.values()),
    }


def main():
    results = []
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for account in ACCOUNTS:
            result = {"account": account}
            try:
                response = client.get(f"https://x.com/{account}")
                result.update(status=response.status_code, final_url=str(response.url))
                response.raise_for_status()
                result.update(inspect_page(response.text, account))
                result["extractable"] = result["dated_posts"] > 0
            except httpx.HTTPError as error:
                result.update(error=type(error).__name__, extractable=False)
            results.append(result)
    print(json.dumps({"single_run_only": True, "results": results}, indent=2))
    return 0 if all(item["extractable"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
