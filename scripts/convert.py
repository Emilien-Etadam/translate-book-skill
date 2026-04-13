#!/usr/bin/env python3
"""
convert.py — PDF/DOCX/EPUB → squelette HTML + segments + chunks texte (via Calibre HTMLZ)

Architecture structure / contenu :
- Calibre produit un HTMLZ ; le HTML principal est extrait depuis le ZIP.
- Le corps du document est parcouru (BeautifulSoup) : chaque nœud texte traduisible devient
  un segment ``T0001``, … remplacé dans le DOM par ``{{T0001}}``, …
- Le LLM ne reçoit que des fichiers ``chunkNNNN.txt`` au format ``T0001: texte`` (une ligne
  logique par segment ; les retours ligne dans le texte sont échappés ``\\n``). Des commentaires
  ``# NOTE: ...`` peuvent apparaître en tête de chunk pour relier appel/corps de note.

Impact aval (à implémenter dans merge_and_build.py et dans les instructions subagents) :
- Les subagents reçoivent ``chunk*.txt`` : traduire chaque ligne ``Txxxx: …`` en conservant
  l’identifiant et le même format ; écrire dans ``output_chunk*.txt``.
- merge_and_build.py devra : parser les chunks traduits, reconstruire ``segments_translated.json``,
  réinjecter dans ``skeleton.html`` en remplaçant chaque ``{{Txxxx}}`` par le texte traduit,
  réassembler éventuellement ``<head>`` (cf. ``head.html``) si besoin, puis passer le HTML
  complet à Calibre pour DOCX/EPUB/PDF.

Dépendances tierces : beautifulsoup4 uniquement. Calibre ``ebook-convert`` est invoqué en
subprocess (outil externe, pas module Python).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString

from manifest import create_manifest

# Balises dont le contenu texte n’est pas traduit (ni extrait comme segment).
_SKIP_ANCESTOR_TAGS = frozenset({"script", "style", "code", "pre", "svg"})

# Attributs susceptibles de pointer vers des ressources copiées sous ``assets/``.
_URL_ATTRS = ("src", "href", "poster", "xlink:href")
_FOOTNOTE_REF_CLASS_KEYWORDS = ("footnote-ref", "noteref", "fn-ref")
_FOOTNOTE_BODY_CLASS_KEYWORDS = ("footnote", "fn", "endnote")
SegmentValue = Union[str, Dict[str, str]]


def find_calibre_convert() -> Optional[str]:
    """Localise la commande ``ebook-convert`` (Calibre)."""
    possible_paths = [
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        "/usr/bin/ebook-convert",
        "/usr/local/bin/ebook-convert",
        "ebook-convert",
    ]
    for path in possible_paths:
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                print(f"Found Calibre ebook-convert: {path}")
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def find_marker_single() -> Optional[str]:
    """Localise la commande ``marker_single`` (marker-pdf)."""
    candidates = ["marker_single", "marker-single"]
    for cmd in candidates:
        path = shutil.which(cmd)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode in (0, 1):
                return path
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None


def find_pdf2svg() -> Optional[str]:
    """Localise la commande ``pdf2svg``."""
    return shutil.which("pdf2svg")


def find_mutool() -> Optional[str]:
    """Localise la commande ``mutool``."""
    return shutil.which("mutool")


@dataclass
class PdfHeuristicResult:
    classification: str
    indicators: List[str]
    warnings: List[str]
    pages: int = 0
    page_width_pts: float = 0.0


def _parse_pdfinfo_output(pdfinfo_text: str) -> Tuple[int, float]:
    pages = 0
    page_width_pts = 0.0
    for line in pdfinfo_text.splitlines():
        if line.startswith("Pages:"):
            value = line.split(":", 1)[1].strip()
            m = re.match(r"(\d+)", value)
            if m:
                pages = int(m.group(1))
        elif line.startswith("Page size:"):
            value = line.split(":", 1)[1].strip()
            m = re.search(r"([0-9.]+)\s+x\s+([0-9.]+)\s+pts", value)
            if m:
                page_width_pts = float(m.group(1))
    return pages, page_width_pts


def _normalize_header_footer_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def detect_pdf_structure(input_file: str) -> PdfHeuristicResult:
    """Classifie un PDF en simple/complex via heuristiques Poppler."""
    warnings: List[str] = []
    indicators: List[str] = []

    pdfinfo_cmd = shutil.which("pdfinfo")
    pdftotext_cmd = shutil.which("pdftotext")
    if not pdfinfo_cmd or not pdftotext_cmd:
        warnings.append(
            "Warning: pdfinfo/pdftotext indisponibles. "
            "Détection PDF désactivée, fallback sur simple."
        )
        return PdfHeuristicResult("simple", indicators, warnings)

    try:
        pdfinfo_res = subprocess.run(
            [pdfinfo_cmd, input_file],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if pdfinfo_res.returncode != 0:
            warnings.append(
                "Warning: pdfinfo a échoué. Détection PDF désactivée, fallback sur simple."
            )
            return PdfHeuristicResult("simple", indicators, warnings)
        pages, page_width_pts = _parse_pdfinfo_output(pdfinfo_res.stdout)
    except (subprocess.TimeoutExpired, OSError) as e:
        warnings.append(
            f"Warning: impossible d'exécuter pdfinfo ({e}). "
            "Détection PDF désactivée, fallback sur simple."
        )
        return PdfHeuristicResult("simple", indicators, warnings)

    try:
        pdftotext_res = subprocess.run(
            [
                pdftotext_cmd,
                "-layout",
                "-f",
                "1",
                "-l",
                "5",
                input_file,
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pdftotext_res.returncode != 0:
            warnings.append(
                "Warning: pdftotext -layout a échoué. Détection PDF désactivée, fallback sur simple."
            )
            return PdfHeuristicResult("simple", indicators, warnings, pages, page_width_pts)
        extracted_text = pdftotext_res.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        warnings.append(
            f"Warning: impossible d'exécuter pdftotext ({e}). "
            "Détection PDF désactivée, fallback sur simple."
        )
        return PdfHeuristicResult("simple", indicators, warnings, pages, page_width_pts)

    page_chunks = extracted_text.split("\f")
    pages_text: List[List[str]] = []
    for chunk in page_chunks:
        cleaned = [line.rstrip("\n") for line in chunk.splitlines()]
        if any(line.strip() for line in cleaned):
            pages_text.append(cleaned)

    all_lines = [line for page in pages_text for line in page if line.strip()]
    if all_lines:
        # ~6pt/char donne un ordre de grandeur pour approximer 40% de largeur page.
        indent_threshold = max(8, int((page_width_pts * 0.40) / 6.0)) if page_width_pts else 32
        indented_lines = 0
        for line in all_lines:
            indent = len(line) - len(line.lstrip(" "))
            if indent >= indent_threshold:
                indented_lines += 1
        indent_ratio = indented_lines / len(all_lines)
        if indent_ratio > 0.30:
            indicators.append(
                f"high_indentation_ratio={indent_ratio:.2f} (>0.30), probable colonnes"
            )

    if pages_text:
        page_count = len(pages_text)
        top_counter: Counter[str] = Counter()
        bottom_counter: Counter[str] = Counter()
        footer_like_pages = 0

        for page in pages_text:
            non_empty = [ln for ln in page if ln.strip()]
            if not non_empty:
                continue
            top = _normalize_header_footer_line(non_empty[0])
            bottom = _normalize_header_footer_line(non_empty[-1])
            if top:
                top_counter[top] += 1
            if bottom:
                bottom_counter[bottom] += 1

            # Proxy "notes de bas de page": lignes courtes numérotées en bas de page.
            tail = non_empty[max(0, int(len(non_empty) * 0.75)) :]
            footnote_lines = 0
            for line in tail:
                stripped = line.strip()
                if len(stripped) > 55:
                    continue
                if re.match(
                    r"^(\[?\d+\]?[\)\.\:]?|[¹²³⁴⁵⁶⁷⁸⁹][\)\.\:]?)\s+\S+",
                    stripped,
                ):
                    footnote_lines += 1
            if footnote_lines >= 2:
                footer_like_pages += 1

        repeated_threshold = max(2, int(page_count * 0.4))
        repeated_header = top_counter and max(top_counter.values()) >= repeated_threshold
        repeated_footer = bottom_counter and max(bottom_counter.values()) >= repeated_threshold
        if repeated_header or repeated_footer:
            indicators.append("repeated_headers_or_footers_detected")

        footnote_threshold = max(1, int(page_count * 0.3))
        if footer_like_pages >= footnote_threshold:
            indicators.append("recurrent_footnote_patterns_detected")

    classification = "complex" if indicators else "simple"
    return PdfHeuristicResult(classification, indicators, warnings, pages, page_width_pts)


def choose_pdf_engine(input_file: str, pdf_engine: str) -> Tuple[str, Optional[str], str]:
    """Choisit le moteur PDF final: calibre ou marker."""
    if pdf_engine == "calibre":
        return "calibre", None, "forced"

    marker_cmd = find_marker_single()
    if pdf_engine == "marker":
        if not marker_cmd:
            raise RuntimeError(
                "Error: --pdf-engine marker demandé mais marker_single est introuvable dans PATH."
            )
        return "marker", marker_cmd, "forced"

    result = detect_pdf_structure(input_file)
    for w in result.warnings:
        print(w)
    print(
        f"PDF heuristic classification: {result.classification} "
        f"(pages={result.pages or 'unknown'}, width={result.page_width_pts or 'unknown'} pts)"
    )
    for indicator in result.indicators:
        print(f"  indicator: {indicator}")

    if result.classification == "complex":
        if marker_cmd:
            print("Complex PDF detected — routing extraction to marker_single.")
            return "marker", marker_cmd, "complex"
        print(
            "Warning: PDF complexe détecté mais marker n'est pas installé. "
            "La qualité d'extraction sera dégradée. Installer marker-pdf pour de meilleurs résultats."
        )
        return "calibre", None, "complex"
    return "calibre", None, "simple"


def convert_to_htmlz(input_file: str, htmlz_file: str, calibre_path: str) -> bool:
    """Convertit l’entrée en HTMLZ via Calibre."""
    try:
        print(f"Converting {input_file} to HTMLZ...")
        cmd = [calibre_path, input_file, htmlz_file]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            size = os.path.getsize(htmlz_file)
            print(f"HTMLZ conversion successful: {htmlz_file} ({size} bytes)")
            return True
        print(f"HTMLZ conversion failed: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("HTMLZ conversion timed out")
        return False
    except Exception as e:
        print(f"HTMLZ conversion error: {e}")
        return False


def run_marker_single(input_file: str, output_dir: str, marker_cmd: str) -> bool:
    """Exécute marker-pdf en mode single-file."""
    try:
        print(f"Extracting structured Markdown via marker: {input_file}")
        cmd = [marker_cmd, input_file, "--output_dir", output_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return True
        print(f"marker_single failed: {result.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("marker_single timed out")
        return False
    except Exception as e:
        print(f"marker_single error: {e}")
        return False


def _parse_page_num_from_text(text: str) -> Optional[int]:
    """
    Extrait un numéro de page probable depuis un nom de fichier/URL.
    Heuristique: recherche des tokens ``page``/``p`` + chiffres, sinon dernier nombre.
    """
    base = os.path.basename(text or "")
    if not base:
        return None
    stem = os.path.splitext(base)[0].lower()

    m = re.search(r"(?:^|[_\-.])(page|p)[_\-.]?0*(\d+)(?:$|[_\-.])", stem)
    if m:
        return int(m.group(2))

    nums = re.findall(r"\d+", stem)
    if not nums:
        return None
    try:
        return int(nums[-1])
    except ValueError:
        return None


def _extract_pdf_pages_to_svg_with_pdf2svg(
    input_file: str,
    assets_root: str,
    pdf2svg_cmd: str,
) -> Tuple[bool, Dict[int, str]]:
    output_pattern = os.path.join(assets_root, "vector_page%d.svg")
    cmd = [pdf2svg_cmd, input_file, output_pattern, "all"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()
        if tail:
            print(f"Warning: pdf2svg failed: {tail[-1000:]}")
        else:
            print("Warning: pdf2svg failed.")
        return False, {}

    page_map: Dict[int, str] = {}
    for name in os.listdir(assets_root):
        if not name.lower().endswith(".svg"):
            continue
        if not name.lower().startswith("vector_page"):
            continue
        p = _parse_page_num_from_text(name)
        if p is None:
            continue
        page_map[p] = name.replace("\\", "/")
    if not page_map:
        print("Warning: pdf2svg succeeded but produced no vector_page*.svg files.")
        return False, {}
    return True, page_map


def _extract_svg_assets_with_mutool(
    input_file: str,
    assets_root: str,
    mutool_cmd: str,
) -> bool:
    """
    Fallback best-effort via ``mutool extract``.
    Cette extraction ne garantit pas une correspondance page->figure.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        cmd = [mutool_cmd, "extract", input_file]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1200,
            cwd=tmp_dir,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()
            if tail:
                print(f"Warning: mutool extract failed: {tail[-1000:]}")
            else:
                print("Warning: mutool extract failed.")
            return False

        copied = 0
        for root, _dirs, files in os.walk(tmp_dir):
            for name in files:
                if not name.lower().endswith(".svg"):
                    continue
                src = os.path.join(root, name)
                dst_name = f"mutool_{name}"
                dest = os.path.join(assets_root, dst_name)
                if os.path.exists(dest):
                    base, ext = os.path.splitext(dst_name)
                    i = 2
                    while os.path.exists(os.path.join(assets_root, f"{base}_{i}{ext}")):
                        i += 1
                    dest = os.path.join(assets_root, f"{base}_{i}{ext}")
                shutil.copy2(src, dest)
                copied += 1

        if copied == 0:
            print(
                "Warning: mutool extract completed but no SVG assets were found. "
                "Keeping Marker PNG figures."
            )
            return False
        print(
            "Warning: SVG extracted with mutool extract (no page mapping); "
            "Marker PNG figures are kept to avoid wrong substitutions."
        )
        return True


