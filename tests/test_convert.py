import sys
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402


class EscapeChunkPayloadTests(unittest.TestCase):
    def test_escapes_newlines_and_backslash(self):
        self.assertEqual(convert._escape_chunk_payload("a\nb"), "a\\nb")
        self.assertEqual(convert._escape_chunk_payload("x\ry"), "x\\ry")
        self.assertEqual(convert._escape_chunk_payload(r"a\b"), r"a\\b")


class ExtractSegmentsAndSkeletonTests(unittest.TestCase):
    def test_placeholders_in_body(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head>"
            "<body><p>Hello</p><p>World</p></body></html>"
        )
        skel, head_inner, segments = convert.extract_segments_and_skeleton(html)
        self.assertIn("{{T0001}}", skel)
        self.assertIn("{{T0002}}", skel)
        self.assertEqual(segments["T0001"], "Hello")
        self.assertEqual(segments["T0002"], "World")
        self.assertIn("title", head_inner.lower())

    def test_svg_inline_text_is_not_extracted(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head>"
            "<body><p>Before</p>"
            "<svg viewBox='0 0 100 100'><text x='10' y='10'>SVG Label</text></svg>"
            "<p>After</p></body></html>"
        )
        skel, _head_inner, segments = convert.extract_segments_and_skeleton(html)
        self.assertEqual(list(segments.values()), ["Before", "After"])
        self.assertIn("SVG Label", skel)
        self.assertNotIn("{{T0002}}", skel.split("<svg", 1)[1].split("</svg>", 1)[0])


class MarkdownToHtmlTests(unittest.TestCase):
    def test_markdown_to_html_supports_common_blocks(self):
        md = (
            "# Title\n\n"
            "Paragraph with **bold** and *italic* plus [link](https://example.com).\n\n"
            "- Item one\n"
            "- Item two\n\n"
            "![img alt](images/p1.png)\n\n"
            "```python\nprint('x')\n```\n"
        )
        html = convert.markdown_to_html(md)
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn('<a href="https://example.com">link</a>', html)
        self.assertIn("<ul>", html)
        self.assertIn('<img alt="img alt" src="images/p1.png"/>', html)
        self.assertIn("<pre><code>", html)


class PdfEngineSelectionTests(unittest.TestCase):
    @mock.patch("convert.find_marker_single", return_value="/usr/bin/marker_single")
    @mock.patch("convert.detect_pdf_structure")
    def test_auto_prefers_marker_when_available(self, detect_mock, _marker_mock):
        engine, marker_cmd, pdf_type = convert.choose_pdf_engine("book.pdf", "auto")
        self.assertEqual(engine, "marker")
        self.assertEqual(marker_cmd, "/usr/bin/marker_single")
        self.assertEqual(pdf_type, "auto")
        detect_mock.assert_not_called()

    @mock.patch("convert.find_marker_single", return_value=None)
    @mock.patch("builtins.print")
    @mock.patch("convert.detect_pdf_structure")
    def test_auto_falls_back_to_calibre_when_marker_missing(self, detect_mock, print_mock, _marker_mock):
        engine, marker_cmd, pdf_type = convert.choose_pdf_engine("book.pdf", "auto")
        self.assertEqual(engine, "calibre")
        self.assertIsNone(marker_cmd)
        self.assertEqual(pdf_type, "auto")
        detect_mock.assert_not_called()
        print_mock.assert_any_call(
            "Warning: marker-pdf non installé, utilisation de Calibre pour le PDF. "
            "Installer marker-pdf est recommandé pour une meilleure extraction."
        )


