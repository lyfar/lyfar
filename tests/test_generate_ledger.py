from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "github.json"
SPEC = importlib.util.spec_from_file_location(
    "generate_ledger", ROOT / "scripts" / "generate_ledger.py"
)
assert SPEC and SPEC.loader
ledger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ledger
SPEC.loader.exec_module(ledger)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_normalizes_separate_evidence_states(self) -> None:
        rows, observed_at = ledger.normalize(self.data)
        by_number = {row["number"]: row for row in rows}

        self.assertEqual(by_number[179]["build"], "PASS")
        self.assertEqual(by_number[179]["automated"], "APPROVE")
        self.assertEqual(by_number[179]["human"], "NONE")
        self.assertEqual(by_number[179]["lifecycle"], "MERGED")
        self.assertEqual(by_number[271]["lifecycle"], "DRAFT")
        self.assertEqual(by_number[272]["scope"], "3-INPUT CONDITIONAL")
        self.assertTrue(by_number[272]["conditional"])
        self.assertEqual(by_number[273]["lifecycle"], "OPEN")
        self.assertEqual(observed_at.isoformat(), "2026-07-22T02:44:17+00:00")

    def test_stale_automated_review_is_not_reported_as_approval(self) -> None:
        changed = copy.deepcopy(self.data)
        changed["pulls"][1]["head_sha"] = "f" * 40
        rows, _ = ledger.normalize(changed)
        by_number = {row["number"]: row for row in rows}
        self.assertEqual(by_number[271]["automated"], "STALE")

    def test_human_review_excludes_author_and_bots(self) -> None:
        reviews = [
            {
                "author": "lyfar",
                "author_type": "User",
                "state": "APPROVED",
                "submitted_at": "2026-07-22T01:00:00Z",
            },
            {
                "author": "review-bot",
                "author_type": "Bot",
                "state": "APPROVED",
                "submitted_at": "2026-07-22T02:00:00Z",
            },
        ]
        self.assertEqual(ledger.human_review(reviews, "lyfar"), "NONE")
        reviews.append(
            {
                "author": "maintainer",
                "author_type": "User",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-22T03:00:00Z",
            }
        )
        self.assertEqual(ledger.human_review(reviews, "lyfar"), "CHANGES")

    def test_human_review_uses_each_reviewers_latest_state(self) -> None:
        reviews = [
            {
                "author": "reviewer-one",
                "author_type": "User",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-22T01:00:00Z",
            },
            {
                "author": "reviewer-one",
                "author_type": "User",
                "state": "APPROVED",
                "submitted_at": "2026-07-22T03:00:00Z",
            },
            {
                "author": "reviewer-two",
                "author_type": "User",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-07-22T02:00:00Z",
            },
        ]
        self.assertEqual(ledger.human_review(reviews, "lyfar"), "CHANGES")

        reviews.append(
            {
                "author": "reviewer-two",
                "author_type": "User",
                "state": "APPROVED",
                "submitted_at": "2026-07-22T04:00:00Z",
            }
        )
        self.assertEqual(ledger.human_review(reviews, "lyfar"), "APPROVED")

    def test_authenticated_api_error_is_not_retried_anonymously(self) -> None:
        error = ledger.urllib.error.HTTPError(
            "https://api.github.com/test", 403, "forbidden", {}, None
        )
        client = ledger.GitHubClient("secret-token")
        with mock.patch.object(
            ledger.urllib.request, "urlopen", side_effect=error
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "returned 403"):
                client.get("/test")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(client.token, "secret-token")

    def test_all_rendered_assets_are_accessible_xml(self) -> None:
        rows, observed_at = ledger.normalize(self.data)
        rendered = ledger.render_all(rows, observed_at)
        self.assertEqual(set(rendered), set(ledger.OUTPUT_NAMES.values()))
        for filename, source in rendered.items():
            root = ET.fromstring(source)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertEqual(root.attrib["role"], "img")
            self.assertIn("ledger-title", root.attrib["aria-labelledby"])
            self.assertIn("#272", source)
            self.assertIn("CONDITIONAL", source)
            self.assertNotIn("—", source)
            self.assertIn("data-theme", source, filename)

    def test_theme_text_contrast_meets_wcag_aa(self) -> None:
        def luminance(color: str) -> float:
            channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def ratio(foreground: str, background: str) -> float:
            brighter, darker = sorted(
                (luminance(foreground), luminance(background)), reverse=True
            )
            return (brighter + 0.05) / (darker + 0.05)

        for palette in ledger.PALETTES.values():
            self.assertGreaterEqual(ratio(palette.text, palette.canvas), 4.5)
            self.assertGreaterEqual(ratio(palette.muted, palette.canvas), 4.5)
            self.assertGreaterEqual(ratio(palette.accent, palette.canvas), 4.5)
            self.assertGreaterEqual(
                ratio(palette.accent_text, palette.accent_fill), 4.5
            )

    def test_check_mode_detects_drift_without_writing(self) -> None:
        rows, observed_at = ledger.normalize(self.data)
        rendered = ledger.render_all(rows, observed_at)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            changed = ledger.write_outputs(output, rendered, check=True)
            self.assertEqual(set(changed), set(rendered))
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
