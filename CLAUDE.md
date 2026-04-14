# CLAUDE.md

## Project

translate-book is a Claude Code Skill that translates books (PDF/DOCX/EPUB) into any language using parallel subagents. Published on ClawHub as `translate-book` and on GitHub as `deusyu/translate-book`.

## Structure

- `SKILL.md` — Skill definition, the orchestration logic that Claude Code / OpenClaw follows
- `scripts/prepare.py` — Deterministic preprocessing entrypoint: conversion routing (`--pdf-engine auto|calibre|marker`, `--preserve-svg auto|always|never`), segment extraction, in-script dedup (`dedup_map.json` when aliases exist), glossary candidate extraction, canonical-only chunking, summarize prompt generation (`--num-samples`), manifest/config generation, and `pipeline_state.json`
- `scripts/convert.py` — Importable conversion primitives and standalone conversion CLI
- `scripts/glossary.py` — Importable glossary candidate extraction and standalone CLI
- `scripts/summarize.py` — Build `summary_prompt.txt` / `fewshot_prompt.txt`, detect source language, and persist `source_lang.txt`
- `scripts/manifest.py` — SHA-256 chunk tracking and merge validation
- `scripts/merge_and_build.py` — Parse `output_chunk*.txt`, expand aliases from `dedup_map.json` when present, validate vs `segments.json`, reinject `skeleton.html`, assemble `book.html`, Calibre → DOCX/EPUB/PDF
- `scripts/validate_consistency.py` — Build `segments_translated.json` from all `output_chunk*.txt` (with alias expansion when `dedup_map.json` exists) and write `consistency_report.txt` (glossary violations, untranslated segments, empty translations)

## Testing changes

Test with a small PDF to verify the full pipeline:

```bash
python3 scripts/prepare.py /path/to/small.pdf --olang zh
# then run translation via the skill
python3 scripts/validate_consistency.py --temp-dir <name>_temp --olang zh
python3 scripts/merge_and_build.py --temp-dir <name>_temp --title "test" --olang zh
```

Verify: all `output_chunk*.txt` files exist, consistency report is generated (`No issues found.` or actionable findings), manifest validation passes, output formats generate.
Also verify summarize artifacts exist after prepare: `summary_prompt.txt`, `fewshot_prompt.txt`, `source_lang.txt`.

Optional PDF tools:

- Poppler (`pdfinfo`, `pdftotext`) provides informational PDF structure classification logs (does not control routing in auto mode)
- `marker-pdf` (`marker_single`) enables structured extraction for PDFs; in `--pdf-engine auto`, Marker is preferred whenever available, otherwise auto mode falls back to Calibre with warning
- `pdf2svg` or `mutool` enables optional SVG extraction/preservation in Marker PDF flow (`--preserve-svg auto|always|never`)

## Conventions

- Only `chunk*.txt` / `output_chunk*.txt` naming — no `page*` legacy support
- SKILL.md frontmatter must stay single-line per field (OpenClaw parser requirement)
- Script paths in SKILL.md use `{baseDir}` not hardcoded paths
- Subagent instructions in SKILL.md must be platform-neutral (work on Claude Code, OpenClaw, Codex)
- README changes must be synced to both README.md and README.zh-CN.md
- Glossary flow: `prepare.py` writes `glossary_candidates.txt` and `pipeline_state.json`; run one glossary sub-agent only when `glossary_needed=true`; translation sub-agents inject `glossary.json` when present (SKILL.md)
- Summary/few-shot flow: `prepare.py` always runs `summarize.py` to generate `summary_prompt.txt`, `fewshot_prompt.txt`, and `source_lang.txt`; SKILL runs one summary sub-agent (`book_summary.json`) and one few-shot sub-agent (`fewshot_examples.txt`) before style detection
- Style flow: use `pipeline_state.json` as source of truth; if `style_detection_needed=true`, detect from first chunks via one sub-agent, else use `style` directly; inject the mapped style instruction into translator prompts
- Translator prompt assembly order: style instruction → formatted book summary (`book_summary.json`) → glossary (optional) → few-shot examples (`fewshot_examples.txt`) → sliding context → chunk to translate
- Cost note: summary+few-shot usually add ~500-800 tokens per translator sub-agent, but improve coherence and lexical consistency
- Consistency flow (optional): run `validate_consistency.py` after merge to inspect glossary consistency and empty/untranslated lines; if issues exist, one correction sub-agent patches only listed `Txxxx` lines, then rerun merge/build
- PDF routing flow: in `--pdf-engine auto`, route PDFs to Marker when `marker_single` is available, else warn and fall back to Calibre; keep Poppler heuristic classification only for informational logs
- SVG flow: never segment text under `<svg>`; preserve referenced `.svg` assets; in Marker PDF flow, optionally extract page SVGs and replace Marker PNG images only when page-level mapping is unambiguous
- Footnote flow: detect call/body pairs in HTML, annotate linked note segments in `segments.json` via object values (`text`, `footnote_for`), and keep linked pairs in the same `chunk*.txt` (chunk may exceed `--chunk-size` when required)
- Dedup flow: build `dedup_map.json` after extraction (exact text match after strip + same `footnote_for` context), write only canonical segment ids to chunks, then expand alias translations in merge/consistency stages
- Publish to both GitHub (`git push`) and ClawHub (`clawhub publish ./ --version <semver>`) on release

## Do not

- Do not reintroduce `page*` file support — it was intentionally removed
- Do not hardcode `~/.claude/skills/` paths in SKILL.md — use `{baseDir}`
- Do not put platform-specific tool names (Agent, sessions_spawn) in `allowed-tools` as the only option — keep the whitelist cross-platform
- Do not add mtime-based incremental rebuild for HTML/format generation — the current skip logic is intentionally simple (existence check). Metadata/template changes require manual cleanup. This is documented in the README.
