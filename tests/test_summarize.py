import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import summarize  # noqa: E402


class SamplingTests(unittest.TestCase):
    def test_uniform_sampling_indices_on_10_chunks(self):
        indices = summarize.select_uniform_indices(10, 5)
        self.assertEqual(indices, [0, 2, 4, 6, 9])


class ChunkExtractionTests(unittest.TestCase):
    def test_extracts_only_t_lines_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "chunk1.txt"
            p.write_text(
                "# commentaire global\n"
                "T0001: Bonjour\n"
                " # commentaire indenté\n"
                "noise line\n"
                "T0002: Monde\n",
                encoding="utf-8",
            )
            segments = summarize.parse_chunk_file(str(p))
            self.assertEqual(segments, [("T0001", "Bonjour"), ("T0002", "Monde")])


class LanguageDetectionTests(unittest.TestCase):
    def test_detect_english(self):
        text = '{"T0001":"the story is in the city and the people are together"}'
        self.assertEqual(summarize.detect_source_language(text[:500]), "en")

    def test_detect_french(self):
        text = '{"T0001":"le roman est dans la ville et les personnages sont ici"}'
        self.assertEqual(summarize.detect_source_language(text[:500]), "fr")

    def test_detect_chinese(self):
        text = '{"T0001":"这是一本小说，讲述一个家庭在城市中的生活与变化。"}'
        self.assertEqual(summarize.detect_source_language(text[:500]), "zh")


class PromptGenerationTests(unittest.TestCase):
    def test_summary_prompt_contains_excerpts_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            temp_dir = Path(d)
            (temp_dir / "segments.json").write_text(
                json.dumps({"T0001": "x"}, ensure_ascii=False),
                encoding="utf-8",
            )

            for i in range(1, 5):
                (temp_dir / f"chunk{i}.txt").write_text(
                    f"T{i:04d}: extrait_{i}\n",
                    encoding="utf-8",
                )

            rc = summarize.run(str(temp_dir), "fr", 4)
            self.assertEqual(rc, 0)

            prompt = (temp_dir / "summary_prompt.txt").read_text(encoding="utf-8")
            p1 = prompt.find("extrait_1")
            p2 = prompt.find("extrait_2")
            p3 = prompt.find("extrait_3")
            p4 = prompt.find("extrait_4")
            self.assertTrue(0 <= p1 < p2 < p3 < p4)
            self.assertIn("--- EXTRAIT 1 (début du livre) ---", prompt)
            self.assertIn("--- EXTRAIT 4 (fin du livre) ---", prompt)

    def test_fewshot_uses_three_longest_segments_from_sampled_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            temp_dir = Path(d)
            (temp_dir / "segments.json").write_text(
                json.dumps({"T0001": "the and of"}, ensure_ascii=False),
                encoding="utf-8",
            )

            (temp_dir / "chunk1.txt").write_text(
                "T0001: court\n"
                "T0002: segment nettement plus long que court\n"
                "T0003: longueur moyenne ici\n",
                encoding="utf-8",
            )
            (temp_dir / "chunk2.txt").write_text(
                "T0004: ceci est probablement le segment le plus long de tous les exemples\n"
                "T0005: mini\n",
                encoding="utf-8",
            )
            (temp_dir / "chunk3.txt").write_text(
                "T0006: encore un segment assez long pour entrer dans le top\n",
                encoding="utf-8",
            )

            rc = summarize.run(str(temp_dir), "fr", 3)
            self.assertEqual(rc, 0)

            fewshot = (temp_dir / "fewshot_prompt.txt").read_text(encoding="utf-8")
            self.assertIn("segment le plus long de tous les exemples", fewshot)
            self.assertIn("encore un segment assez long pour entrer dans le top", fewshot)
            self.assertIn("segment nettement plus long que court", fewshot)
            self.assertNotIn("Segment 1: court", fewshot)
            self.assertNotIn("Segment 1: mini", fewshot)


if __name__ == "__main__":
    unittest.main()
