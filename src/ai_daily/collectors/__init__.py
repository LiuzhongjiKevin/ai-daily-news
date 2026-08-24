from ai_daily.collectors.base import CollectorRegistry, SourceConfig, load_sources
from ai_daily.collectors.discovery import DiscoveryCollector
from ai_daily.collectors.feed import FeedCollector
from ai_daily.collectors.github import GitHubReleaseCollector
from ai_daily.collectors.page import PageCollector
from ai_daily.http import RetryingClient


def build_collector_registry(client: RetryingClient) -> CollectorRegistry:
    return CollectorRegistry(
        {
            "feed": FeedCollector(client),
            "page": PageCollector(client),
            "github_releases": GitHubReleaseCollector(client),
            "discovery": DiscoveryCollector(client),
        }
    )


__all__ = ["CollectorRegistry", "SourceConfig", "build_collector_registry", "load_sources"]