class DetectPdfStructureTests(unittest.TestCase):
    def _pdfinfo_ok(self, pages=5, width=612.0):
        stdout = (
            "Title: Sample Document\n"
            f"Pages:          {pages}\n"
            f"Page size:      {width} x 792 pts (letter)\n"
        )
        return subprocess.CompletedProcess(
            args=["pdfinfo", "book.pdf"], returncode=0, stdout=stdout, stderr=""
        )

    def _pdftotext_ok(self, text):
        return subprocess.CompletedProcess(
            args=["pdftotext", "-layout", "book.pdf", "-"],
            returncode=0,
            stdout=text,
            stderr="",
        )

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_simple(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        text = (
            "Linear paragraph line one\n"
            "Linear paragraph line two\n"
            "Another normal line\n"
            "\f"
            "Second page standard text\n"
            "Still linear text flow\n"
        )
        run_mock.side_effect = [self._pdfinfo_ok(pages=2, width=612.0), self._pdftotext_ok(text)]

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "simple")
        self.assertEqual(result.indicators, [])
        self.assertEqual(result.warnings, [])

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_columns(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        indented = " " * 45 + "column text"
        text = "\n".join(
            [
                indented,
                indented,
                indented,
                indented,
                "regular line",
                "regular line",
                "regular line",
                "regular line",
                "regular line",
                "regular line",
            ]
        )
        run_mock.side_effect = [self._pdfinfo_ok(pages=1, width=612.0), self._pdftotext_ok(text)]

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "complex")
        self.assertTrue(any("high_indentation_ratio" in x for x in result.indicators))

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_footnotes(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        page = (
            "Academic paper title\n"
            "Main body first line\n"
            "Main body second line\n"
            "Main body third line\n"
            "Main body fourth line\n"
            "Main body fifth line\n"
            "1 short footnote text\n"
            "2 another footnote\n"
        )
        text = "\f".join([page, page, page])
        run_mock.side_effect = [self._pdfinfo_ok(pages=3, width=612.0), self._pdftotext_ok(text)]

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "complex")
        self.assertIn("recurrent_footnote_patterns_detected", result.indicators)

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_repeated_headers_or_footers(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        pages = []
        for i in range(1, 6):
            pages.append(
                "\n".join(
                    [
                        "Journal of Testing 2026",
                        f"Body content line page {i}",
                        "More body content",
                        "Page footer fixed",
                    ]
                )
            )
        text = "\f".join(pages)
        run_mock.side_effect = [self._pdfinfo_ok(pages=5, width=612.0), self._pdftotext_ok(text)]

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "complex")
        self.assertIn("repeated_headers_or_footers_detected", result.indicators)

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_pdfinfo_filenotfound_fallback_simple(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        run_mock.side_effect = FileNotFoundError("pdfinfo not found")

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "simple")
        self.assertTrue(result.warnings)

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_pdftotext_filenotfound_fallback_simple(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        run_mock.side_effect = [self._pdfinfo_ok(pages=5, width=612.0), FileNotFoundError("pdftotext not found")]

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "simple")
        self.assertTrue(result.warnings)

    @mock.patch("convert.shutil.which")
    @mock.patch("convert.subprocess.run")
    def test_detect_pdf_structure_pdfinfo_non_zero_fallback_simple(self, run_mock, which_mock):
        which_mock.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        run_mock.return_value = subprocess.CompletedProcess(
            args=["pdfinfo", "book.pdf"], returncode=1, stdout="", stderr="error"
        )

        result = convert.detect_pdf_structure("book.pdf")

        self.assertEqual(result.classification, "simple")
        self.assertTrue(result.warnings)
        self.assertEqual(run_mock.call_count, 1)


class AssetCopyTests(unittest.TestCase):
    def test_copy_assets_preserves_svg_files(self):
        with tempfile.TemporaryDirectory() as extract_dir, tempfile.TemporaryDirectory() as temp_dir:
            main_html = Path(extract_dir) / "index.html"
            images_dir = Path(extract_dir) / "images"
            images_dir.mkdir(parents=True, exist_ok=True)

            svg_path = images_dir / "diagram.svg"
            svg_content = "<svg viewBox='0 0 10 10'><text>vector</text></svg>"
            svg_path.write_text(svg_content, encoding="utf-8")
            (images_dir / "photo.png").write_bytes(b"\x89PNG\r\n")
            main_html.write_text("<html><body><img src='images/diagram.svg'/></body></html>", encoding="utf-8")

            assets_root = convert.copy_assets_from_extract(
                str(extract_dir),
                str(main_html),
                str(temp_dir),
            )
            copied_svg = Path(assets_root) / "images" / "diagram.svg"
            self.assertTrue(copied_svg.is_file())
            self.assertEqual(copied_svg.read_text(encoding="utf-8"), svg_content)


