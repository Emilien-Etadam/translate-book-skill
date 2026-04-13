#!/usr/bin/env python3
"""
validate_consistency.py

Builds a complete translated segment map from output_chunk*.txt and reports:
- Glossary consistency violations (when glossary.json exists)
- Untranslated segments (source == translation)
- Empty translations
"""

import argparse
import json
import os
import re
import sys


CHUNK_LINE_RE = re.compile(r"^(T\d{4,}):\s*(.*)\s*$")
OUTPUT_CHUNK_RE = re.compile(r"^output_chunk(\d+)\.txt$", re.IGNORECASE)


def unescape_chunk_payload(text):
    out = []
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


def list_output_chunk_paths(temp_dir):
    found = []
    for name in os.listdir(temp_dir):
        match = OUTPUT_CHUNK_RE.match(name)
        if match:
            found.append((int(match.group(1)), os.path.join(temp_dir, name)))
    found.sort(key=lambda x: x[0])
    return [path for _, path in found]


def parse_translated_chunks(temp_dir):
    translated = {}
    warnings = []

    paths = list_output_chunk_paths(temp_dir)
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for lineno, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line:
                    continue
                match = CHUNK_LINE_RE.match(line)
                if not match:
                    warnings.append(
                        f"{os.path.basename(path)}:{lineno}: ignored line (expected Txxxx: ...)"
                    )
                    continue
                sid, payload = match.group(1), match.group(2)
                translated[sid] = unescape_chunk_payload(payload)
    return translated, warnings


def load_json_dict(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object.")
    out = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            out[key] = value
            continue
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                out[key] = text
    return out


def load_dedup_map_if_present(temp_dir):
    path = os.path.join(temp_dir, "dedup_map.json")
    if not os.path.isfile(path):
        return None
    return load_json_dict(path)


def apply_dedup_aliases(translated, dedup_map):
    expanded = dict(translated)
    if not dedup_map:
        return expanded
    for sid, canonical in dedup_map.items():
        if sid in expanded:
            continue
        if canonical in expanded:
            expanded[sid] = expanded[canonical]
    return expanded


def contains_term(text, term):
    return re.search(re.escape(term), text, flags=re.IGNORECASE) is not None


def summarize_found_text(source_term, translated_text):
    one_line = translated_text.replace("\r", " ").replace("\n", " ").strip()
    if not one_line:
        return "(empty)"
    if len(one_line) > 120:
        return one_line[:117] + "..."
    return one_line


def collect_glossary_violations(segments_source, segments_translated, glossary):
    violations = []
    for source_term, target_term in glossary.items():
        if not source_term.strip() or not target_term.strip():
            continue
        for sid in sorted(segments_source.keys()):
            src_text = segments_source.get(sid, "")
            translated_text = segments_translated.get(sid, "")

            if not src_text or not contains_term(src_text, source_term):
                continue
            if contains_term(translated_text, target_term):
                continue

            found = summarize_found_text(source_term, translated_text)
            if contains_term(translated_text, source_term):
                violations.append(
                    f'{sid}: "{source_term}" should be "{target_term}", found: "{source_term}" (untranslated)'
                )
            else:
                violations.append(
                    f'{sid}: "{source_term}" should be "{target_term}", found: "{found}"'
                )
    return violations


def collect_untranslated_segments(segments_source, segments_translated):
    lines = []
    for sid in sorted(segments_source.keys()):
        src = segments_source[sid]
        translated = segments_translated.get(sid, "")
        if translated == src:
            lines.append(f"{sid}: source and translation are identical")
    return lines


def collect_empty_translations(segments_source, segments_translated):
    lines = []
    for sid in sorted(segments_source.keys()):
        translated = segments_translated.get(sid, "")
        if translated.strip() == "":
            lines.append(f"{sid}: empty or whitespace-only")
    return lines


def build_report_text(
    glossary_violations, untranslated_segments, empty_translations, include_glossary_section
):
    has_issues = bool(untranslated_segments or empty_translations)
    if include_glossary_section:
        has_issues = bool(glossary_violations or has_issues)
    if not has_issues:
        return "No issues found.\n"

    blocks = []
    if include_glossary_section:
        blocks.append("=== GLOSSARY VIOLATIONS ===")
        if glossary_violations:
            blocks.extend(glossary_violations)
        else:
            blocks.append("(none)")
        blocks.append("")

    blocks.append("=== UNTRANSLATED SEGMENTS ===")
    if untranslated_segments:
        blocks.extend(untranslated_segments)
    else:
        blocks.append("(none)")

    blocks.append("")
    blocks.append("=== EMPTY TRANSLATIONS ===")
    if empty_translations:
        blocks.extend(empty_translations)
    else:
        blocks.append("(none)")
    blocks.append("")
    return "\n".join(blocks)


def main():
    parser = argparse.ArgumentParser(
        description="Validate glossary consistency and translation completeness."
    )
    parser.add_argument("--temp-dir", required=True, help="Temporary working directory.")
    parser.add_argument("--olang", required=True, help="Target language code.")
    args = parser.parse_args()

    temp_dir = args.temp_dir
    if not os.path.isdir(temp_dir):
        print(f"Error: temp directory not found: {temp_dir}", file=sys.stderr)
        sys.exit(1)

    segments_path = os.path.join(temp_dir, "segments.json")
    if not os.path.isfile(segments_path):
        print(f"Error: missing required file: {segments_path}", file=sys.stderr)
        sys.exit(1)

    output_chunks = list_output_chunk_paths(temp_dir)
    if not output_chunks:
        print("Error: no output_chunk*.txt files found.", file=sys.stderr)
        sys.exit(1)

    try:
        segments_source = load_json_dict(segments_path)
        segments_translated, parse_warnings = parse_translated_chunks(temp_dir)
        dedup_map = load_dedup_map_if_present(temp_dir)
        segments_translated = apply_dedup_aliases(segments_translated, dedup_map)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: failed to parse translation inputs: {exc}", file=sys.stderr)
        sys.exit(1)

    translated_json_path = os.path.join(temp_dir, "segments_translated.json")
    with open(translated_json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(segments_translated, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    glossary_path = os.path.join(temp_dir, "glossary.json")
    glossary = {}
    has_glossary = False
    if os.path.isfile(glossary_path):
        has_glossary = True
        try:
            glossary = load_json_dict(glossary_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Error: invalid glossary.json: {exc}", file=sys.stderr)
            sys.exit(1)

    glossary_violations = []
    if has_glossary:
        glossary_violations = collect_glossary_violations(
            segments_source, segments_translated, glossary
        )
    untranslated_segments = collect_untranslated_segments(
        segments_source, segments_translated
    )
    empty_translations = collect_empty_translations(segments_source, segments_translated)

    report_text = build_report_text(
        glossary_violations,
        untranslated_segments,
        empty_translations,
        has_glossary,
    )
    report_path = os.path.join(temp_dir, "consistency_report.txt")
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report_text)

    for warning in parse_warnings:
        print(f"Warning: {warning}")

    if report_text.strip() == "No issues found.":
        print("No issues found.")
    else:
        print(f"Consistency report written: {report_path}")


if __name__ == "__main__":
    main()
