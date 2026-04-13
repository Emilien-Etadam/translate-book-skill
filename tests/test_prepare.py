import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import prepare  # noqa: E402


class PreparePipelineTests(unittest.TestCase):
    def setUp(self):
        self.work_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _run_prepare(self, html_content: str, extra_args=None):
        extra_args = extra_args or []
        src = self.work_dir / "book.html"
        src.write_text(html_content, encoding="utf-8")
        rc = prepare.main(
            [
                str(src),
                "--olang",
                "zh",
                "--chunk-size",
                "200",
                *extra_args,
            ]
        )
        self.assertEqual(rc, 0)
        temp_dir = self.work_dir / "book_temp"
        state = json.loads((temp_dir / "pipeline_state.json").read_text(encoding="utf-8"))
        return temp_dir, state

    def test_full_run_writes_pipeline_state_for_minimal_html(self):
        temp_dir, state = self._run_prepare(
            "<html><head><title>X</title></head><body><p>Hello world.</p></body></html>"
        )
        self.assertEqual(state["input_file"], "book.html")
        self.assertEqual(state["target_lang"], "zh")
        self.assertEqual(state["total_segments"], 1)
        self.assertEqual(state["total_chunks"], 1)
        self.assertEqual(state["style"], "auto")
        self.assertTrue(state["style_detection_needed"])
        self.assertEqual(state["conversion_method"], "html_segments")
        self.assertTrue((temp_dir / "manifest.json").is_file())
        self.assertTrue((temp_dir / "chunk0001.txt").is_file())

    def test_dedup_produces_map_and_chunks_only_canonical(self):
        temp_dir, state = self._run_prepare(
            "<html><body><p>Repeated sentence.</p><p>Repeated sentence.</p><p>Unique line.</p></body></html>"
        )
        dedup_map = json.loads((temp_dir / "dedup_map.json").read_text(encoding="utf-8"))
        self.assertEqual(dedup_map["T0001"], "T0001")
        self.assertEqual(dedup_map["T0002"], "T0001")
        self.assertEqual(state["dedup_segments_skipped"], 1)
        chunk_text = (temp_dir / "chunk0001.txt").read_text(encoding="utf-8")
        self.assertIn("T0001: Repeated sentence.", chunk_text)
        self.assertNotIn("T0002:", chunk_text)
        self.assertIn("T0003: Unique line.", chunk_text)

    def test_same_note_text_different_context_not_deduped(self):
        html = (
            "<html><body>"
            "<p>Alpha<sup><a id='r1' href='#fn1'>1</a></sup></p>"
            "<p>Beta<sup><a id='r2' href='#fn2'>2</a></sup></p>"
            "<div id='fn1' class='footnote'>Same note text</div>"
            "<div id='fn2' class='footnote'>Same note text</div>"
            "</body></html>"
        )
        temp_dir, state = self._run_prepare(html)
        segments = json.loads((temp_dir / "segments.json").read_text(encoding="utf-8"))
        dedup_map_path = temp_dir / "dedup_map.json"
        dedup_map = {}
        if dedup_map_path.is_file():
            dedup_map = json.loads(dedup_map_path.read_text(encoding="utf-8"))

        note_ids = [
            sid
            for sid, value in segments.items()
            if isinstance(value, dict) and value.get("text") == "Same note text"
        ]
        self.assertEqual(len(note_ids), 2)
        if dedup_map:
            self.assertEqual(dedup_map[note_ids[0]], note_ids[0])
            self.assertEqual(dedup_map[note_ids[1]], note_ids[1])
        self.assertEqual(state["dedup_segments_skipped"], 0)

    def test_glossary_needed_false_when_no_candidates(self):
        _temp_dir, state = self._run_prepare(
            "<html><body><p>one two</p><p>three four</p></body></html>"
        )
        self.assertEqual(state["glossary_candidates_count"], 0)
        self.assertFalse(state["glossary_needed"])

    def test_style_detection_needed_false_with_explicit_style(self):
        _temp_dir, state = self._run_prepare(
            "<html><body><p>Hello world.</p></body></html>",
            extra_args=["--style", "formal"],
        )
        self.assertEqual(state["style"], "formal")
        self.assertFalse(state["style_detection_needed"])


if __name__ == "__main__":
    unittest.main()
