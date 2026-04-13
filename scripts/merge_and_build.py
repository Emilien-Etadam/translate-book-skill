#!/usr/bin/env python3
"""
merge_and_build.py — Fusion des chunks traduits (.txt), réinjection dans skeleton.html,
assemblage du HTML complet et conversion Calibre (EPUB/DOCX/PDF).

Usage:
    python3 merge_and_build.py --temp-dir <répertoire> [--title <titre>] [--olang <langue>]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

# Placeholders du squelette : {{T0001}}, {{T12345}}, …
_PLACEHOLDER_RE = re.compile(r"\{\{(T\d{4,})\}\}")
# Lignes de chunk : T0001: texte…
_CHUNK_LINE_RE = re.compile(r"^(T\d{4,}):\s*(.*)\s*$")
_OUTPUT_CHUNK_RE = re.compile(r"^output_chunk(\d+)\.txt$", re.IGNORECASE)


def find_ebook_convert() -> Optional[str]:
    """Localise la commande Calibre ``ebook-convert``."""
    candidates = [
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        "/usr/bin/ebook-convert",
        "/usr/local/bin/ebook-convert",
        "ebook-convert",
    ]
    for path in candidates:
        try:
            r = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def load_config_txt(temp_dir: str) -> Dict[str, str]:
    """Lit config.txt si présent (clés ``clé=valeur``)."""
    cfg: Dict[str, str] = {}
    path = os.path.join(temp_dir, "config.txt")
    if not os.path.isfile(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def resolve_target_lang(temp_dir: str, olang_cli: Optional[str]) -> str:
    """Langue cible : ``--olang`` > ``output_lang`` dans config.txt > ``en``."""
    if olang_cli:
        return olang_cli.strip()
    cfg = load_config_txt(temp_dir)
    v = cfg.get("output_lang")
    if v:
        return v.strip()
    return "en"


def unescape_chunk_payload(text: str) -> str:
    """Inverse l’échappement des chunks : ``\\n``, ``\\r``, ``\\\\``."""
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def list_output_chunk_txt_paths(temp_dir: str) -> List[str]:
    """Chemins ``output_chunk*.txt`` triés par numéro."""
    found: List[Tuple[int, str]] = []
    for name in os.listdir(temp_dir):
        m = _OUTPUT_CHUNK_RE.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(temp_dir, name)))
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def parse_translated_chunks(temp_dir: str) -> Tuple[Dict[str, str], List[str]]:
    """
    Lit tous les ``output_chunk*.txt`` dans l’ordre numérique.
    Retourne (segments_translated, warnings).
    """
    segments: Dict[str, str] = {}
    warnings: List[str] = []
    paths = list_output_chunk_txt_paths(temp_dir)
    seen_ids: Dict[str, int] = {}

    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                m = _CHUNK_LINE_RE.match(line)
                if not m:
                    warnings.append(
                        f"{os.path.basename(path)}:{lineno}: ligne ignorée (format attendu Txxxx: …)"
                    )
                    continue
                sid, payload = m.group(1), m.group(2)
                prev = seen_ids.get(sid)
                if prev is not None:
                    warnings.append(
                        f"Identifiant dupliqué {sid} (déjà vu ligne {prev} dans {os.path.basename(path)})"
                    )
                seen_ids[sid] = lineno
                segments[sid] = unescape_chunk_payload(payload)

    return segments, warnings


def load_segments_json(temp_dir: str) -> Dict[str, str]:
    path = os.path.join(temp_dir, "segments.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Fichier introuvable : {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f, object_pairs_hook=dict)
    if not isinstance(data, dict):
        raise ValueError("segments.json doit contenir un objet JSON (dictionnaire).")
    out: Dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, str):
            out[k] = v
            continue
        if isinstance(v, dict):
            text = v.get("text")
            if isinstance(text, str):
                out[k] = text
    return out


def load_dedup_map(temp_dir: str) -> Optional[Dict[str, str]]:
    """
    Lit ``dedup_map.json`` si présent.
    Retourne ``None`` si absent (rétrocompatibilité).
    """
    path = os.path.join(temp_dir, "dedup_map.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("dedup_map.json doit contenir un objet JSON (dictionnaire).")
    out: Dict[str, str] = {}
    for sid, canonical in data.items():
        if isinstance(sid, str) and isinstance(canonical, str):
            out[sid] = canonical
    return out


def apply_dedup_aliases(
    segments_translated: Dict[str, str],
    dedup_map: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """
    Étend les traductions avec les alias dédupliqués.

    Si ``dedup_map`` est absent, retourne une copie inchangée.
    """
    expanded = dict(segments_translated)
    if not dedup_map:
        return expanded

    def resolve_canonical(sid: str) -> Optional[str]:
        seen = set()
        cur = sid
        while cur in dedup_map and cur not in seen:
            seen.add(cur)
            nxt = dedup_map[cur]
            if nxt == cur:
                return cur
            cur = nxt
        return cur if cur not in seen else None

    for sid in dedup_map:
        if sid in expanded:
            continue
        canonical = resolve_canonical(sid)
        if canonical and canonical in expanded:
            expanded[sid] = expanded[canonical]
    return expanded


def validate_translation_completeness(
    segments_source: Dict[str, str], segments_translated: Dict[str, str], temp_dir: str
) -> List[str]:
    """Retourne la liste des identifiants manquants (vide si complet)."""
    missing = [sid for sid in segments_source if sid not in segments_translated]
    if missing:
        report = os.path.join(temp_dir, "missing_segments.txt")
        with open(report, "w", encoding="utf-8", newline="\n") as f:
            f.write("Identifiants présents dans segments.json sans traduction :\n")
            for sid in sorted(missing):
                f.write(sid + "\n")
    return missing


def reinject_placeholders(skeleton_html: str, segments_translated: Dict[str, str]) -> str:
    """Remplace chaque ``{{Txxxx}}`` via un seul ``re.sub`` et lookup dans le dictionnaire."""

    def repl(match: re.Match[str]) -> str:
        sid = match.group(1)
        return segments_translated[sid]

    return _PLACEHOLDER_RE.sub(repl, skeleton_html)


def count_placeholders(skeleton_html: str) -> int:
    return len(_PLACEHOLDER_RE.findall(skeleton_html))


def inject_title_in_head_fragment(head_inner: str, title: Optional[str]) -> str:
    """Met à jour ou insère ``<title>`` dans le fragment ``<head>``."""
    if not title:
        return head_inner
    soup = BeautifulSoup(f"<head>{head_inner}</head>", "html.parser")
    head = soup.find("head")
    if not head:
        return head_inner
    ttag = head.find("title")
    if ttag:
        ttag.clear()
        ttag.append(title)
    else:
        nt = soup.new_tag("title")
        nt.string = title
        head.insert(0, nt)
    parts: List[str] = []
    for child in head.children:
        parts.append(str(child))
    return "".join(parts)


def build_full_html(
    reinjected_skeleton: str,
    head_inner: str,
    html_lang: str,
    title: Optional[str],
) -> str:
    """Assemble ``<!DOCTYPE html>``, ``<html lang>``, ``<head>`` + corps issu du squelette réinjecté."""
    head_use = inject_title_in_head_fragment(head_inner, title)
    soup = BeautifulSoup(reinjected_skeleton, "html.parser")
    body = soup.find("body")
    if not body:
        raise ValueError("Le squelette réinjecté ne contient pas de balise <body>.")
    body_html = str(body)
    lang_esc = html.escape(html_lang, quote=True)
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{lang_esc}">\n'
        "<head>\n"
        f"{head_use}\n"
        "</head>\n"
        f"{body_html}\n"
        "</html>\n"
    )


def run_ebook_convert(
    ebook_convert_exe: str,
    src_html: str,
    dest_path: str,
    extra_args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    timeout: int = 600,
) -> bool:
    cmd = [ebook_convert_exe, src_html, dest_path]
    if extra_args:
        cmd.extend(extra_args)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        if r.returncode == 0 and os.path.isfile(dest_path):
            return True
        tail = (r.stderr or r.stdout or "").strip()
        if tail:
            print(f"Avertissement : ebook-convert a échoué pour {dest_path} :\n{tail[-2000:]}")
        else:
            print(f"Avertissement : ebook-convert a échoué pour {dest_path} (code {r.returncode}).")
        return False
    except FileNotFoundError:
        print(f"Avertissement : exécutable introuvable : {ebook_convert_exe}")
        return False
    except subprocess.TimeoutExpired:
        print(f"Avertissement : dépassement du délai pour {dest_path}.")
        return False
    except OSError as e:
        print(f"Avertissement : erreur lors de la conversion vers {dest_path} : {e}")
        return False


def generate_calibre_outputs(temp_dir: str, book_html: str) -> Tuple[int, List[str]]:
    """
    Tente EPUB, DOCX, PDF via ``ebook-convert``.
    Retourne (nombre de réussites, chemins des fichiers produits).
    """
    exe = find_ebook_convert()
    if not exe:
        print(
            "Avertissement : Calibre (ebook-convert) est introuvable — "
            "seul book.html est produit. Installez Calibre : https://calibre-ebook.com/"
        )
        return 0, []

    produced: List[str] = []
    ok = 0
    book_abs = os.path.abspath(book_html)
    base_extra_args = ["--extra-css", "svg { max-width: 100%; height: auto; }"]
    for ext in (".epub", ".docx", ".pdf"):
        dest = os.path.abspath(os.path.join(temp_dir, f"book{ext}"))
        extra_args = list(base_extra_args)
        if ext == ".epub":
            extra_args.extend(["--preserve-cover-aspect-ratio", "--no-svg-cover"])
        if run_ebook_convert(exe, book_abs, dest, extra_args=extra_args, cwd=temp_dir):
            ok += 1
            produced.append(dest)
    return ok, produced


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fusion des output_chunk*.txt → book.html (+ EPUB/DOCX/PDF via Calibre).",
    )
    parser.add_argument("--temp-dir", required=True, help="Répertoire de travail (temp)")
    parser.add_argument("--title", default=None, help="Titre traduit (balise <title>)")
    parser.add_argument("--olang", default=None, help="Langue cible (attribut lang de <html>)")

    args = parser.parse_args()
    temp_dir = args.temp_dir

    if not os.path.isdir(temp_dir):
        print(f"Erreur : répertoire introuvable : {temp_dir}", file=sys.stderr)
        sys.exit(1)

    target_lang = resolve_target_lang(temp_dir, args.olang)

    skeleton_path = os.path.join(temp_dir, "skeleton.html")
    head_path = os.path.join(temp_dir, "head.html")
    for p, label in (
        (skeleton_path, "skeleton.html"),
        (head_path, "head.html"),
    ):
        if not os.path.isfile(p):
            print(f"Erreur : fichier requis manquant : {label} ({p})", file=sys.stderr)
            sys.exit(1)

    try:
        segments_source = load_segments_json(temp_dir)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"Erreur : impossible de charger segments.json : {e}", file=sys.stderr)
        sys.exit(1)
    try:
        dedup_map = load_dedup_map(temp_dir)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Erreur : impossible de charger dedup_map.json : {e}", file=sys.stderr)
        sys.exit(1)

    paths = list_output_chunk_txt_paths(temp_dir)
    if not paths:
        print(
            "Erreur : aucun fichier output_chunk*.txt trouvé — traductions manquantes.",
            file=sys.stderr,
        )
        sys.exit(1)

    segments_translated, parse_warnings = parse_translated_chunks(temp_dir)
    segments_translated = apply_dedup_aliases(segments_translated, dedup_map)
    for w in parse_warnings:
        print(f"Avertissement : {w}")

    missing = validate_translation_completeness(segments_source, segments_translated, temp_dir)
    if missing:
        print(
            f"Erreur : {len(missing)} segment(s) sans traduction. "
            f"Rapport écrit : {os.path.join(temp_dir, 'missing_segments.txt')}",
            file=sys.stderr,
        )
        print("Identifiants manquants (extrait) : " + ", ".join(sorted(missing)[:40]), file=sys.stderr)
        if len(missing) > 40:
            print(f"… et {len(missing) - 40} autre(s).", file=sys.stderr)
        sys.exit(1)

    with open(skeleton_path, "r", encoding="utf-8", errors="replace") as f:
        skeleton_html = f.read()

    n_ph = count_placeholders(skeleton_html)
    reinjected = reinject_placeholders(skeleton_html, segments_translated)

    with open(head_path, "r", encoding="utf-8", errors="replace") as f:
        head_inner = f.read()

    title = args.title

    try:
        full_html = build_full_html(reinjected, head_inner, target_lang, title)
    except ValueError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)

    book_html_path = os.path.join(temp_dir, "book.html")
    with open(book_html_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(full_html)

    n_formats, format_paths = generate_calibre_outputs(temp_dir, book_html_path)

    print("=== Fusion et construction terminées ===")
    print(f"Segments réinjectés (placeholders remplacés) : {n_ph}")
    print(f"Formats générés par Calibre : {n_formats}")
    print(f"Fichier principal : {book_html_path}")
    for p in format_paths:
        print(f"  → {p}")

    if n_formats == 0 and find_ebook_convert() is None:
        pass
    elif n_formats < 3:
        print("(Certaines conversions Calibre ont échoué ; book.html est utilisable.)")


if __name__ == "__main__":
    main()
