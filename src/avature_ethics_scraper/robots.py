"""robots.txt retrieval and authorization checks."""

from __future__ import annotations

import urllib.robotparser
from dataclasses import dataclass

import requests

from .models import RobotsDecision
from .urls import robots_url_for


@dataclass(frozen=True)
class RobotsPolicy:
    user_agent: str
    timeout_seconds: float = 15.0

    def check(self, url: str) -> RobotsDecision:
        robots_url = robots_url_for(url)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        try:
            response = requests.get(
                robots_url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.user_agent},
            )
        except requests.RequestException as exc:
            return RobotsDecision(
                url=url,
                robots_url=robots_url,
                allowed=True,
                has_robots_txt=False,
                reason=f"robots.txt could not be retrieved ({exc.__class__.__name__}); proceeding as no explicit rule was available.",
                user_agent=self.user_agent,
            )

        if response.status_code == 404:
            return RobotsDecision(
                url=url,
                robots_url=robots_url,
                allowed=True,
                has_robots_txt=False,
                reason="No robots.txt was found; no explicit robots exclusion rule applies.",
                user_agent=self.user_agent,
            )

        if response.status_code >= 400:
            return RobotsDecision(
                url=url,
                robots_url=robots_url,
                allowed=True,
                has_robots_txt=False,
                reason=f"robots.txt returned HTTP {response.status_code}; proceeding because no usable explicit rule was available.",
                user_agent=self.user_agent,
            )

        parser.parse(response.text.splitlines())
        allowed = parser.can_fetch(self.user_agent, url)
        return RobotsDecision(
            url=url,
            robots_url=robots_url,
            allowed=allowed,
            has_robots_txt=True,
            reason=(
                "robots.txt allows this URL for the selected user agent."
                if allowed
                else "robots.txt disallows this URL for the selected user agent."
            ),
            user_agent=self.user_agent,
        )