def extract_svg_assets_from_pdf(
    input_file: str,
    temp_dir: str,
    preserve_svg: str,
) -> Dict[int, str]:
    """
    Extrait des SVG depuis le PDF pour préservation vectorielle.
    Retourne un mapping ``page -> relative svg path`` si disponible.
    """
    if preserve_svg == "never":
        return {}

    pdf2svg_cmd = find_pdf2svg()
    mutool_cmd = find_mutool()
    assets_root = os.path.join(temp_dir, "assets")
    os.makedirs(assets_root, exist_ok=True)

    if not pdf2svg_cmd and not mutool_cmd:
        msg = (
            "SVG preservation requested but no extractor found. "
            "Install pdf2svg or mutool."
        )
        if preserve_svg == "always":
            raise RuntimeError(f"Error: {msg}")
        print(f"Warning: {msg} Falling back to Marker PNG assets.")
        return {}

    if pdf2svg_cmd:
        ok, page_map = _extract_pdf_pages_to_svg_with_pdf2svg(input_file, assets_root, pdf2svg_cmd)
        if ok:
            print(f"SVG page extraction enabled via pdf2svg ({len(page_map)} page SVG files).")
            return page_map
        if preserve_svg == "always":
            raise RuntimeError("Error: preserve-svg=always but pdf2svg extraction failed.")
        print("Warning: pdf2svg extraction failed, trying mutool extract.")

    if mutool_cmd:
        ok = _extract_svg_assets_with_mutool(input_file, assets_root, mutool_cmd)
        if ok:
            return {}
        if preserve_svg == "always":
            raise RuntimeError("Error: preserve-svg=always but mutool extract failed.")
    return {}


