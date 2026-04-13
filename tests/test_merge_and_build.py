import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import merge_and_build  # noqa: E402


class UnescapeChunkPayloadTests(unittest.TestCase):
    def test_unescape_newline_cr_backslash(self):
        self.assertEqual(
            merge_and_build.unescape_chunk_payload("a\\nb\\\\c\\r"),
            "a\nb\\c\r",
        )

    def test_empty(self):
        self.assertEqual(merge_and_build.unescape_chunk_payload(""), "")


class ListOutputChunkTxtTests(unittest.TestCase):
    def test_numeric_order_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "output_chunk0002.txt").write_text("", encoding="utf-8")
            Path(d, "output_chunk0010.txt").write_text("", encoding="utf-8")
            Path(d, "output_chunk0001.txt").write_text("", encoding="utf-8")
            paths = merge_and_build.list_output_chunk_txt_paths(d)
            names = [os.path.basename(p) for p in paths]
            self.assertEqual(names, ["output_chunk0001.txt", "output_chunk0002.txt", "output_chunk0010.txt"])


class ParseTranslatedChunksTests(unittest.TestCase):
    def test_parses_lines_and_unescapes(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "output_chunk0001.txt").write_text(
                "T0001: Hello\\nWorld\nT0002: \\\\\n",
                encoding="utf-8",
            )
            segs, warns = merge_and_build.parse_translated_chunks(d)
            self.assertEqual(segs["T0001"], "Hello\nWorld")
            self.assertEqual(segs["T0002"], "\\")
            self.assertEqual(warns, [])


class DedupAliasExpansionTests(unittest.TestCase):
    def test_expands_aliases_from_canonical(self):
        expanded = merge_and_build.apply_dedup_aliases(
            {"T0001": "Bonjour"},
            {"T0001": "T0001", "T0002": "T0001"},
        )
        self.assertEqual(expanded["T0002"], "Bonjour")

    def test_expands_aliases_with_transitive_chain(self):
        expanded = merge_and_build.apply_dedup_aliases(
            {"T0001": "Salut"},
            {"T0001": "T0001", "T0002": "T0001", "T0003": "T0002"},
        )
        self.assertEqual(expanded["T0002"], "Salut")
        self.assertEqual(expanded["T0003"], "Salut")


class ValidateCompletenessTests(unittest.TestCase):
    def test_writes_missing_report(self):
        with tempfile.TemporaryDirectory() as d:
            missing = merge_and_build.validate_translation_completeness(
                {"T0001": "a", "T0002": "b"},
                {"T0001": "x"},
                d,
            )
            self.assertEqual(missing, ["T0002"])
            report = Path(d) / "missing_segments.txt"
            self.assertTrue(report.is_file())
            text = report.read_text(encoding="utf-8")
            self.assertIn("T0002", text)


class ReinjectPlaceholdersTests(unittest.TestCase):
    def test_single_pass_regex_sub(self):
        sk = '<p>{{T0001}}</p><span>{{T0002}}</span>'
        tr = {"T0001": "A{{fake}}", "T0002": "B"}
        out = merge_and_build.reinject_placeholders(sk, tr)
        self.assertEqual(out, "<p>A{{fake}}</p><span>B</span>")


class BuildFullHtmlTests(unittest.TestCase):
    def test_lang_title_and_body(self):
        sk = "<html><head><meta charset=utf-8></head><body><p>{{T0001}}</p></body></html>"
        head = '<meta charset="utf-8"><title>Old</title>'
        tr = {"T0001": "Hi"}
        reinj = merge_and_build.reinject_placeholders(sk, tr)
        html = merge_and_build.build_full_html(reinj, head, "fr", "Nouveau")
        self.assertIn('<html lang="fr">', html)
        self.assertIn("<title>Nouveau</title>", html)
        self.assertIn("<p>Hi</p>", html)
        self.assertNotIn("{{T0001}}", html)


class GenerateCalibreOutputsTests(unittest.TestCase):
    def test_no_crash_when_calibre_missing(self):
        with tempfile.TemporaryDirectory() as d:
            book = Path(d) / "book.html"
            book.write_text("<!DOCTYPE html><html><body>x</body></html>", encoding="utf-8")
            with mock.patch.object(merge_and_build, "find_ebook_convert", return_value=None):
                n, paths = merge_and_build.generate_calibre_outputs(str(d), str(book))
            self.assertEqual(n, 0)
            self.assertEqual(paths, [])


if __name__ == "__main__":
    unittest.main()
