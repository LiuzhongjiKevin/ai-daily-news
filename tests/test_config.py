from pathlib import Path

from ai_daily.config import load_prices, load_settings


def test_loads_required_defaults(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.yaml").write_text(
        "timezone: Asia/Shanghai\n"
        "ai_mode: full\n"
        "ai_base_url: https://api.deepseek.com\n"
        "ai_model: deepseek-v4-flash\n"
        "ai_max_output_tokens: 2500\n"
        "max_news_candidates: 12\n"
        "min_news_score: 40\n"
        "github_top_n: 10\n"
        "github_window_days: 7\n"
        "snapshot_retention_days: 35\n",
        encoding="utf-8",
    )
    (tmp_path / "config/pricing.yaml").write_text(
        "effective_date: 2026-08-24\n"
        "currency: USD\n"
        "models:\n"
        "  deepseek-v4-flash:\n"
        "    input_cache_hit: 0.014\n"
        "    input_cache_miss: 0.44\n"
        "    output: 1.32\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)
    prices = load_prices(tmp_path)

    assert settings.timezone == "Asia/Shanghai"
    assert settings.max_news_candidates == 12
    assert settings.min_news_score == 40
    assert settings.github_window_days == 7
    assert prices.models["deepseek-v4-flash"].output == 1.32
