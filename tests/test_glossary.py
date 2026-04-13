import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import glossary  # noqa: E402


class BuildCandidatesTests(unittest.TestCase):
    def test_min_freq_unigram(self):
        segs = {
            "T0001": "alpha beta alpha gamma alpha",
        }
        out = glossary.build_candidates(segs, min_freq=3, max_terms=50)
        terms = [t for t, _ in out]
        self.assertIn("alpha", terms)

    def test_bigram_above_threshold(self):
        segs = {"T0001": "machine learning is machine learning and machine learning"}
        out = glossary.build_candidates(segs, min_freq=3, max_terms=50)
        terms = {t: f for t, f in out}
        self.assertIn("machine learning", terms)
        self.assertGreaterEqual(terms["machine learning"], 3)

    def test_proper_noun_twice(self):
        segs = {
            "T0001": "We visit Paris. The food in Paris is great.",
        }
        out = glossary.build_candidates(segs, min_freq=99, max_terms=50)
        terms = [t for t, _ in out]
        self.assertTrue(any("Paris" in t for t in terms))

    def test_cli_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "segments.json"
            p.write_text(
                '{"T0001": "x y z x y z x y z"}',
                encoding="utf-8",
            )
            rc = glossary.main(["--temp-dir", d, "--olang", "fr", "--min-freq", "3"])
            self.assertEqual(rc, 0)
            out = Path(d) / "glossary_candidates.txt"
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertGreater(len(text.strip()), 0)


if __name__ == "__main__":
    unittest.main()
