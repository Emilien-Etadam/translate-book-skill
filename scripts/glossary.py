#!/usr/bin/env python3
"""Extrait des candidats de glossaire depuis segments.json (sans LLM)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Tuple

# Mots (Unicode) : lettres, chiffres internes, traits d'union/apostrophes internes
_WORD_RE = re.compile(
    r"[^\W\d_](?:['\-][^\W\d_]|[^\W\d_])*|\d+(?:[.,]\d+)*",
    re.UNICODE,
)


def _words_normalized(text: str) -> List[str]:
    t = text.lower()
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    return [w for w in t.split() if w]


def _split_sentences(text: str) -> List[str]:
    """Découpe en phrases pour repérer le début de phrase (premier mot exclu)."""
    parts: List[str] = []
    for block in text.split("\n"):
        b = block.strip()
        if not b:
            continue
        sub = re.split(r"(?<=[.!?])\s+", b)
        for s in sub:
            s = s.strip()
            if s:
                parts.append(s)
    return parts if parts else ([text.strip()] if text.strip() else [])


def _words_original(sentence: str) -> List[str]:
    return _WORD_RE.findall(sentence)


def _collect_ngram_frequencies(
    segments: Dict[str, str], min_freq: int
) -> Counter:
    uni: Counter = Counter()
    bi: Counter = Counter()
    tri: Counter = Counter()
    for text in segments.values():
        w = _words_normalized(text)
        uni.update(w)
        for i in range(len(w) - 1):
            bi[w[i] + " " + w[i + 1]] += 1
        for i in range(len(w) - 2):
            tri[w[i] + " " + w[i + 1] + " " + w[i + 2]] += 1
    out: Counter = Counter()
    for c in (uni, bi, tri):
        for term, n in c.items():
            if n >= min_freq:
                out[term] = max(out[term], n)
    return out


def _proper_noun_candidates(segments: Dict[str, str]) -> Counter:
    """Mots avec majuscule hors début de phrase, au moins une occurrence par segment cumul."""
    counts: Counter = Counter()
    for text in segments.values():
        for sentence in _split_sentences(text):
            words = _words_original(sentence)
            for idx, w in enumerate(words):
                if idx == 0:
                    continue
                if not w or not w[0].isupper():
                    continue
                if not any(ch.isalpha() for ch in w):
                    continue
                counts[w] += 1
    return Counter({w: n for w, n in counts.items() if n >= 2})


def _dedupe_casefold(terms: Iterable[Tuple[str, int]]) -> Dict[str, Tuple[str, int]]:
    """Fusionne par casefold ; fréquences additionnées ; affichage préfère la forme la plus « titre »."""
    by_cf: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for term, freq in terms:
        if not term or not term.strip():
            continue
        t = term.strip()
        by_cf[t.casefold()].append((t, freq))
    merged: Dict[str, Tuple[str, int]] = {}
    for cf, items in by_cf.items():
        # Même lemme peut venir des n-grammes normalisés et de l'heuristique nom propre : ne pas additionner.
        total = max(f for _, f in items)
        best = max(
            items,
            key=lambda x: (
                sum(1 for c in x[0] if c.isupper()),
                x[1],
                len(x[0]),
            ),
        )[0]
        merged[cf] = (best, total)
    return merged


def build_candidates(
    segments: Dict[str, str],
    min_freq: int,
    max_terms: int,
) -> List[Tuple[str, int]]:
    ngram_terms = _collect_ngram_frequencies(segments, min_freq)
    proper = _proper_noun_candidates(segments)

    pairs: List[Tuple[str, int]] = []
    for term, freq in ngram_terms.items():
        pairs.append((term, freq))
    for term, freq in proper.items():
        pairs.append((term, freq))

    deduped = _dedupe_casefold(pairs)
    ranked = sorted(deduped.values(), key=lambda x: (-x[1], -len(x[0]), x[0].lower()))
    return ranked[:max_terms]


def _normalize_segment_texts(raw: Dict[str, object]) -> Dict[str, str]:
    segments: Dict[str, str] = {}
    for key, value in raw.items():
        sid = str(key)
        if isinstance(value, str):
            segments[sid] = value
            continue
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                segments[sid] = text
    return segments


def run(temp_dir: str, olang: str, min_freq: int, max_terms: int) -> int:
    _ = olang  # réservé pour cohérence CLI / orchestration (SKILL.md)
    seg_path = os.path.join(temp_dir, "segments.json")
    if not os.path.isfile(seg_path):
        print(f"Erreur : fichier introuvable : {seg_path}", file=sys.stderr)
        return 1
    try:
        with open(seg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Erreur : impossible de lire segments.json : {e}", file=sys.stderr)
        return 1
    if not isinstance(raw, dict):
        print("Erreur : segments.json doit être un objet JSON.", file=sys.stderr)
        return 1
    segments = _normalize_segment_texts(raw)

    candidates = build_candidates(segments, min_freq, max_terms)
    out_path = os.path.join(temp_dir, "glossary_candidates.txt")
    os.makedirs(temp_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for term, freq in candidates:
            f.write(f"{term} ({freq})\n")
    print(f"Écrit {len(candidates)} candidat(s) dans {out_path}")
    return 0


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Extrait glossary_candidates.txt depuis segments.json."
    )
    p.add_argument("--temp-dir", required=True, help="Répertoire temporaire du livre")
    p.add_argument("--olang", required=True, help="Code langue cible (métadonnée pipeline)")
    p.add_argument("--min-freq", type=int, default=3, help="Seuil pour mots / n-grammes")
    p.add_argument("--max-terms", type=int, default=200, help="Nombre max de candidats")
    args = p.parse_args(argv)
    return run(args.temp_dir, args.olang, args.min_freq, args.max_terms)


if __name__ == "__main__":
    raise SystemExit(main())
