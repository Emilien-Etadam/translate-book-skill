import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402
import merge_and_build  # noqa: E402


class FootnoteDetectionTests(unittest.TestCase):
    def test_detects_call_to_body_pair(self):
        html = (
            "<html><body>"
            "<p>Texte <sup><a id='ref1' href='#fn1'>1</a></sup></p>"
            "<ol><li id='fn1'>Texte de note</li></ol>"
            "</body></html>"
        )
        links = convert.detect_footnote_links(html)
        self.assertEqual(links, {"ref1": "fn1"})


class FootnoteChunkingTests(unittest.TestCase):
    def test_linked_note_is_forced_into_same_chunk(self):
        html = (
            "<html><body>"
            "<p>Intro longue phrase.</p>"
            "<p>Appel <sup><a id='ref1' href='#fn1'>1</a></sup> dans le corps.</p>"
            "<p>Beaucoup de texte séparateur pour forcer des chunks.</p>"
            "<p>Encore du texte séparateur.</p>"
            "<ol><li id='fn1'>Texte de note important.</li></ol>"
            "</body></html>"
        )
        _skeleton, _head, segments = convert.extract_segments_and_skeleton(html)

        note_sid = None
        call_sid = None
        for sid, value in segments.items():
            if isinstance(value, dict) and value.get("footnote_for"):
                note_sid = sid
                call_sid = value["footnote_for"]
                break

        self.assertIsNotNone(note_sid)
        self.assertIsNotNone(call_sid)

        with tempfile.TemporaryDirectory() as d:
            chunk_files = convert.build_translation_chunks(segments, d, chunk_size=20)
            self.assertTrue(chunk_files)

            call_chunk = None
            note_chunk = None
            for chunk_name in chunk_files:
                content = Path(d, chunk_name).read_text(encoding="utf-8")
                if f"{call_sid}:" in content:
                    call_chunk = chunk_name
                if f"{note_sid}:" in content:
                    note_chunk = chunk_name
                if f"{note_sid}:" in content:
                    self.assertIn(f"# NOTE: {note_sid} is footnote for {call_sid}", content)

            self.assertEqual(call_chunk, note_chunk)

    def test_no_footnote_keeps_legacy_segment_shape(self):
        html = "<html><body><p>Alpha</p><p>Beta</p></body></html>"
        _skeleton, _head, segments = convert.extract_segments_and_skeleton(html)
        self.assertTrue(all(isinstance(v, str) for v in segments.values()))


class MergeMixedSegmentsTests(unittest.TestCase):
    def test_load_segments_json_accepts_mixed_value_formats(self):
        with tempfile.TemporaryDirectory() as d:
            data = {
                "T0001": "Texte normal",
                "T0002": {"text": "Texte de note", "footnote_for": "T0001"},
            }
            Path(d, "segments.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            loaded = merge_and_build.load_segments_json(d)
            self.assertEqual(loaded["T0001"], "Texte normal")
            self.assertEqual(loaded["T0002"], "Texte de note")


if __name__ == "__main__":
    unittest.main()
