import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert  # noqa: E402
import merge_and_build  # noqa: E402


class TablePipelineIntegrationTests(unittest.TestCase):
    def _run_pipeline(self, html_text):
        skeleton_html, head_inner, segments = convert.extract_segments_and_skeleton(html_text)

        translated = {}
        for sid, source_text in segments.items():
            translated[sid] = f"TR::{source_text.strip()}"

        reinjected = merge_and_build.reinject_placeholders(skeleton_html, translated)
        final_html = merge_and_build.build_full_html(reinjected, head_inner, "fr", "Test Tables")

        return skeleton_html, segments, translated, final_html

    def test_simple_table_extracts_cells_and_roundtrips(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body>"
            "<table>"
            "<thead><tr><th>Head 1</th><th>Head 2</th></tr></thead>"
            "<tbody>"
            "<tr><td>R1C1</td><td>R1C2</td></tr>"
            "<tr><td>R2C1</td><td>R2C2</td></tr>"
            "</tbody>"
            "</table>"
            "</body></html>"
        )

        skeleton_html, segments, translated, final_html = self._run_pipeline(html)
        self.assertEqual(
            list(segments.values()),
            ["Head 1", "Head 2", "R1C1", "R1C2", "R2C1", "R2C2"],
        )

        skeleton_soup = BeautifulSoup(skeleton_html, "html.parser")
        table = skeleton_soup.find("table")
        self.assertIsNotNone(table)
        self.assertIsNotNone(table.find("thead"))
        self.assertIsNotNone(table.find("tbody"))

        cells = table.find_all(["th", "td"])
        self.assertEqual(
            [cell.get_text(strip=True) for cell in cells],
            [f"{{{{{sid}}}}}" for sid in segments.keys()],
        )
        self.assertIn("{{T0001}}", skeleton_html)
        self.assertIn("{{T0006}}", skeleton_html)

        final_soup = BeautifulSoup(final_html, "html.parser")
        final_table = final_soup.find("table")
        self.assertIsNotNone(final_table)
        self.assertEqual(len(final_table.find_all("tr")), 3)
        self.assertEqual(len(final_table.find_all("th")), 2)
        self.assertEqual(len(final_table.find_all("td")), 4)

        final_cell_texts = [cell.get_text(strip=True) for cell in final_table.find_all(["th", "td"])]
        self.assertEqual(final_cell_texts, [translated[sid] for sid in segments.keys()])

    def test_table_with_merged_cells_preserves_colspan_and_rowspan(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body>"
            "<table>"
            "<tr><td colspan='2'>Wide</td><td rowspan='3'>Tall</td></tr>"
            "<tr><td>A</td><td>B</td></tr>"
            "<tr><td>C</td><td>D</td></tr>"
            "</table>"
            "</body></html>"
        )

        skeleton_html, segments, translated, final_html = self._run_pipeline(html)
        self.assertEqual(list(segments.values()), ["Wide", "Tall", "A", "B", "C", "D"])

        skeleton_soup = BeautifulSoup(skeleton_html, "html.parser")
        wide_cell = skeleton_soup.find("td", attrs={"colspan": "2"})
        tall_cell = skeleton_soup.find("td", attrs={"rowspan": "3"})
        self.assertIsNotNone(wide_cell)
        self.assertIsNotNone(tall_cell)
        self.assertIn("{{T0001}}", wide_cell.get_text())
        self.assertIn("{{T0002}}", tall_cell.get_text())

        final_soup = BeautifulSoup(final_html, "html.parser")
        final_wide = final_soup.find("td", attrs={"colspan": "2"})
        final_tall = final_soup.find("td", attrs={"rowspan": "3"})
        self.assertIsNotNone(final_wide)
        self.assertIsNotNone(final_tall)
        self.assertEqual(final_wide.get("colspan"), "2")
        self.assertEqual(final_tall.get("rowspan"), "3")
        self.assertEqual(final_wide.get_text(strip=True), translated["T0001"])
        self.assertEqual(final_tall.get_text(strip=True), translated["T0002"])

    def test_table_cell_with_mixed_content_preserves_nested_structure(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body>"
            "<table><tr><td><p>Para</p><ul><li>Item A</li><li>Item B</li></ul></td></tr></table>"
            "</body></html>"
        )

        skeleton_html, segments, translated, final_html = self._run_pipeline(html)
        self.assertEqual(list(segments.values()), ["Para", "Item A", "Item B"])

        skeleton_soup = BeautifulSoup(skeleton_html, "html.parser")
        cell = skeleton_soup.find("td")
        self.assertIsNotNone(cell)
        self.assertIsNotNone(cell.find("p"))
        self.assertIsNotNone(cell.find("ul"))
        self.assertEqual(len(cell.find_all("li")), 2)
        self.assertIn("{{T0001}}", str(cell.find("p")))
        self.assertIn("{{T0002}}", str(cell.find_all("li")[0]))
        self.assertIn("{{T0003}}", str(cell.find_all("li")[1]))

        final_soup = BeautifulSoup(final_html, "html.parser")
        final_cell = final_soup.find("td")
        self.assertIsNotNone(final_cell)
        self.assertIsNotNone(final_cell.find("p"))
        self.assertIsNotNone(final_cell.find("ul"))
        self.assertEqual(len(final_cell.find_all("li")), 2)
        self.assertEqual(final_cell.find("p").get_text(strip=True), translated["T0001"])
        self.assertEqual(final_cell.find_all("li")[0].get_text(strip=True), translated["T0002"])
        self.assertEqual(final_cell.find_all("li")[1].get_text(strip=True), translated["T0003"])

    def test_table_with_empty_cells_does_not_create_empty_segments(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body>"
            "<table><tr><td></td><td>   </td><td>Filled</td></tr></table>"
            "</body></html>"
        )

        skeleton_html, segments, translated, final_html = self._run_pipeline(html)
        self.assertEqual(list(segments.values()), ["Filled"])
        self.assertEqual(list(segments.keys()), ["T0001"])

        skeleton_soup = BeautifulSoup(skeleton_html, "html.parser")
        cells = skeleton_soup.find_all("td")
        self.assertEqual(len(cells), 3)
        self.assertEqual(cells[0].get_text(strip=True), "")
        self.assertEqual(cells[1].get_text(strip=True), "")
        self.assertEqual(cells[2].get_text(strip=True), "{{T0001}}")

        final_soup = BeautifulSoup(final_html, "html.parser")
        final_cells = final_soup.find_all("td")
        self.assertEqual(len(final_cells), 3)
        self.assertEqual(final_cells[0].get_text(strip=True), "")
        self.assertEqual(final_cells[1].get_text(strip=True), "")
        self.assertEqual(final_cells[2].get_text(strip=True), translated["T0001"])

    def test_table_preserves_style_related_attributes(self):
        html = (
            "<!DOCTYPE html><html><head><title>T</title></head><body>"
            "<table style='border-collapse:collapse'><colgroup><col width='30%'/><col width='70%'/></colgroup>"
            "<tr><td align='right'>Amount</td><td style='font-weight:bold'>42</td></tr></table>"
            "</body></html>"
        )

        skeleton_html, segments, translated, final_html = self._run_pipeline(html)
        self.assertEqual(list(segments.values()), ["Amount", "42"])

        skeleton_soup = BeautifulSoup(skeleton_html, "html.parser")
        table = skeleton_soup.find("table")
        self.assertIsNotNone(table)
        self.assertEqual(table.get("style"), "border-collapse:collapse")
        cols = skeleton_soup.find_all("col")
        self.assertEqual([col.get("width") for col in cols], ["30%", "70%"])
        right_td = skeleton_soup.find("td", attrs={"align": "right"})
        self.assertIsNotNone(right_td)
        bold_td = skeleton_soup.find("td", attrs={"style": "font-weight:bold"})
        self.assertIsNotNone(bold_td)

        final_soup = BeautifulSoup(final_html, "html.parser")
        final_table = final_soup.find("table")
        self.assertIsNotNone(final_table)
        self.assertEqual(final_table.get("style"), "border-collapse:collapse")
        final_cols = final_soup.find_all("col")
        self.assertEqual([col.get("width") for col in final_cols], ["30%", "70%"])
        final_right_td = final_soup.find("td", attrs={"align": "right"})
        final_bold_td = final_soup.find("td", attrs={"style": "font-weight:bold"})
        self.assertIsNotNone(final_right_td)
        self.assertIsNotNone(final_bold_td)
        self.assertEqual(final_right_td.get_text(strip=True), translated["T0001"])
        self.assertEqual(final_bold_td.get_text(strip=True), translated["T0002"])


if __name__ == "__main__":
    unittest.main()
