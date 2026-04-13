#!/usr/bin/env python3
"""Pipeline de préparation déterministe: conversion + dedup + glossary + chunking."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple, Union

import convert
import glossary
from manifest import create_manifest

SegmentValue = Union[str, Dict[str, str]]
_CHUNK_RE = re.compile(r"^chunk\d{4}\.txt$", re.IGNORECASE)
_OUTPUT_CHUNK_RE = re.compile(r"^output_chunk\d{4}\.txt$", re.IGNORECASE)


def _segment_text(value: SegmentValue) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        return ""
    return value


def _segment_footnote_for(value: SegmentValue) -> Optional[str]:
    if isinstance(value, dict):
        linked = value.get("footnote_for")
        if isinstance(linked, str) and linked:
            return linked
    return None


def build_dedup_map(segments: Dict[str, SegmentValue]) -> Dict[str, str]:
    groups: Dict[Tuple[str, Optional[str]], List[str]] = {}
    for sid in sorted(segments.keys()):
        key = (_segment_text(segments[sid]).strip(), _segment_footnote_for(segments[sid]))
        groups.setdefault(key, []).append(sid)

    dedup_map: Dict[str, str] = {}
    for ids in groups.values():
        canonical = ids[0]
        for sid in ids:
            dedup_map[sid] = canonical
    return dedup_map


def select_canonical_segments(
    segments: Dict[str, SegmentValue],
    dedup_map: Dict[str, str],
) -> Dict[str, SegmentValue]:
    out: Dict[str, SegmentValue] = {}
    for sid, value in segments.items():
        if dedup_map.get(sid) == sid:
            out[sid] = value
    return out


def _count_aliases(dedup_map: Dict[str, str]) -> int:
    return sum(1 for sid, canonical in dedup_map.items() if sid != canonical)


def _has_real_duplicates(dedup_map: Dict[str, str]) -> bool:
    return _count_aliases(dedup_map) > 0


def _clear_generated_files(temp_dir: str) -> None:
    if not os.path.isdir(temp_dir):
        return
    for name in os.listdir(temp_dir):
        if _CHUNK_RE.match(name) or _OUTPUT_CHUNK_RE.match(name):
            try:
                os.remove(os.path.join(temp_dir, name))
            except OSError:
                pass
            continue
        if name in {
            "manifest.json",
            "dedup_map.json",
            "glossary_candidates.txt",
            "pipeline_state.json",
            "missing_segments.txt",
            "segments_translated.json",
            "consistency_report.txt",
        }:
            try:
                os.remove(os.path.join(temp_dir, name))
            except OSError:
                pass


def _collect_metadata_for_html() -> Dict[str, str]:
    return {}


def _extract_to_segments(
    input_file: str,
    temp_dir: str,
    pdf_engine: str,
    preserve_svg: str,
) -> Tuple[Dict[str, SegmentValue], str, bool, Dict[str, str]]:
    ext = os.path.splitext(input_file)[1].lower()
    metadata: Dict[str, str] = {}
    svg_extracted = False

    if ext in (".html", ".htm"):
        with open(input_file, "r", encoding="utf-8", errors="replace") as f:
            html_text = f.read()
        segments = convert.write_skeleton_and_segments(temp_dir, html_text)
        return segments, "html_segments", False, _collect_metadata_for_html()

    selected_engine = "calibre"
    marker_cmd: Optional[str] = None
    if ext == ".pdf":
        selected_engine, marker_cmd, _ = convert.choose_pdf_engine(input_file, pdf_engine)
    elif pdf_engine == "marker":
        print("Warning: --pdf-engine marker ignoré hors PDF.")

    if selected_engine == "marker":
        if not marker_cmd:
            raise RuntimeError("Moteur marker sélectionné mais commande introuvable.")
        assets_root = os.path.join(temp_dir, "assets")
        before_svg = set()
        if os.path.isdir(assets_root):
            before_svg = {x for x in os.listdir(assets_root) if x.lower().endswith(".svg")}
        page_svg_map = convert.extract_svg_assets_from_pdf(input_file, temp_dir, preserve_svg)
        if page_svg_map:
            svg_extracted = True
        if os.path.isdir(assets_root):
            after_svg = {x for x in os.listdir(assets_root) if x.lower().endswith(".svg")}
            if len(after_svg - before_svg) > 0:
                svg_extracted = True

        with tempfile.TemporaryDirectory() as marker_output_dir:
            if not convert.run_marker_single(input_file, marker_output_dir, marker_cmd):
                raise RuntimeError("Échec extraction marker_single.")
            markdown_path = convert._find_marker_markdown(marker_output_dir)
            if not markdown_path:
                raise RuntimeError("Aucun markdown détecté en sortie marker.")
            with open(markdown_path, "r", encoding="utf-8", errors="replace") as f:
                md_text = f.read()
            html_text = convert.markdown_to_html(md_text)
            html_text, replaced = convert.replace_marker_png_with_extracted_svg(html_text, page_svg_map)
            if replaced > 0:
                svg_extracted = True
            segments = convert.write_skeleton_and_segments(temp_dir, html_text)
            convert.copy_assets_from_marker_extract(marker_output_dir, markdown_path, temp_dir)
        return segments, "marker_markdown_segments", svg_extracted, metadata

    calibre_path = convert.find_calibre_convert()
    if not calibre_path:
        raise RuntimeError("Calibre ebook-convert introuvable.")

    htmlz_file = f"{os.path.splitext(input_file)[0]}.htmlz"
    if not convert.convert_to_htmlz(input_file, htmlz_file, calibre_path):
        raise RuntimeError("Échec de conversion vers HTMLZ.")

    try:
        with tempfile.TemporaryDirectory() as extract_dir:
            with zipfile.ZipFile(htmlz_file, "r") as zf:
                zf.extractall(extract_dir)
            main_html = convert._find_main_html_in_extract(extract_dir)
            if not main_html:
                raise RuntimeError("Aucun HTML principal trouvé dans HTMLZ.")
            metadata = convert.extract_metadata_from_extract_dir(extract_dir)
            with open(main_html, "r", encoding="utf-8", errors="replace") as f:
                html_text = f.read()
            segments = convert.write_skeleton_and_segments(temp_dir, html_text)
            convert.copy_assets_from_extract(extract_dir, main_html, temp_dir)
    finally:
        if os.path.isfile(htmlz_file):
            try:
                os.remove(htmlz_file)
            except OSError:
                pass
    return segments, "calibre_htmlz_segments", False, metadata


def _list_chunk_files(temp_dir: str) -> List[str]:
    names = [n for n in os.listdir(temp_dir) if _CHUNK_RE.match(n)]
    names.sort(key=lambda x: int(re.search(r"(\d+)", x).group(1)))
    return names


def _count_glossary_candidates(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    count = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _chunks_with_footnotes(temp_dir: str) -> List[int]:
    out: List[int] = []
    for name in _list_chunk_files(temp_dir):
        idx = int(re.search(r"(\d+)", name).group(1))
        path = os.path.join(temp_dir, name)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            has_note = any(line.startswith("# NOTE:") for line in f)
        if has_note:
            out.append(idx)
    return out


def _footnote_pairs_count(segments: Dict[str, SegmentValue]) -> int:
    pairs = set()
    for value in segments.values():
        linked = _segment_footnote_for(value)
        if linked:
            pairs.add(linked)
    return len(pairs)


def _write_pipeline_state(temp_dir: str, state: Dict[str, object]) -> None:
    path = os.path.join(temp_dir, "pipeline_state.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run_prepare(args: argparse.Namespace) -> int:
    input_file = os.path.abspath(args.input_file)
    if not os.path.isfile(input_file):
        print(f"Error: fichier introuvable: {input_file}", file=sys.stderr)
        return 1

    ext = os.path.splitext(input_file)[1].lower()
    if ext not in (".pdf", ".docx", ".epub", ".html", ".htm"):
        print(f"Error: type de fichier non supporté: {ext}", file=sys.stderr)
        return 1

    base_name = os.path.splitext(os.path.basename(input_file))[0]
    temp_dir = os.path.join(os.path.dirname(input_file), f"{base_name}_temp")
    os.makedirs(temp_dir, exist_ok=True)
    _clear_generated_files(temp_dir)

    try:
        segments, conversion_method, svg_extracted, metadata = _extract_to_segments(
            input_file=input_file,
            temp_dir=temp_dir,
            pdf_engine=args.pdf_engine,
            preserve_svg=args.preserve_svg,
        )
    except Exception as e:
        print(f"Error: échec conversion/préparation: {e}", file=sys.stderr)
        return 1

    dedup_map = build_dedup_map(segments)
    alias_count = _count_aliases(dedup_map)
    dedup_map_path = os.path.join(temp_dir, "dedup_map.json")
    if _has_real_duplicates(dedup_map):
        with open(dedup_map_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(dedup_map, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    elif os.path.isfile(dedup_map_path):
        os.remove(dedup_map_path)

    glossary_rc = glossary.run(
        temp_dir=temp_dir,
        olang=args.olang,
        min_freq=args.min_freq,
        max_terms=args.max_terms,
    )
    if glossary_rc != 0:
        return glossary_rc

    canonical_segments = select_canonical_segments(segments, dedup_map)
    chunk_files = convert.build_translation_chunks(
        canonical_segments,
        temp_dir,
        args.chunk_size,
        dedup_map=dedup_map,
    )
    if not chunk_files:
        print("Error: aucun chunk généré.", file=sys.stderr)
        return 1

    segments_path = os.path.join(temp_dir, "segments.json")
    skeleton_path = os.path.join(temp_dir, "skeleton.html")
    create_manifest(
        temp_dir=temp_dir,
        chunk_files=chunk_files,
        source_md_path=segments_path,
        skeleton_path=skeleton_path,
    )
    convert.create_config_file(
        temp_dir=temp_dir,
        input_file=input_file,
        input_lang="auto",
        output_lang=args.olang,
        style=args.style,
        metadata=metadata,
        conversion_method=conversion_method,
    )

    glossary_candidates_path = os.path.join(temp_dir, "glossary_candidates.txt")
    glossary_candidates_count = _count_glossary_candidates(glossary_candidates_path)

    state = {
        "temp_dir": os.path.abspath(temp_dir),
        "input_file": os.path.basename(input_file),
        "target_lang": args.olang,
        "total_chunks": len(chunk_files),
        "total_segments": len(segments),
        "dedup_segments_skipped": alias_count,
        "glossary_candidates_count": glossary_candidates_count,
        "glossary_needed": glossary_candidates_count > 0,
        "style": args.style,
        "style_detection_needed": args.style == "auto",
        "conversion_method": conversion_method,
        "svg_extracted": svg_extracted,
        "footnote_pairs": _footnote_pairs_count(segments),
        "chunks_with_footnotes": _chunks_with_footnotes(temp_dir),
    }
    _write_pipeline_state(temp_dir, state)

    print("Préparation terminée.")
    print(f"Temp directory: {temp_dir}")
    print("Artifacts: skeleton.html, head.html, segments.json, chunk*.txt, manifest.json, pipeline_state.json")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Préparation déterministe: conversion + dedup + glossary + chunking.",
    )
    parser.add_argument("input_file", help="Fichier d'entrée (PDF, DOCX, EPUB, HTML)")
    parser.add_argument("--olang", required=True, help="Code langue cible")
    parser.add_argument("--chunk-size", type=int, default=6000, help="Taille max chunk (défaut: 6000)")
    parser.add_argument(
        "--style",
        default="auto",
        choices=["formal", "literary", "technical", "conversational", "auto"],
        help="Registre de traduction",
    )
    parser.add_argument(
        "--pdf-engine",
        default="auto",
        choices=["auto", "calibre", "marker"],
        help="Moteur PDF",
    )
    parser.add_argument(
        "--preserve-svg",
        default="auto",
        choices=["auto", "always", "never"],
        help="Mode préservation SVG pour flux Marker PDF",
    )
    parser.add_argument("--min-freq", type=int, default=3, help="Fréquence minimale glossaire")
    parser.add_argument("--max-terms", type=int, default=200, help="Nombre max de termes glossaire")
    args = parser.parse_args(argv)
    return run_prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