def replace_marker_png_with_extracted_svg(
    html_text: str,
    page_svg_map: Dict[int, str],
) -> Tuple[str, int]:
    """
    Remplace ``<img src=...png>`` issus de Marker par des SVG extraits si mapping non ambigu.
    Règle: une seule image Marker candidate par page -> substitution autorisée.
    """
    if not page_svg_map:
        return html_text, 0

    soup = BeautifulSoup(html_text, "html.parser")
    candidates = []
    page_counts: Dict[int, int] = {}
    for img in soup.find_all("img"):
        src = img.get("src")
        if not isinstance(src, str):
            continue
        low = src.lower()
        if not low.endswith(".png"):
            continue
        page_num = _parse_page_num_from_text(src)
        if page_num is None:
            continue
        candidates.append((img, page_num))
        page_counts[page_num] = page_counts.get(page_num, 0) + 1

    replaced = 0
    for img, page_num in candidates:
        svg_rel = page_svg_map.get(page_num)
        if not svg_rel:
            continue
        if page_counts.get(page_num, 0) != 1:
            # Ambigu: plusieurs figures sur la même page -> conserver PNG.
            continue
        img["src"] = svg_rel
        replaced += 1

    return str(soup), replaced


def _find_marker_markdown(marker_output_dir: str) -> Optional[str]:
    candidates: List[Tuple[int, str]] = []
    for root, _dirs, files in os.walk(marker_output_dir):
        for name in files:
            if name.lower().endswith((".md", ".markdown")):
                path = os.path.join(root, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                candidates.append((size, path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _render_inline_markdown(text: str) -> str:
    rendered = html.escape(text, quote=False)

    rendered = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: (
            f'<img alt="{html.escape(m.group(1), quote=True)}" '
            f'src="{html.escape(m.group(2), quote=True)}"/>'
        ),
        rendered,
    )
    rendered = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: (
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{m.group(1)}</a>"
        ),
        rendered,
    )
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    return rendered


def markdown_to_html(md_text: str) -> str:
    """
    Convertisseur Markdown -> HTML volontairement minimal :
    headings, paragraphes, gras/italique, images, liens, listes, blocs de code.
    """
    lines = md_text.splitlines()
    out: List[str] = ["<html><head><meta charset=\"utf-8\"/></head><body>"]

    paragraph_lines: List[str] = []
    in_code = False
    code_lines: List[str] = []
    in_list = False

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        text = " ".join(x.strip() for x in paragraph_lines if x.strip())
        if text:
            out.append(f"<p>{_render_inline_markdown(text)}</p>")
        paragraph_lines = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        if line.strip().startswith("```"):
            flush_paragraph()
            close_list()
            if in_code:
                escaped = html.escape("\n".join(code_lines))
                out.append(f"<pre><code>{escaped}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            flush_paragraph()
            close_list()
            level = len(heading_match.group(1))
            content = _render_inline_markdown(heading_match.group(2).strip())
            out.append(f"<h{level}>{content}</h{level}>")
            continue

        bullet_match = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet_match:
            flush_paragraph()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_render_inline_markdown(bullet_match.group(1).strip())}</li>")
            continue

        if line.strip() == "":
            flush_paragraph()
            close_list()
            continue

        paragraph_lines.append(line)

    flush_paragraph()
    close_list()
    if in_code:
        escaped = html.escape("\n".join(code_lines))
        out.append(f"<pre><code>{escaped}</code></pre>")
    out.append("</body></html>")
    return "\n".join(out)


def extract_metadata_from_extract_dir(extract_dir: str) -> dict:
    """Lit les métadonnées depuis ``metadata.opf`` dans l’arborescence extraite."""
    metadata_file = None
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            if name.lower() == "metadata.opf":
                metadata_file = os.path.join(root, name)
                break
        if metadata_file:
            break
    if not metadata_file:
        return {}
    try:
        tree = ET.parse(metadata_file)
        root = tree.getroot()
        ns = {
            "opf": "http://www.idpf.org/2007/opf",
            "dc": "http://purl.org/dc/elements/1.1/",
        }
        meta = {}
        title_el = root.find(".//dc:title", ns)
        if title_el is not None and title_el.text:
            meta["title"] = title_el.text.strip()
        creator_el = root.find(".//dc:creator", ns)
        if creator_el is not None and creator_el.text:
            meta["creator"] = creator_el.text.strip()
        publisher_el = root.find(".//dc:publisher", ns)
        if publisher_el is not None and publisher_el.text:
            meta["publisher"] = publisher_el.text.strip()
        lang_el = root.find(".//dc:language", ns)
        if lang_el is not None and lang_el.text:
            meta["language"] = lang_el.text.strip()
        return meta
    except Exception as e:
        print(f"Warning: Error extracting metadata: {e}")
        return {}


def _find_main_html_in_extract(extract_dir: str) -> Optional[str]:
    """Retourne le chemin du fichier HTML principal (index ou premier .html)."""
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            if name.lower() in ("index.html", "index.htm"):
                return os.path.join(root, name)
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            if name.lower().endswith((".html", ".htm")):
                return os.path.join(root, name)
    return None


def _ancestor_names(element) -> List[str]:
    names: List[str] = []
    p = getattr(element, "parent", None)
    while p is not None:
        n = getattr(p, "name", None)
        if n:
            names.append(n.lower())
        p = getattr(p, "parent", None)
    return names


def _is_under_skip_tag(node) -> bool:
    return bool(_SKIP_ANCESTOR_TAGS.intersection(_ancestor_names(node)))


def _segment_id(n: int) -> str:
    return f"T{n:04d}"


def _escape_chunk_payload(text: str) -> str:
    """Échappe ``\\``, ``\\n``, ``\\r`` pour une ligne unique par segment dans les chunks."""
    return text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _class_tokens(tag) -> List[str]:
    classes = tag.get("class")
    if not classes:
        return []
    if isinstance(classes, str):
        return [part.strip().lower() for part in classes.split() if part.strip()]
    out: List[str] = []
    for item in classes:
        if isinstance(item, str):
            out.extend(part.strip().lower() for part in item.split() if part.strip())
    return out


def _class_matches_any(tag, keywords: Tuple[str, ...]) -> bool:
    tokens = _class_tokens(tag)
    for token in tokens:
        for kw in keywords:
            if kw in token:
                return True
    return False


def _href_target_id(tag) -> Optional[str]:
    href = tag.get("href")
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href.startswith("#"):
        return None
    target = href[1:].strip()
    return target or None


def _find_call_target_id(call_tag) -> Optional[str]:
    direct = _href_target_id(call_tag)
    if direct:
        return direct
    anchor = call_tag.find("a")
    if anchor is None:
        return None
    return _href_target_id(anchor)


def detect_footnote_links(html_text: str) -> Dict[str, str]:
    """
    Détecte les paires appel↔note et retourne ``{call_id: note_body_id}``.
    Les identifiants d'appel sont stabilisés via l'attribut ``id`` si présent,
    sinon un identifiant synthétique ``fn_call_XXXX``.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.find("body")
    if not body:
        return {}

    id_map = {}
    for tag in body.find_all(True):
        tid = tag.get("id")
        if isinstance(tid, str) and tid.strip():
            id_map[tid.strip()] = tag

    footnote_links: Dict[str, str] = {}
    synthetic_index = 0
    seen_calls: Set[int] = set()

    call_candidates: List[Any] = []
    for anchor in body.find_all("a"):
        if _href_target_id(anchor) and anchor.find_parent("sup") is not None:
            call_candidates.append(anchor)
    for tag in body.find_all(True):
        if _class_matches_any(tag, _FOOTNOTE_REF_CLASS_KEYWORDS):
            call_candidates.append(tag)

    for call_tag in call_candidates:
        marker = id(call_tag)
        if marker in seen_calls:
            continue
        seen_calls.add(marker)

        target = _find_call_target_id(call_tag)
        if not target:
            continue
        note_body = id_map.get(target)
        if note_body is None:
            continue
        call_id = call_tag.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            synthetic_index += 1
            call_id = f"fn_call_{synthetic_index:04d}"
        footnote_links[str(call_id)] = target

    return footnote_links


def _build_footnote_context(body) -> Tuple[Dict[int, str], Dict[int, str], Dict[str, str]]:
    """
    Prépare les métadonnées nécessaires pour relier segments d'appel et segments de notes.

    Returns:
        call_key_by_tagid: id(tag appel) -> call_key
        note_id_by_tagid: id(tag note body) -> note_body_id
        note_to_call_key: note_body_id -> call_key
    """
    id_map = {}
    for tag in body.find_all(True):
        tid = tag.get("id")
        if isinstance(tid, str) and tid.strip():
            id_map[tid.strip()] = tag

    note_id_by_tagid: Dict[int, str] = {}
    for note_id, tag in id_map.items():
        note_id_by_tagid[id(tag)] = note_id
    for tag in body.find_all(True):
        if _class_matches_any(tag, _FOOTNOTE_BODY_CLASS_KEYWORDS):
            tid = tag.get("id")
            if isinstance(tid, str) and tid.strip():
                note_id_by_tagid[id(tag)] = tid.strip()

    call_key_by_tagid: Dict[int, str] = {}
    note_to_call_key: Dict[str, str] = {}
    synthetic_index = 0
    seen_calls: Set[int] = set()

    call_candidates: List[Any] = []
    for anchor in body.find_all("a"):
        if _href_target_id(anchor) and anchor.find_parent("sup") is not None:
            call_candidates.append(anchor)
    for tag in body.find_all(True):
        if _class_matches_any(tag, _FOOTNOTE_REF_CLASS_KEYWORDS):
            call_candidates.append(tag)

    for call_tag in call_candidates:
        marker = id(call_tag)
        if marker in seen_calls:
            continue
        seen_calls.add(marker)

        target = _find_call_target_id(call_tag)
        if not target:
            continue
        if target not in id_map:
            continue
        call_key = call_tag.get("id")
        if not isinstance(call_key, str) or not call_key.strip():
            synthetic_index += 1
            call_key = f"fn_call_{synthetic_index:04d}"
        call_key = str(call_key)
        call_key_by_tagid[id(call_tag)] = call_key
        note_to_call_key.setdefault(target, call_key)

    return call_key_by_tagid, note_id_by_tagid, note_to_call_key


def _first_ancestor_mapping_key(node, mapping: Dict[int, str]) -> Optional[str]:
    cur = getattr(node, "parent", None)
    while cur is not None:
        key = mapping.get(id(cur))
        if key is not None:
            return key
        cur = getattr(cur, "parent", None)
    return None


def _segment_text(value: SegmentValue) -> str:
    if isinstance(value, dict):
        return value.get("text", "")
    return value


def _segment_footnote_for(value: SegmentValue) -> Optional[str]:
    if isinstance(value, dict):
        linked = value.get("footnote_for")
        if isinstance(linked, str) and linked:
            return linked
    return None


def build_dedup_map(segments: Dict[str, SegmentValue]) -> Dict[str, str]:
    """
    Construit une map ``segment_id -> canonical_segment_id``.

    Deux segments ne sont dédupliqués que si leur texte est identique après ``strip()``
    et qu'ils partagent le même contexte ``footnote_for`` (ou absence de contexte).
    """
    groups: Dict[Tuple[str, Optional[str]], List[str]] = {}
    for sid in sorted(segments.keys()):
        value = segments[sid]
        key = (_segment_text(value).strip(), _segment_footnote_for(value))
        groups.setdefault(key, []).append(sid)

    dedup_map: Dict[str, str] = {}
    for ids in groups.values():
        canonical = ids[0]
        for sid in ids:
            dedup_map[sid] = canonical
    return dedup_map


def dedup_stats(segments: Dict[str, SegmentValue], dedup_map: Dict[str, str]) -> Tuple[int, int, float]:
    """Retourne (canoniques, alias, taille_moyenne_segments)."""
    total = len(segments)
    canonical_count = sum(1 for sid, canonical in dedup_map.items() if sid == canonical)
    alias_count = total - canonical_count
    avg_len = 0.0
    if total > 0:
        avg_len = sum(len(_segment_text(v)) for v in segments.values()) / float(total)
    return canonical_count, alias_count, avg_len


def select_canonical_segments(
    segments: Dict[str, SegmentValue],
    dedup_map: Dict[str, str],
) -> Dict[str, SegmentValue]:
    """Conserve l'ordre d'origine et ne garde que les segments canoniques."""
    canonical_segments: Dict[str, SegmentValue] = {}
    for sid, value in segments.items():
        if dedup_map.get(sid) == sid:
            canonical_segments[sid] = value
    return canonical_segments


def extract_segments_and_skeleton(html_text: str) -> Tuple[str, str, Dict[str, SegmentValue]]:
    """
    Parse le HTML, extrait les segments depuis ``<body>``, injecte ``{{Txxxx}}``.

    Returns:
        skeleton_html: document HTML complet (sérialisé) avec placeholders
        head_inner: contenu interne de ``<head>`` (fragment)
        segments: dictionnaire ordonné id → texte source
    """
    soup = BeautifulSoup(html_text, "html.parser")
    head = soup.find("head")
    head_inner = ""
    if head:
        parts = []
        for child in head.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            else:
                parts.append(str(child))
        head_inner = "".join(parts)

    body = soup.find("body")
    if not body:
        raise ValueError("No <body> in HTML — cannot extract translatable text")

    segments: Dict[str, str] = {}
    counter = 0
    call_key_by_tagid, note_id_by_tagid, note_to_call_key = _build_footnote_context(body)
    call_segment_by_call_key: Dict[str, str] = {}
    note_segments_by_note_id: Dict[str, List[str]] = {}

    # Copie de la liste : le parcours modifie l’arbre.
    for text_node in list(body.descendants):
        if not isinstance(text_node, NavigableString):
            continue
        if isinstance(text_node, Comment):
            continue
        if _is_under_skip_tag(text_node):
            continue
        raw = str(text_node)
        if raw.strip() == "":
            continue

        call_key = _first_ancestor_mapping_key(text_node, call_key_by_tagid)
        note_id = _first_ancestor_mapping_key(text_node, note_id_by_tagid)

        counter += 1
        sid = _segment_id(counter)
        segments[sid] = raw
        text_node.replace_with(f"{{{{{sid}}}}}")

        if call_key and call_key not in call_segment_by_call_key:
            call_segment_by_call_key[call_key] = sid

        if note_id:
            note_segments_by_note_id.setdefault(note_id, []).append(sid)

    segment_values: Dict[str, SegmentValue] = {}
    note_for_segment: Dict[str, str] = {}
    for note_id, note_sids in note_segments_by_note_id.items():
        call_key = note_to_call_key.get(note_id)
        if not call_key:
            continue
        call_sid = call_segment_by_call_key.get(call_key)
        if not call_sid:
            continue
        for sid in note_sids:
            note_for_segment[sid] = call_sid

    for sid, text in segments.items():
        footnote_for = note_for_segment.get(sid)
        if footnote_for:
            segment_values[sid] = {"text": text, "footnote_for": footnote_for}
        else:
            segment_values[sid] = text

    skeleton_html = str(soup)
    return skeleton_html, head_inner, segment_values


def _is_probably_external(url: str) -> bool:
    u = url.strip()
    if not u or u.startswith("#"):
        return True
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "javascript:", "data:")):
        return True
    if u.startswith("//"):
        return True
    return False


def _rebase_attr_url(value: str) -> str:
    """Préfixe ``assets/`` pour les URLs relatives (hors ancres / externes)."""
    if not isinstance(value, str):
        return value
    val = value.strip()
    if _is_probably_external(val):
        return value
    # srcset : traiter chaque déclaration
    if " " in val and ("," in val or val.endswith("w") or val.endswith("x")):
        pieces = []
        for part in val.split(","):
            chunk = part.strip()
            if not chunk:
                continue
            url_rest = chunk.split(None, 1)
            if not url_rest:
                continue
            u0 = url_rest[0]
            rest = url_rest[1] if len(url_rest) > 1 else ""
            if not _is_probably_external(u0) and not u0.startswith("/"):
                u0 = "assets/" + u0.replace("\\", "/")
            if rest:
                pieces.append(f"{u0} {rest}")
            else:
                pieces.append(u0)
        return ", ".join(pieces)
    if val.startswith("/"):
        return value
    return "assets/" + val.replace("\\", "/")


def rewrite_asset_urls(soup: BeautifulSoup) -> None:
    """Réécrit ``src`` / ``href`` / … vers le préfixe ``assets/``."""
    for tag in soup.find_all(True):
        for attr in _URL_ATTRS:
            if attr not in tag.attrs:
                continue
            v = tag[attr]
            if isinstance(v, str):
                tag[attr] = _rebase_attr_url(v)
            elif isinstance(v, list):
                tag[attr] = [_rebase_attr_url(x) if isinstance(x, str) else x for x in v]


def copy_assets_from_extract(extract_dir: str, main_html_path: str, temp_dir: str) -> str:
    """
    Copie toutes les ressources du HTMLZ sous ``<temp_dir>/assets/`` en préservant les chemins
    relatifs depuis la racine d’extraction. Le fichier HTML principal n’est pas recopié.
    """
    assets_root = os.path.join(temp_dir, "assets")
    extract_dir = os.path.abspath(extract_dir)
    main_abs = os.path.abspath(main_html_path)
    main_rel = os.path.normpath(os.path.relpath(main_abs, extract_dir)).replace("\\", "/")

    os.makedirs(assets_root, exist_ok=True)

    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, extract_dir)
            rel_posix = rel.replace("\\", "/")
            if rel_posix.lower() == main_rel.lower():
                continue
            dest = os.path.join(assets_root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)

    return assets_root


def copy_assets_from_marker_extract(marker_output_dir: str, markdown_path: str, temp_dir: str) -> str:
    """
    Copie les ressources produites par marker sous ``<temp_dir>/assets/``.
    Les fichiers markdown sources ne sont pas copiés.
    """
    assets_root = os.path.join(temp_dir, "assets")
    marker_output_dir = os.path.abspath(marker_output_dir)
    markdown_abs = os.path.abspath(markdown_path)

    os.makedirs(assets_root, exist_ok=True)

    for root, _dirs, files in os.walk(marker_output_dir):
        for name in files:
            src = os.path.join(root, name)
            if os.path.abspath(src) == markdown_abs:
                continue
            if name.lower().endswith((".md", ".markdown")):
                continue
            rel = os.path.relpath(src, marker_output_dir)
            dest = os.path.join(assets_root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)

    return assets_root


def write_skeleton_and_segments(temp_dir: str, html_text: str) -> Dict[str, SegmentValue]:
    """Écrit skeleton/head/segments depuis un HTML source."""
    skeleton_path = os.path.join(temp_dir, "skeleton.html")
    segments_path = os.path.join(temp_dir, "segments.json")
    head_path = os.path.join(temp_dir, "head.html")

    soup = BeautifulSoup(html_text, "html.parser")
    rewrite_asset_urls(soup)
    rewritten_html = str(soup)

    skeleton_html, head_inner, segments = extract_segments_and_skeleton(rewritten_html)
    with open(skeleton_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(skeleton_html)
    with open(head_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(head_inner)
    with open(segments_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    print(f"Wrote skeleton.html ({len(skeleton_html)} chars), {len(segments)} segments")
    return segments


def build_translation_chunks(
    segments: Dict[str, SegmentValue],
    temp_dir: str,
    chunk_size: int,
    dedup_map: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Regroupe les segments en ``chunk0001.txt``, … sans couper un segment.
    Chaque ligne : ``T0001:`` + texte (échappé pour les retours ligne).
    Les segments de note liés (``footnote_for``) sont forcés dans le chunk de leur appel,
    même si cela dépasse ``chunk_size``.
    """
    chunk_files: List[str] = []
    chunk_to_ids: Dict[int, List[str]] = {}
    current_ids: List[str] = []
    current_chars = 0
    chunk_index = 0
    sid_to_chunk: Dict[str, int] = {}

    def current_chunk_id() -> int:
        return chunk_index + 1

    def flush() -> None:
        nonlocal chunk_index, current_ids, current_chars
        if not current_ids:
            return
        chunk_index += 1
        chunk_to_ids[chunk_index] = list(current_ids)
        for sid in current_ids:
            sid_to_chunk[sid] = chunk_index
        current_ids = []
        current_chars = 0

    dedup_lookup = dedup_map or {}

    for sid, value in segments.items():
        text = _segment_text(value)
        footnote_for = _segment_footnote_for(value)
        if footnote_for:
            footnote_for = dedup_lookup.get(footnote_for, footnote_for)
        if footnote_for and footnote_for in sid_to_chunk:
            linked_chunk = sid_to_chunk[footnote_for]
            if linked_chunk == current_chunk_id():
                current_ids.append(sid)
                current_chars += len(text)
            else:
                chunk_to_ids.setdefault(linked_chunk, []).append(sid)
            sid_to_chunk[sid] = linked_chunk
            continue

        t_len = len(text)
        if current_ids and current_chars + t_len > chunk_size:
            flush()
        current_ids.append(sid)
        current_chars += t_len
        sid_to_chunk[sid] = current_chunk_id()

    flush()
    for idx in sorted(chunk_to_ids):
        ids_in_chunk = chunk_to_ids[idx]
        name = f"chunk{idx:04d}.txt"
        path = os.path.join(temp_dir, name)

        comment_lines: List[str] = []
        for sid in ids_in_chunk:
            linked_to = _segment_footnote_for(segments[sid])
            if linked_to:
                comment_lines.append(f"# NOTE: {sid} is footnote for {linked_to}")

        payload_lines = []
        for sid in ids_in_chunk:
            payload_lines.append(f"{sid}: {_escape_chunk_payload(_segment_text(segments[sid]))}")

        lines = comment_lines + payload_lines
        body = "\n".join(lines) + "\n"
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        chunk_files.append(name)

        nseg = len(ids_in_chunk)
        nchars = sum(len(_segment_text(segments[i])) for i in ids_in_chunk)
        first_id = ids_in_chunk[0]
        last_id = ids_in_chunk[-1]
        print(
            f"  {name}: chunk {idx}, {nseg} segments, "
            f"{nchars} characters, ids {first_id}–{last_id}"
        )
    return chunk_files


def create_config_file(
    temp_dir: str,
    input_file: str,
    input_lang: str,
    output_lang: str,
    style: str,
    metadata: Optional[dict] = None,
    conversion_method: str = "calibre_htmlz_segments",
) -> bool:
    """Crée ``config.txt`` pour la suite du pipeline."""
    try:
        config_file = os.path.join(temp_dir, "config.txt")
        lines = [
            "# Translation Configuration",
            f"input_file={input_file}",
            f"input_lang={input_lang}",
            f"output_lang={output_lang}",
            f"style={style}",
            f"conversion_method={conversion_method}",
            "",
        ]
        if metadata:
            lines.append("# Book Metadata")
            if metadata.get("title"):
                lines.append(f"original_title={metadata['title']}")
            if metadata.get("creator"):
                lines.append(f"creator={metadata['creator']}")
            if metadata.get("publisher"):
                lines.append(f"publisher={metadata['publisher']}")
            if metadata.get("language"):
                lines.append(f"source_language={metadata['language']}")
        with open(config_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Created config file: {config_file}")
        return True
    except Exception as e:
        print(f"Error creating config file: {e}")
        return False


def _glob_chunk_txt(temp_dir: str) -> List[str]:
    names = [f for f in os.listdir(temp_dir) if re.match(r"^chunk\d{4}\.txt$", f, re.I)]
    return sorted(names, key=lambda x: int(re.search(r"(\d+)", x).group(1)))


def run_pipeline(
    input_file: str,
    temp_dir: str,
    chunk_size: int,
    ilang: str,
    olang: str,
    style: str,
    calibre_path: Optional[str],
    conversion_engine: str,
    marker_cmd: Optional[str],
    force_htmlz: bool,
    preserve_svg: str,
) -> bool:
    """Exécute conversion + extraction segments + chunks + manifest."""
    htmlz_file = f"{os.path.splitext(input_file)[0]}.htmlz"
    os.makedirs(temp_dir, exist_ok=True)

    skeleton_path = os.path.join(temp_dir, "skeleton.html")
    segments_path = os.path.join(temp_dir, "segments.json")
    head_path = os.path.join(temp_dir, "head.html")
    dedup_map_path = os.path.join(temp_dir, "dedup_map.json")

    reuse_state = (
        os.path.isfile(skeleton_path)
        and os.path.isfile(segments_path)
        and os.path.isdir(os.path.join(temp_dir, "assets"))
    )
    metadata: dict = {}

    if reuse_state and not force_htmlz:
        print("Skipping HTMLZ — skeleton.html, segments.json and assets/ already present")
        with open(segments_path, "r", encoding="utf-8") as f:
            segments: Dict[str, SegmentValue] = json.load(f, object_pairs_hook=dict)

        cfg = os.path.join(temp_dir, "config.txt")
        if os.path.isfile(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.strip().split("=", 1)
                        if k == "original_title":
                            metadata["title"] = v
                        elif k == "creator":
                            metadata["creator"] = v
                        elif k == "publisher":
                            metadata["publisher"] = v
                        elif k == "source_language":
                            metadata["language"] = v
    else:
        if conversion_engine == "marker":
            if not marker_cmd:
                print("Error: marker engine selected but marker command is missing")
                return False
            try:
                page_svg_map = extract_svg_assets_from_pdf(input_file, temp_dir, preserve_svg)
            except RuntimeError as e:
                print(str(e))
                return False
            with tempfile.TemporaryDirectory() as marker_output_dir:
                if not run_marker_single(input_file, marker_output_dir, marker_cmd):
                    return False
                markdown_path = _find_marker_markdown(marker_output_dir)
                if not markdown_path:
                    print("Error: marker did not produce a markdown file")
                    return False
                with open(markdown_path, "r", encoding="utf-8", errors="replace") as f:
                    md_text = f.read()
                html_text = markdown_to_html(md_text)
                html_text, replaced = replace_marker_png_with_extracted_svg(html_text, page_svg_map)
                if replaced > 0:
                    print(f"Replaced {replaced} Marker PNG figure(s) with extracted SVG.")
                segments = write_skeleton_and_segments(temp_dir, html_text)
                copy_assets_from_marker_extract(marker_output_dir, markdown_path, temp_dir)
        else:
            if not calibre_path:
                print("Error: calibre engine selected but ebook-convert is not available")
                return False
            if not force_htmlz and os.path.isfile(skeleton_path):
                print("Incomplete temp state — re-running HTMLZ conversion")
            if not convert_to_htmlz(input_file, htmlz_file, calibre_path):
                return False

            with tempfile.TemporaryDirectory() as extract_dir:
                with zipfile.ZipFile(htmlz_file, "r") as zf:
                    zf.extractall(extract_dir)

                main_html = _find_main_html_in_extract(extract_dir)
                if not main_html:
                    print("Error: No HTML file found inside HTMLZ")
                    return False

                metadata = extract_metadata_from_extract_dir(extract_dir)
                with open(main_html, "r", encoding="utf-8", errors="replace") as f:
                    html_text = f.read()
                segments = write_skeleton_and_segments(temp_dir, html_text)
                copy_assets_from_extract(extract_dir, main_html, temp_dir)

            if os.path.isfile(htmlz_file):
                try:
                    os.remove(htmlz_file)
                except OSError:
                    pass

    dedup_map = build_dedup_map(segments)
    canonical_segments_count, deduped_segments_count, avg_seg_len = dedup_stats(segments, dedup_map)
    est_saved_chars = deduped_segments_count * avg_seg_len
    with open(dedup_map_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dedup_map, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(
        "Dedup summary: "
        f"{canonical_segments_count} canonical / {len(segments)} total, "
        f"{deduped_segments_count} deduplicated alias segments, "
        f"estimated savings ≈ {est_saved_chars:.1f} chars "
        f"({deduped_segments_count} × {avg_seg_len:.1f})."
    )

    existing_chunks = _glob_chunk_txt(temp_dir)
    if existing_chunks:
        print(f"Skipping chunk build — found {len(existing_chunks)} existing chunk*.txt")
        chunk_files = existing_chunks
    else:
        print(f"Building translation chunks (threshold {chunk_size} characters cumulative)")
        canonical_segments = select_canonical_segments(segments, dedup_map)
        chunk_files = build_translation_chunks(
            canonical_segments,
            temp_dir,
            chunk_size,
            dedup_map=dedup_map,
        )
        if not chunk_files:
            print("Error: No chunks produced (empty book?)")
            return False
        print(f"Split into {len(chunk_files)} chunks")

    create_manifest(
        temp_dir,
        chunk_files,
        source_md_path=segments_path,
        skeleton_path=skeleton_path,
    )
    create_config_file(
        temp_dir,
        input_file,
        ilang,
        olang,
        style,
        metadata,
        conversion_method=(
            "marker_markdown_segments"
            if conversion_engine == "marker"
            else "calibre_htmlz_segments"
        ),
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert PDF/DOCX/EPUB to HTML skeleton + segment chunks (Calibre HTMLZ)",
    )
    parser.add_argument("input_file", help="Input file (PDF, DOCX, or EPUB)")
    parser.add_argument("-l", "--ilang", default="auto", help="Input language (default: auto)")
    parser.add_argument("--olang", default="zh", help="Output language (default: zh)")
    parser.add_argument(
        "--style",
        default="auto",
        choices=["formal", "literary", "technical", "conversational", "auto"],
        help="Translation style register (default: auto)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=6000,
        help="Max cumulative characters per chunk file (default: 6000)",
    )
    parser.add_argument(
        "--force-htmlz",
        action="store_true",
        help="Re-run extraction stage even if skeleton.html exists",
    )
    parser.add_argument(
        "--pdf-engine",
        default="auto",
        choices=["auto", "calibre", "marker"],
        help="PDF extraction engine: auto (heuristic), calibre, marker (default: auto)",
    )
    parser.add_argument(
        "--preserve-svg",
        default="auto",
        choices=["auto", "always", "never"],
        help=(
            "SVG preservation mode for PDF marker flow: "
            "auto (best effort), always (error if tools missing/fail), never (disable)"
        ),
    )
    args = parser.parse_args()
    input_file = args.input_file

    if not os.path.isfile(input_file):
        print(f"Error: Input file not found: {input_file}")
        sys.exit(1)

    ext = os.path.splitext(input_file)[1].lower()
    if ext not in (".pdf", ".docx", ".epub"):
        print(f"Error: Unsupported file type: {ext}")
        sys.exit(1)

    selected_engine = "calibre"
    marker_cmd = None
    pdf_classification = "n/a"
    if ext == ".pdf":
        try:
            selected_engine, marker_cmd, pdf_classification = choose_pdf_engine(
                input_file,
                args.pdf_engine,
            )
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
    elif args.pdf_engine == "marker":
        print("Warning: --pdf-engine marker is only applicable to PDF input. Using calibre.")

    print("=== File conversion (document → segments) ===")
    print(f"Input file: {input_file}")
    print(f"Chunk cumulative threshold: {args.chunk_size} characters")
    if ext == ".pdf":
        print(f"PDF classification: {pdf_classification}")
        print(f"Selected PDF engine: {selected_engine}")

    calibre_path: Optional[str] = None
    if selected_engine == "calibre":
        calibre_path = find_calibre_convert()
        if not calibre_path:
            print("Error: Calibre ebook-convert not found")
            print("Please install Calibre: https://calibre-ebook.com/")
            sys.exit(1)

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    temp_dir = f"{base_name}_temp"

    try:
        ok = run_pipeline(
            input_file,
            temp_dir,
            args.chunk_size,
            args.ilang,
            args.olang,
            args.style,
            calibre_path,
            selected_engine,
            marker_cmd,
            args.force_htmlz,
            args.preserve_svg,
        )
        if not ok:
            sys.exit(1)
        print("Conversion completed successfully!")
        print(f"Temp directory: {temp_dir}")
        print("Artifacts: skeleton.html, head.html, segments.json, chunk*.txt, manifest.json, assets/")
    except KeyboardInterrupt:
        print("\nConversion interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
