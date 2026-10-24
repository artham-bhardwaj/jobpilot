"""robots.txt evaluation following RFC 9309 (the Robots Exclusion Protocol).

Python's ``urllib.robotparser`` applies the *first* rule in file order, so a site that writes
``Disallow: /`` followed by ``Allow: /api/jobs`` (Microsoft's careers site does) is wrongly treated as
fully disallowed. RFC 9309 says the most specific (longest) matching rule wins, and on a tie ``Allow`` wins.

- Groups: the group(s) naming our product token (e.g. ``JobPilot``) apply; otherwise the ``*`` group(s);
  several groups for the same agent are combined. No matching group means everything is allowed.
- Patterns: ``*`` matches any characters, a trailing ``$`` anchors the end; matched against path + query.
- ``/robots.txt`` itself is always allowed.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


def _product(user_agent: str) -> str:
    return (re.match(r"[A-Za-z_-]+", user_agent or "") or [""])[0].lower()


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return re.compile("".join(".*" if c == "*" else re.escape(c) for c in unquote(body)) + ("$" if anchored else ""))


class RobotsRules:
    """Same small interface as ``urllib.robotparser.RobotFileParser`` (``parse``, ``can_fetch``, flags)."""

    def __init__(self) -> None:
        self.allow_all = False
        self.disallow_all = False
        self._groups: list[tuple[list[str], list[tuple[bool, str]]]] = []

    def parse(self, lines: list[str]) -> None:
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_rules = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:  # a user-agent line after rules starts a new group
                    self._groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow") and agents:
                in_rules = True
                if value:  # an empty Disallow means "nothing disallowed": no rule
                    rules.append((key == "allow", value))
        if agents:
            self._groups.append((agents, rules))

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        product = _product(user_agent)
        named = [r for agents, rules in self._groups if product and product in agents for r in rules]
        if named or any(product and product in agents for agents, _ in self._groups):
            return named
        return [r for agents, rules in self._groups if "*" in agents for r in rules]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = unquote((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None  # (pattern length, allowed)
        for allowed, pattern in self._rules_for(user_agent):
            if _compile(pattern).match(path):
                key = (len(pattern), allowed)  # longer wins; on equal length True (allow) sorts higher
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]
