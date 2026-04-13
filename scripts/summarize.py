#!/usr/bin/env python3
"""Prépare des prompts de résumé et few-shot sans appel LLM."""

import argparse
import glob
import json
import os
import re
from collections import Counter


CHUNK_NAME_RE = re.compile(r"chunk(\d+)\.txt$", re.IGNORECASE)
CHUNK_LINE_RE = re.compile(r"^(T\d{4,}):\s*(.*)\s*$")
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")


def list_chunk_paths(temp_dir):
    """Liste chunk*.txt triés par index numérique croissant."""
    found = []
    pattern = os.path.join(temp_dir, "chunk*.txt")
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        match = CHUNK_NAME_RE.match(name)
        if not match:
            continue
        found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return [path for _, path in found]


def select_uniform_indices(total_count, num_samples):
    """Sélectionne des indices uniformément répartis en incluant début et fin."""
    if total_count <= 0:
        return []
    if num_samples <= 1:
        return [0]

    wanted = min(num_samples, total_count)
    if wanted == 1:
        return [0]

    indices = []
    for i in range(wanted):
        idx = (i * (total_count - 1)) // (wanted - 1)
        if not indices or idx != indices[-1]:
            indices.append(idx)

    if indices[0] != 0:
        indices.insert(0, 0)
    if indices[-1] != total_count - 1:
        indices.append(total_count - 1)

    if len(indices) > wanted:
        indices = indices[: wanted - 1] + [total_count - 1]
    return indices


def parse_chunk_file(path):
    """Extrait les lignes Txxxx en ignorant les commentaires #."""
    segments = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            match = CHUNK_LINE_RE.match(line)
            if not match:
                continue
            sid = match.group(1)
            text = match.group(2).strip()
            if text:
                segments.append((sid, text))
    return segments


def build_chunk_excerpt(segments):
    return "\n".join(text for _, text in segments)


def detect_source_language(sample_text):
    """Heuristique légère sur 500 premiers caractères de segments.json."""
    if not sample_text:
        return "unknown"

    hangul = sum(1 for c in sample_text if "\uac00" <= c <= "\ud7af")
    kana = sum(
        1
        for c in sample_text
        if ("\u3040" <= c <= "\u309f") or ("\u30a0" <= c <= "\u30ff")
    )
    han = sum(1 for c in sample_text if "\u4e00" <= c <= "\u9fff")
    cyrillic = sum(1 for c in sample_text if "\u0400" <= c <= "\u04ff")

    if hangul > 0:
        return "ko"
    if kana > 0:
        return "ja"
    if han > 0:
        return "zh"
    if cyrillic > 0:
        return "ru"

    tokens = [t.lower() for t in WORD_RE.findall(sample_text)]
    if not tokens:
        return "unknown"

    stopwords = {
        "en": {
            "the",
            "and",
            "of",
            "to",
            "in",
            "a",
            "is",
            "that",
            "for",
            "with",
        },
        "fr": {
            "le",
            "la",
            "les",
            "de",
            "des",
            "et",
            "un",
            "une",
            "dans",
            "est",
            "que",
        },
        "es": {
            "el",
            "la",
            "los",
            "las",
            "de",
            "del",
            "y",
            "en",
            "que",
            "un",
            "una",
        },
        "de": {
            "der",
            "die",
            "das",
            "und",
            "ist",
            "ein",
            "eine",
            "zu",
            "den",
            "von",
            "mit",
        },
    }

    counts = Counter(tokens)
    scores = {}
    for lang, words in stopwords.items():
        score = 0
        for word in words:
            score += counts.get(word, 0)
        scores[lang] = score

    best_lang = max(scores, key=scores.get)
    if scores[best_lang] == 0:
        return "unknown"
    return best_lang


def select_longest_segments(segments, max_items):
    indexed = list(enumerate(segments))
    indexed.sort(key=lambda item: (-len(item[1][1]), item[0]))
    return [seg for _, seg in indexed[:max_items]]


