import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402
import merge_and_build  # noqa: E402


class DedupConvertTests(unittest.TestCase):
    def test_identical_segments_grouped_and_only_canonical_chunked(self):
        segments = {
            "T0001": "Repeated header",
            "T0002": "Repeated header",
            "T0003": "Unique body",
        }
        dedup_map = convert.build_dedup_map(segments)
        self.assertEqual(dedup_map["T0001"], "T0001")
        self.assertEqual(dedup_map["T0002"], "T0001")
        self.assertEqual(dedup_map["T0003"], "T0003")

        canonical = convert.select_canonical_segments(segments, dedup_map)
        with tempfile.TemporaryDirectory() as d:
            chunks = convert.build_translation_chunks(
                canonical,
                d,
                chunk_size=10_000,
                dedup_map=dedup_map,
            )
            self.assertEqual(chunks, ["chunk0001.txt"])
            text = Path(d, "chunk0001.txt").read_text(encoding="utf-8")
            self.assertIn("T0001: Repeated header", text)
            self.assertIn("T0003: Unique body", text)
            self.assertNotIn("T0002:", text)

    def test_dedup_keeps_segments_separate_when_footnote_context_differs(self):
        segments = {
            "T0001": "Call A",
            "T0002": "Call B",
            "T0003": {"text": "Same note text", "footnote_for": "T0001"},
            "T0004": {"text": "Same note text", "footnote_for": "T0002"},
        }
        dedup_map = convert.build_dedup_map(segments)
        self.assertEqual(dedup_map["T0003"], "T0003")
        self.assertEqual(dedup_map["T0004"], "T0004")


class DedupMergeTests(unittest.TestCase):
    def test_alias_translation_is_inherited_from_canonical(self):
        parsed = {"T0005": "Bonjour"}
        dedup_map = {
            "T0005": "T0005",
            "T0023": "T0005",
            "T0089": "T0005",
        }
        expanded = merge_and_build.apply_dedup_aliases(parsed, dedup_map)
        self.assertEqual(expanded["T0023"], "Bonjour")
        self.assertEqual(expanded["T0089"], "Bonjour")

        skeleton = "<p>{{T0005}} / {{T0023}} / {{T0089}}</p>"
        reinjected = merge_and_build.reinject_placeholders(skeleton, expanded)
        self.assertEqual(reinjected, "<p>Bonjour / Bonjour / Bonjour</p>")

    def test_backward_compatible_without_dedup_map(self):
        parsed = {"T0001": "A", "T0002": "B"}
        expanded = merge_and_build.apply_dedup_aliases(parsed, None)
        self.assertEqual(expanded, parsed)


if __name__ == "__main__":
    unittest.main()
