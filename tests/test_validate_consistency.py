import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_consistency.py"


class ValidateConsistencyScriptTests(unittest.TestCase):
    def run_validator(self, temp_dir, olang="fr"):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--temp-dir",
                str(temp_dir),
                "--olang",
                olang,
            ],
            capture_output=True,
            text=True,
        )

    def write_segments(self, temp_dir, mapping):
        Path(temp_dir, "segments.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_output_chunk(self, temp_dir, chunk_no, lines):
        Path(temp_dir, f"output_chunk{chunk_no:04d}.txt").write_text(
            "".join(lines),
            encoding="utf-8",
        )

    def write_glossary(self, temp_dir, mapping):
        Path(temp_dir, "glossary.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_dedup_map(self, temp_dir, mapping):
        Path(temp_dir, "dedup_map.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_report(self, temp_dir):
        return Path(temp_dir, "consistency_report.txt").read_text(encoding="utf-8")

    def read_segments_translated(self, temp_dir):
        return json.loads(
            Path(temp_dir, "segments_translated.json").read_text(encoding="utf-8")
        )

    def test_no_issues_writes_exact_no_issues_found(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(d, {"T0001": "Hello world"})
            self.write_output_chunk(d, 1, ["T0001: Bonjour le monde\n"])

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(self.read_report(d), "No issues found.\n")

    def test_detects_glossary_violation(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(d, {"T0001": "I study machine learning every day."})
            self.write_output_chunk(d, 1, ["T0001: J'etudie l'apprentissage machine chaque jour.\n"])
            self.write_glossary(d, {"machine learning": "apprentissage automatique"})

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            report = self.read_report(d)
            self.assertIn("=== GLOSSARY VIOLATIONS ===", report)
            self.assertIn(
                '"machine learning" should be "apprentissage automatique"',
                report,
            )
            self.assertIn("T0001:", report)

    def test_detects_untranslated_segment(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(d, {"T0001": "Stay curious."})
            self.write_output_chunk(d, 1, ["T0001: Stay curious.\n"])

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            report = self.read_report(d)
            self.assertIn("=== UNTRANSLATED SEGMENTS ===", report)
            self.assertIn("T0001: source and translation are identical", report)

    def test_detects_empty_or_whitespace_translation(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(
                d,
                {
                    "T0001": "One",
                    "T0002": "Two",
                },
            )
            self.write_output_chunk(
                d,
                1,
                [
                    "T0001:    \n",
                    "T0002: \t \n",
                ],
            )

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            report = self.read_report(d)
            self.assertIn("=== EMPTY TRANSLATIONS ===", report)
            self.assertIn("T0001: empty or whitespace-only", report)
            self.assertIn("T0002: empty or whitespace-only", report)

    def test_without_glossary_section_absent_and_other_checks_work(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(
                d,
                {
                    "T0001": "Untranslated text",
                    "T0002": "This should not be empty",
                },
            )
            self.write_output_chunk(
                d,
                1,
                [
                    "T0001: Untranslated text\n",
                    "T0002:    \n",
                ],
            )
            # Intentionally no glossary.json file

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            report = self.read_report(d)
            self.assertNotIn("=== GLOSSARY VIOLATIONS ===", report)
            self.assertIn("=== UNTRANSLATED SEGMENTS ===", report)
            self.assertIn("T0001: source and translation are identical", report)
            self.assertIn("=== EMPTY TRANSLATIONS ===", report)
            self.assertIn("T0002: empty or whitespace-only", report)

    def test_writes_segments_translated_json_with_unescaped_payload(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(
                d,
                {
                    "T0001": "source a",
                    "T0002": "source b",
                },
            )
            self.write_output_chunk(
                d,
                1,
                [
                    r"T0001: Ligne1\nLigne2\\Slash\ret-fin" + "\n",
                    r"T0002: Valeur simple" + "\n",
                ],
            )

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            translated = self.read_segments_translated(d)
            self.assertEqual(translated["T0001"], "Ligne1\nLigne2\\Slash\ret-fin")
            self.assertEqual(translated["T0002"], "Valeur simple")

    def test_dedup_alias_is_expanded_and_not_reported_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_segments(
                d,
                {
                    "T0001": "Repeated legal text",
                    "T0002": "Repeated legal text",
                },
            )
            self.write_dedup_map(
                d,
                {
                    "T0001": "T0001",
                    "T0002": "T0001",
                },
            )
            self.write_output_chunk(
                d,
                1,
                [
                    "T0001: Texte legal repete\n",
                ],
            )

            result = self.run_validator(d)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(self.read_report(d), "No issues found.\n")
            translated = self.read_segments_translated(d)
            self.assertEqual(translated["T0002"], "Texte legal repete")


if __name__ == "__main__":
    unittest.main()
