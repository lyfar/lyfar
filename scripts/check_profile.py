#!/usr/bin/env python3
"""Check profile copy, local assets, and optionally external links."""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


BANNED_COPY = {
    "—": "em dash",
    "–": "en dash",
    "seamless": "marketing filler",
    "unleash": "marketing filler",
    "elevate": "marketing filler",
    "game-changer": "marketing filler",
    "at its core": "throat-clearing phrase",
    "here's the thing": "throat-clearing phrase",
    "quietly in use": "design-copy tell",
}
BANNED_MARKUP = ("<script", "<iframe", "<style", "javascript:")
BANNED_HOSTS = {
    "github-readme-stats.vercel.app",
    "github-readme-streak-stats.herokuapp.com",
    "github-profile-trophy.vercel.app",
}
LINK_RE = re.compile(r"(?:href|src|srcset)=[\"']([^\"']+)|\[[^]]*\]\(([^)]+)\)")


def extract_links(text: str) -> list[str]:
    links = []
    for match in LINK_RE.finditer(text):
        value = next(group for group in match.groups() if group)
        links.extend(part.strip().split()[0] for part in value.split(","))
    return sorted(set(links))


def local_checks(readme: Path) -> tuple[list[str], list[str]]:
    source = readme.read_text(encoding="utf-8")
    lowered = source.lower()
    errors = []
    for needle, reason in BANNED_COPY.items():
        if needle.lower() in lowered:
            errors.append(f"README contains {reason}: {needle!r}")
    for needle in BANNED_MARKUP:
        if needle in lowered:
            errors.append(f"README contains forbidden markup: {needle}")

    links = extract_links(source)
    for link in links:
        parsed = urlparse(link)
        if parsed.netloc in BANNED_HOSTS:
            errors.append(f"README uses a forbidden hosted card: {parsed.netloc}")
        if not parsed.scheme and not link.startswith("#"):
            target = (readme.parent / link).resolve()
            if not target.exists():
                errors.append(f"Missing local target: {link}")
    return errors, links


def check_url(url: str) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "lyfar-profile-link-check"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 400:
                return f"{url} returned HTTP {response.status}"
    except urllib.error.HTTPError as error:
        if error.code == 405:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "lyfar-profile-link-check",
                    "Range": "bytes=0-0",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    if response.status >= 400:
                        return f"{url} returned HTTP {response.status}"
            except (urllib.error.HTTPError, urllib.error.URLError) as retry_error:
                return f"{url} failed: {retry_error}"
        else:
            return f"{url} returned HTTP {error.code}"
    except urllib.error.URLError as error:
        return f"{url} failed: {error.reason}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    errors, links = local_checks(args.readme)
    if args.online:
        for link in links:
            if link.startswith(("https://", "http://")):
                result = check_url(link)
                if result:
                    errors.append(result)
    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors))
        return 1
    mode = "local and online" if args.online else "local"
    print(f"Profile checks passed ({mode}; {len(links)} links).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