def build_summary_prompt(sampled_excerpts):
    lines = []
    lines.append(
        "Voici des extraits d'un livre à traduire. Analyse-les et produis un résumé structuré au format suivant :"
    )
    lines.append("")
    lines.append(
        "GENRE: (un mot : roman, essai, manuel, article, poésie, etc.)"
    )
    lines.append("SUJET: (une phrase)")
    lines.append(
        "TON: (un mot : formel, littéraire, académique, conversationnel, humoristique, etc.)"
    )
    lines.append('ÉPOQUE: (si identifiable, sinon "indéterminée")')
    lines.append(
        'PERSONNAGES: (liste des noms propres principaux, si applicable, sinon "aucun")'
    )
    lines.append("RÉSUMÉ: (2-3 phrases décrivant le contenu global du livre)")
    lines.append("")
    lines.append("Ne produis rien d'autre que ces 6 champs.")
    lines.append("")

    total = len(sampled_excerpts)
    for pos, excerpt in enumerate(sampled_excerpts, start=1):
        if pos == 1:
            header = f"--- EXTRAIT {pos} (début du livre) ---"
        elif pos == total:
            header = f"--- EXTRAIT {pos} (fin du livre) ---"
        else:
            header = f"--- EXTRAIT {pos} ---"
        lines.append(header)
        lines.append(excerpt if excerpt else "(vide)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_fewshot_prompt(source_lang, olang, fewshot_segments):
    lines = []
    lines.append(
        f"Tu es un traducteur professionnel de {source_lang} vers {olang}. Voici un résumé du livre que tu traduis :"
    )
    lines.append("")
    lines.append(
        "[contenu de book_summary.json une fois produit — ce champ est un placeholder, rempli par l'orchestrateur]"
    )
    lines.append("")
    lines.append(
        "Voici 3 segments extraits du livre avec leur traduction exemplaire. Traduis chaque segment dans le style approprié au genre et au ton du livre."
    )
    lines.append("")

    for idx in range(3):
        if idx < len(fewshot_segments):
            text = fewshot_segments[idx][1]
        else:
            text = "(segment indisponible)"
        lines.append(f"Segment {idx + 1}: {text}")

    lines.append("")
    lines.append("Format de sortie :")
    lines.append("ORIGINAL_1: [texte original]")
    lines.append("TRADUCTION_1: [ta traduction]")
    lines.append("ORIGINAL_2: [texte original]")
    lines.append("TRADUCTION_2: [ta traduction]")
    lines.append("ORIGINAL_3: [texte original]")
    lines.append("TRADUCTION_3: [ta traduction]")
    return "\n".join(lines).rstrip() + "\n"


def run(temp_dir, olang, num_samples):
    if not os.path.isdir(temp_dir):
        print(f"Erreur : répertoire introuvable : {temp_dir}")
        return 1

    segments_path = os.path.join(temp_dir, "segments.json")
    if not os.path.isfile(segments_path):
        print(f"Erreur : fichier introuvable : {segments_path}")
        return 1

    chunk_paths = list_chunk_paths(temp_dir)
    if not chunk_paths:
        print("Erreur : aucun fichier chunk*.txt trouvé.")
        return 1

    with open(segments_path, "r", encoding="utf-8", errors="replace") as handle:
        probe = handle.read(500)
    source_lang = detect_source_language(probe)

    selected_indices = select_uniform_indices(len(chunk_paths), num_samples)
    sampled_excerpts = []
    sampled_segments = []

    for idx in selected_indices:
        segments = parse_chunk_file(chunk_paths[idx])
        sampled_segments.extend(segments)
        sampled_excerpts.append(build_chunk_excerpt(segments))

    fewshot_segments = select_longest_segments(sampled_segments, 3)

    summary_prompt = build_summary_prompt(sampled_excerpts)
    fewshot_prompt = build_fewshot_prompt(source_lang, olang, fewshot_segments)

    summary_prompt_path = os.path.join(temp_dir, "summary_prompt.txt")
    fewshot_prompt_path = os.path.join(temp_dir, "fewshot_prompt.txt")
    source_lang_path = os.path.join(temp_dir, "source_lang.txt")

    with open(summary_prompt_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(summary_prompt)
    with open(fewshot_prompt_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(fewshot_prompt)
    with open(source_lang_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source_lang + "\n")

    print(f"Chunks échantillonnés : {len(selected_indices)}")
    print(f"Indices sélectionnés : {selected_indices}")
    print(f"Langue source détectée : {source_lang}")
    print(f"Segments few-shot sélectionnés : {len(fewshot_segments)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Prépare des prompts de résumé/few-shot à partir de chunks."
    )
    parser.add_argument("--temp-dir", required=True, help="Répertoire temporaire du livre")
    parser.add_argument("--olang", required=True, help="Code langue cible")
    parser.add_argument(
        "--num-samples",
        type=int,
        default=5,
        help="Nombre de chunks échantillonnés pour le résumé",
    )
    args = parser.parse_args(argv)

    samples = args.num_samples if args.num_samples > 0 else 1
    return run(args.temp_dir, args.olang, samples)


if __name__ == "__main__":
    raise SystemExit(main())
