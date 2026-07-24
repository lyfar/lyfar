#!/usr/bin/env python3
"""Generate light and dark proof-ledger SVGs from public GitHub data."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


UPSTREAM = "Vilin97/lean-pool"
AUTHOR = "lyfar"
# Evidence-reviewed labels only. This map does not control PR discovery.
SCOPE_OVERRIDES = {
    179: ("EGRS75 two-prime theorem", "KNOWN THEOREM", False),
    270: ("EGRS75 dependency clarification", "DOCUMENTATION", False),
    271: ("Euclidean distance geometry", "CLASSICAL THEORY", False),
    272: ("Erdos 132, fourteen-point case", "3-INPUT CONDITIONAL", True),
    273: ("GKP divisibility via ternary carries", "PARTIAL RESULT", False),
    274: ("Hadwiger-Nelson known bounds", "KNOWN BOUNDS", False),
    275: ("de Bruijn-Erdos compactness", "KNOWN THEOREM", False),
    276: ("Odd-prime valuation distributions", "EXACT RESULT", False),
    277: ("Moser lattice four-colorings", "KNOWN RESULTS", False),
    278: ("Erdos 97 convex-octagon case", "KNOWN CASE", False),
}
DEFAULT_SCOPE = "SCOPE PENDING"
AUTO_REVIEW_MARKER = "<!-- lean-pool-llm-review -->"
BUILD_NAMES = {"build pool", "build project"}
OUTPUT_NAMES = {
    ("light", False): "proof-ledger-light.svg",
    ("dark", False): "proof-ledger-dark.svg",
    ("light", True): "proof-ledger-light-mobile.svg",
    ("dark", True): "proof-ledger-dark-mobile.svg",
}


@dataclass(frozen=True)
class Palette:
    canvas: str
    panel: str
    text: str
    muted: str
    faint: str
    border: str
    accent: str
    accent_fill: str
    accent_text: str
    accent_soft: str


PALETTES = {
    "light": Palette(
        canvas="#f6f8fa",
        panel="#ffffff",
        text="#1f2328",
        muted="#59636e",
        faint="#818b98",
        border="#d0d7de",
        accent="#326d4d",
        accent_fill="#326d4d",
        accent_text="#ffffff",
        accent_soft="#dafbe1",
    ),
    "dark": Palette(
        canvas="#0d1117",
        panel="#161b22",
        text="#f0f6fc",
        muted="#a8b1bb",
        faint="#8c959f",
        border="#30363d",
        accent="#4b8e66",
        accent_fill="#326d4d",
        accent_text="#ffffff",
        accent_soft="#173f2a",
    ),
}


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def get(self, path: str) -> Any:
        url = f"https://api.github.com{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "lyfar-proof-ledger",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"GitHub API returned {error.code} for {path}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"GitHub API request failed for {path}: {error.reason}"
            ) from error


def discover_pull_numbers(client: GitHubClient) -> list[int]:
    numbers = []
    page = 1
    while True:
        query = urlencode(
            {
                "q": f"repo:{UPSTREAM} is:pr author:{AUTHOR}",
                "sort": "created",
                "order": "asc",
                "per_page": 100,
                "page": page,
            }
        )
        result = client.get(f"/search/issues?{query}")
        if result.get("incomplete_results"):
            raise RuntimeError("GitHub returned incomplete pull-request search results")
        items = result["items"]
        numbers.extend(item["number"] for item in items)
        if len(items) < 100:
            return sorted(set(numbers))
        page += 1


def fetch_live(client: GitHubClient | None = None) -> dict[str, Any]:
    client = client or GitHubClient(os.environ.get("GITHUB_TOKEN"))
    pulls = []
    for number in discover_pull_numbers(client):
        pull = client.get(f"/repos/{UPSTREAM}/pulls/{number}")
        head_sha = pull["head"]["sha"]
        checks = client.get(
            f"/repos/{UPSTREAM}/commits/{head_sha}/check-runs?per_page=100"
        )["check_runs"]
        comments = client.get(
            f"/repos/{UPSTREAM}/issues/{number}/comments?per_page=100"
        )
        reviews = client.get(f"/repos/{UPSTREAM}/pulls/{number}/reviews?per_page=100")
        pulls.append(
            {
                "number": number,
                "title": pull["title"],
                "state": pull["state"],
                "draft": pull["draft"],
                "merged_at": pull["merged_at"],
                "merged_by": (pull.get("merged_by") or {}).get("login"),
                "author": pull["user"]["login"],
                "head_sha": head_sha,
                "updated_at": pull["updated_at"],
                "url": pull["html_url"],
                "checks": [
                    {
                        "name": item["name"],
                        "status": item["status"],
                        "conclusion": item["conclusion"],
                        "completed_at": item["completed_at"],
                    }
                    for item in checks
                ],
                "comments": [
                    {
                        "body": item["body"],
                        "updated_at": item["updated_at"],
                    }
                    for item in comments
                ],
                "reviews": [
                    {
                        "author": item["user"]["login"],
                        "author_type": item["user"]["type"],
                        "state": item["state"],
                        "submitted_at": item["submitted_at"],
                    }
                    for item in reviews
                ],
            }
        )
    return {"repository": UPSTREAM, "pulls": pulls}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_status(checks: list[dict[str, Any]]) -> str:
    builds = [item for item in checks if item["name"].lower() in BUILD_NAMES]
    if not builds:
        return "NO RECORD"
    if any(item["status"] != "completed" for item in builds):
        return "RUNNING"
    if any(item["conclusion"] not in {"success", "skipped"} for item in builds):
        return "FAIL"
    if any(item["conclusion"] == "skipped" for item in builds):
        return "SKIPPED"
    return "PASS"


def automated_review(comments: list[dict[str, Any]], head_sha: str) -> str:
    matching = [item for item in comments if AUTO_REVIEW_MARKER in item["body"]]
    if not matching:
        return "NO RECORD"
    latest = max(matching, key=lambda item: item.get("updated_at") or "")
    reviewed = re.search(r"\*\*Reviewed head:\*\*\s*`([0-9a-f]+)`", latest["body"])
    if not reviewed or reviewed.group(1) != head_sha:
        return "STALE"
    verdict = re.search(r"\*\*Verdict:\*\*.*?`([^`]+)`", latest["body"])
    return short_label(verdict.group(1).upper(), 18) if verdict else "POSTED"


def human_review(reviews: list[dict[str, Any]], author: str) -> str:
    human = [
        item
        for item in reviews
        if item["author"] != author and item.get("author_type") != "Bot"
    ]
    if not human:
        return "NONE"

    latest_by_reviewer: dict[str, dict[str, Any]] = {}
    for item in human:
        reviewer = item["author"]
        previous = latest_by_reviewer.get(reviewer)
        if previous is None or (item.get("submitted_at") or "") > (
            previous.get("submitted_at") or ""
        ):
            latest_by_reviewer[reviewer] = item

    states = {
        "APPROVED": "APPROVED",
        "CHANGES_REQUESTED": "CHANGES",
        "COMMENTED": "COMMENTED",
        "DISMISSED": "DISMISSED",
    }
    active_states = {item["state"] for item in latest_by_reviewer.values()}
    for state in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED", "DISMISSED"):
        if state in active_states:
            return states[state]
    return sorted(active_states)[0]


def lifecycle(pull: dict[str, Any]) -> str:
    if pull.get("merged_at"):
        return "MERGED"
    if pull["state"] != "open":
        return pull["state"].upper()
    if pull.get("draft"):
        return "DRAFT"
    return pull["state"].upper()


def short_label(title: str, limit: int = 40) -> str:
    label = re.sub(r"\s+", " ", title.replace("–", "-").replace("—", "-")).strip()
    if len(label) <= limit:
        return label
    return label[: limit - 3].rstrip() + "..."


def normalize(data: dict[str, Any]) -> tuple[list[dict[str, Any]], datetime]:
    rows = []
    timestamps: list[datetime] = []
    for pull in sorted(data["pulls"], key=lambda item: item["number"]):
        number = pull["number"]
        classification = SCOPE_OVERRIDES.get(number)
        if classification:
            label, scope, conditional = classification
        else:
            label, scope, conditional = (
                short_label(pull["title"]),
                DEFAULT_SCOPE,
                False,
            )
        for value in [pull.get("updated_at"), pull.get("merged_at")]:
            parsed = parse_timestamp(value)
            if parsed:
                timestamps.append(parsed)
        for item in pull.get("checks", []):
            parsed = parse_timestamp(item.get("completed_at"))
            if parsed:
                timestamps.append(parsed)
        for item in pull.get("comments", []):
            parsed = parse_timestamp(item.get("updated_at"))
            if parsed:
                timestamps.append(parsed)
        for item in pull.get("reviews", []):
            parsed = parse_timestamp(item.get("submitted_at"))
            if parsed:
                timestamps.append(parsed)

        rows.append(
            {
                "number": number,
                "label": label,
                "title": pull["title"],
                "scope": scope,
                "conditional": conditional,
                "classified": classification is not None,
                "build": build_status(pull.get("checks", [])),
                "automated": automated_review(
                    pull.get("comments", []), pull["head_sha"]
                ),
                "human": human_review(pull.get("reviews", []), pull["author"]),
                "lifecycle": lifecycle(pull),
                "merged_by": pull.get("merged_by"),
                "url": pull["url"],
            }
        )
    observed_at = max(timestamps, default=datetime.now(timezone.utc))
    return rows, observed_at


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def text(
    x: int,
    y: int,
    value: str,
    *,
    fill: str,
    size: int,
    weight: int = 400,
    family: str = "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
    anchor: str = "start",
    spacing: str | None = None,
) -> str:
    letter_spacing = f' letter-spacing="{spacing}"' if spacing else ""
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}"'
        f"{letter_spacing}>{esc(value)}</text>"
    )


def pill(
    x: int, y: int, width: int, label: str, palette: Palette, positive: bool
) -> str:
    fill = palette.accent_fill if positive else palette.panel
    stroke = palette.accent if positive else palette.border
    ink = palette.accent_text if positive else palette.muted
    return "".join(
        [
            f'<rect x="{x}" y="{y}" width="{width}" height="26" rx="6" '
            f'fill="{fill}" stroke="{stroke}"/>',
            text(
                x + width // 2,
                y + 18,
                label,
                fill=ink,
                size=11,
                weight=650,
                family="ui-monospace,SFMono-Regular,Consolas,monospace",
                anchor="middle",
                spacing="0.2",
            ),
        ]
    )


def svg_open(
    width: int, height: int, theme: str, mobile: bool, contribution_count: int
) -> list[str]:
    palette = PALETTES[theme]
    layout = "mobile" if mobile else "desktop"
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-labelledby="ledger-title ledger-desc" data-theme="{theme}" data-layout="{layout}">',
        '<title id="ledger-title">Public proof ledger</title>',
        f'<desc id="ledger-desc">Live status of {contribution_count} Lean Pool '
        "contributions. Each row separates claim scope, Lean build checks, "
        "automated editorial review, formal human review, and pull request state.</desc>",
        f'<rect width="{width}" height="{height}" rx="12" fill="{palette.canvas}"/>',
    ]


def render_header(
    parts: list[str], palette: Palette, width: int, observed_at: datetime, mobile: bool
) -> None:
    left = 24 if mobile else 32
    parts.append(
        text(
            left,
            34,
            "PUBLIC FORMAL WORK",
            fill=palette.accent,
            size=11,
            weight=700,
            spacing="1.5",
        )
    )
    parts.append(text(left, 68, "Proof ledger", fill=palette.text, size=28, weight=700))
    stamp = observed_at.astimezone(timezone.utc).strftime(
        "Data through %Y-%m-%d %H:%M UTC"
    )
    parts.append(text(left, 94, stamp, fill=palette.muted, size=13))
    if not mobile:
        parts.append(
            text(
                width - 32,
                68,
                "GitHub + Vilin97/lean-pool",
                fill=palette.muted,
                size=13,
                anchor="end",
            )
        )


def render_desktop(
    rows: list[dict[str, Any]], observed_at: datetime, theme: str
) -> str:
    width = 1200
    panel_bottom = 170 + len(rows) * 60
    height = panel_bottom + 114
    palette = PALETTES[theme]
    parts = svg_open(width, height, theme, False, len(rows))
    render_header(parts, palette, width, observed_at, False)
    parts.append(
        f'<rect x="20" y="116" width="1160" height="{panel_bottom - 116}" rx="10" '
        f'fill="{palette.panel}" stroke="{palette.border}"/>'
    )
    headers = [
        (34, "CONTRIBUTION"),
        (430, "SCOPE"),
        (630, "LEAN BUILD"),
        (772, "AUTO REVIEW"),
        (920, "HUMAN REVIEW"),
        (1080, "STATE"),
    ]
    for x, label in headers:
        parts.append(
            text(x, 148, label, fill=palette.faint, size=10, weight=700, spacing="0.9")
        )
    parts.append(
        f'<line x1="20" y1="164" x2="1180" y2="164" stroke="{palette.border}"/>'
    )

    for index, row in enumerate(rows):
        top = 164 + index * 60
        if index:
            parts.append(
                f'<line x1="34" y1="{top}" x2="1166" y2="{top}" stroke="{palette.border}"/>'
            )
        parts.append(
            text(
                34,
                top + 25,
                f"#{row['number']}",
                fill=palette.accent,
                size=13,
                weight=700,
                family="ui-monospace,SFMono-Regular,Consolas,monospace",
            )
        )
        parts.append(
            text(82, top + 25, row["label"], fill=palette.text, size=15, weight=600)
        )
        scope_positive = row["conditional"]
        parts.append(pill(430, top + 14, 174, row["scope"], palette, scope_positive))
        parts.append(
            pill(630, top + 14, 112, row["build"], palette, row["build"] == "PASS")
        )
        parts.append(
            pill(
                772,
                top + 14,
                132,
                row["automated"],
                palette,
                row["automated"] == "APPROVE",
            )
        )
        parts.append(
            pill(920, top + 14, 126, row["human"], palette, row["human"] == "APPROVED")
        )
        parts.append(
            pill(
                1080,
                top + 14,
                82,
                row["lifecycle"],
                palette,
                row["lifecycle"] == "MERGED",
            )
        )

    parts.append(
        text(
            32,
            panel_bottom + 30,
            "PASS means the current PR revision completed the Lean Pool build checks reported by GitHub.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            32,
            panel_bottom + 52,
            "AUTO REVIEW is the repository's automated editorial verdict. HUMAN REVIEW counts formal GitHub reviews.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            32,
            panel_bottom + 74,
            "CONDITIONAL marks a theorem checked after assuming named published inputs. Lean does not check those inputs here.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            32,
            panel_bottom + 96,
            "SCOPE PENDING is the conservative default for a newly discovered PR until its claim boundary is classified.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_mobile(rows: list[dict[str, Any]], observed_at: datetime, theme: str) -> str:
    width = 480
    rows_bottom = 102 + len(rows) * 132
    height = rows_bottom + 124
    palette = PALETTES[theme]
    parts = svg_open(width, height, theme, True, len(rows))
    render_header(parts, palette, width, observed_at, True)
    for index, row in enumerate(rows):
        top = 116 + index * 132
        parts.append(
            f'<rect x="16" y="{top}" width="448" height="118" rx="10" '
            f'fill="{palette.panel}" stroke="{palette.border}"/>'
        )
        parts.append(
            text(
                28,
                top + 26,
                f"#{row['number']}",
                fill=palette.accent,
                size=13,
                weight=700,
                family="ui-monospace,SFMono-Regular,Consolas,monospace",
            )
        )
        parts.append(
            text(76, top + 26, row["label"], fill=palette.text, size=14, weight=650)
        )
        parts.append(pill(28, top + 42, 154, row["scope"], palette, row["conditional"]))
        parts.append(
            pill(192, top + 42, 88, row["build"], palette, row["build"] == "PASS")
        )
        parts.append(
            pill(
                290,
                top + 42,
                150,
                row["automated"],
                palette,
                row["automated"] == "APPROVE",
            )
        )
        parts.append(
            pill(
                28,
                top + 78,
                116,
                f"HUMAN {row['human']}",
                palette,
                row["human"] == "APPROVED",
            )
        )
        parts.append(
            pill(
                154,
                top + 78,
                88,
                row["lifecycle"],
                palette,
                row["lifecycle"] == "MERGED",
            )
        )
    parts.append(
        text(
            20,
            rows_bottom + 32,
            "Lean build, automated review, human review, and PR state stay separate.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            20,
            rows_bottom + 54,
            "CONDITIONAL means the proof depends on named published inputs.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            20,
            rows_bottom + 76,
            "SCOPE PENDING is the default for a newly discovered pull request.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append(
        text(
            20,
            rows_bottom + 98,
            "Source: public GitHub API and Vilin97/lean-pool.",
            fill=palette.muted,
            size=12,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_all(rows: list[dict[str, Any]], observed_at: datetime) -> dict[str, str]:
    rendered = {}
    for (theme, mobile), filename in OUTPUT_NAMES.items():
        rendered[filename] = (
            render_mobile(rows, observed_at, theme)
            if mobile
            else render_desktop(rows, observed_at, theme)
        )
    return rendered


def write_outputs(output_dir: Path, rendered: dict[str, str], check: bool) -> list[str]:
    changed = []
    for filename, content in rendered.items():
        path = output_dir / filename
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            changed.append(filename)
            if not check:
                output_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, help="Use a local JSON fixture")
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument(
        "--check", action="store_true", help="Fail if generated files differ"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fixture:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
    else:
        data = fetch_live()
    rows, observed_at = normalize(data)
    changed = write_outputs(args.output_dir, render_all(rows, observed_at), args.check)
    if changed:
        print("Changed: " + ", ".join(changed))
        return 1 if args.check else 0
    print("Proof ledger is current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
