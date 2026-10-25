"""robots.txt rules per RFC 9309, parsed with protego.

The standard library's RobotFileParser is not used: it ignores the '*' and '$' wildcards,
applies the first matching rule instead of the longest, and its read() method would fetch
the file with a Python-urllib User-Agent. The file is fetched by our own client instead.
"""

from dataclasses import dataclass

from protego import Protego

# RFC 9309 section 2.5: crawlers must parse at least the first 500 KiB.
MAX_ROBOTS_BYTES = 500 * 1024
# A host asking for longer gaps than this is skipped rather than slowed to a crawl.
MAX_CRAWL_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class RobotsRules:
    """None means no rules: robots.txt was absent, which allows everything."""

    parser: Protego | None = None

    def allows(self, url: str, user_agent: str) -> bool:
        return self.parser is None or self.parser.can_fetch(url, user_agent)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None


ALLOW_ALL = RobotsRules()


def parse_robots(body: bytes) -> RobotsRules:
    # utf-8-sig drops a byte-order mark that would otherwise hide the first rule.
    text = body[:MAX_ROBOTS_BYTES].decode("utf-8-sig", "replace")
    return RobotsRules(Protego.parse(text))