class MarkerSvgMappingTests(unittest.TestCase):
    def test_single_figure_per_page_replaces_png_with_svg(self):
        html = (
            "<html><body>"
            "<img src='images/figure_page1.png' alt='p1'/>"
            "<img src='images/figure_page2.png' alt='p2'/>"
            "</body></html>"
        )
        page_svg_map = {1: "vector_page1.svg", 2: "vector_page2.svg"}

        out_html, replaced = convert.replace_marker_png_with_extracted_svg(html, page_svg_map)

        self.assertEqual(replaced, 2)
        self.assertIn("vector_page1.svg", out_html)
        self.assertIn("vector_page2.svg", out_html)
        self.assertNotIn("figure_page1.png", out_html)
        self.assertNotIn("figure_page2.png", out_html)

    def test_multiple_png_same_page_keeps_png_due_to_ambiguity(self):
        html = (
            "<html><body>"
            "<img src='images/plot_page3_a.png' alt='a'/>"
            "<img src='images/plot_page3_b.png' alt='b'/>"
            "</body></html>"
        )
        page_svg_map = {3: "vector_page3.svg"}

        out_html, replaced = convert.replace_marker_png_with_extracted_svg(html, page_svg_map)

        self.assertEqual(replaced, 0)
        self.assertIn("plot_page3_a.png", out_html)
        self.assertIn("plot_page3_b.png", out_html)
        self.assertNotIn("vector_page3.svg", out_html)

    def test_extracted_svg_without_matching_marker_png_is_ignored(self):
        html = "<html><body><img src='images/figure_page1.png' alt='p1'/></body></html>"
        page_svg_map = {9: "vector_page9.svg"}

        out_html, replaced = convert.replace_marker_png_with_extracted_svg(html, page_svg_map)

        self.assertEqual(replaced, 0)
        self.assertIn("figure_page1.png", out_html)
        self.assertNotIn("vector_page9.svg", out_html)

    def test_marker_png_without_extracted_svg_keeps_png(self):
        html = "<html><body><img src='images/figure_page5.png' alt='p5'/></body></html>"
        page_svg_map = {4: "vector_page4.svg"}

        out_html, replaced = convert.replace_marker_png_with_extracted_svg(html, page_svg_map)

        self.assertEqual(replaced, 0)
        self.assertIn("figure_page5.png", out_html)
        self.assertNotIn("vector_page4.svg", out_html)

    @mock.patch("convert.subprocess.run")
    @mock.patch("convert.os.makedirs")
    @mock.patch("convert.find_mutool", return_value=None)
    @mock.patch("convert.find_pdf2svg", return_value=None)
    def test_no_extracted_svg_tools_missing_keeps_all_png_without_crash(
        self,
        _pdf2svg_mock,
        _mutool_mock,
        makedirs_mock,
        subprocess_run_mock,
    ):
        page_svg_map = convert.extract_svg_assets_from_pdf(
            input_file="/fake/input.pdf",
            temp_dir="/fake/temp",
            preserve_svg="auto",
        )
        self.assertEqual(page_svg_map, {})
        makedirs_mock.assert_called_once()
        subprocess_run_mock.assert_not_called()

        html = (
            "<html><body>"
            "<img src='images/figure_page1.png' alt='p1'/>"
            "<img src='images/figure_page2.png' alt='p2'/>"
            "</body></html>"
        )
        out_html, replaced = convert.replace_marker_png_with_extracted_svg(html, page_svg_map)
        self.assertEqual(replaced, 0)
        self.assertIn("figure_page1.png", out_html)
        self.assertIn("figure_page2.png", out_html)


if __name__ == "__main__":
    unittest.main()
