# Rainman Translate Book

English | [中文](README.zh-CN.md)

Claude Code skill that translates entire books (PDF/DOCX/EPUB) into any language using parallel subagents.

> Inspired by [claude_translater](https://github.com/wizlijun/claude_translater). The original project uses shell scripts as its entry point, coordinating the Claude CLI with multiple step scripts to perform chunked translation. This project restructures the workflow as a Claude Code Skill, using subagents to translate chunks in parallel, with manifest-driven integrity checks, resumable runs, and multi-format output unified into a single pipeline. As the project structure and implementation differ significantly from the original, this is an independent project rather than a fork.

---

## How It Works

```
Input (PDF/DOCX/EPUB)
  │
  ▼
prepare.py (deterministic preprocessing)
  │  conversion routing (PDF marker-first auto, marker/calibre, SVG preserve)
  │  split structure vs content → skeleton.html + segments.json
  │  in-script dedup → dedup_map.json when aliases exist
  │  glossary candidate extraction → glossary_candidates.txt
  │  summary/few-shot prompt prep → summary_prompt.txt + fewshot_prompt.txt + source_lang.txt
  │  chunk0001.txt, chunk0002.txt, … (canonical-only lines)
  │  manifest.json tracks chunk file hashes
  │  pipeline_state.json = resolved facts contract for SKILL.md
  ▼
Glossary: optional one subagent (only if pipeline_state.glossary_needed)
  ▼
Book summary: conditional (pipeline_state.summary_needed) → book_summary.json
  ▼
Few-shot examples: conditional (pipeline_state.fewshot_enabled) → fewshot_examples.txt
  ▼
Style detection: optional one subagent (only if pipeline_state.style_detection_needed)
  ▼
Parallel subagents (configurable, profile-driven)
  │  each subagent: glossary injected if present → read 1 chunk*.txt → translate → output_chunk*.txt
  │  batched to respect API rate limits
  ▼
Orchestrator validation (line counts, segment ids, manifest non-empty outputs)
  │
  ▼
merge_and_build.py: parse output_chunk*.txt → validate vs segments.json
  │  reinject into skeleton.html → assemble book.html (head.html + body)
  ▼
validate_consistency.py (optional): build segments_translated.json
  │  detect glossary violations / untranslated / empty segments
  │  one fix subagent (targeted lines only) → patch output_chunk*.txt → rebuild
  ▼
Calibre ebook-convert → book.epub / book.docx / book.pdf
```

Each chunk gets its own independent subagent with a fresh context window. This prevents context accumulation and output truncation that happen when translating a full book in a single session.

## Features

- **Parallel subagents** — profile-driven concurrency (full: 8 default, local-lite: 1 default), each with isolated context
- **Resumable** — chunk-level resume; already-translated chunks are skipped on re-run (for metadata or asset changes, use a fresh run)
- **Manifest validation** — SHA-256 hash tracking on source chunk files prevents stale outputs from being trusted before merge
- **Multi-format output** — `book.html`, DOCX, EPUB, PDF via Calibre
- **Multi-language** — zh, en, ja, ko, fr, de, es (extensible)
- **PDF/DOCX/EPUB input** — Calibre handles the conversion heavy lifting

## Prerequisites

- **Claude Code CLI** — installed and authenticated
- **Calibre** — `ebook-convert` command must be available ([download](https://calibre-ebook.com/))
- **Python 3** with **beautifulsoup4** — required for `convert.py` and `merge_and_build.py` (`pip install beautifulsoup4`)
- **Optional (recommended for PDFs): Poppler** — `pdfinfo` and `pdftotext` provide informational PDF structure classification in logs
- **Optional (recommended for PDFs): marker-pdf** — provides `marker_single`; with `--pdf-engine auto`, PDFs use Marker by default when available
- **Optional (recommended for SVG preservation in PDF+Marker): `pdf2svg` or `mutool`** — enables vector extraction before Marker PNG mapping

## Quick Start

### 1. Install the skill

**Option A: npx (recommended)**

```bash
npx skills add deusyu/translate-book -a claude-code -g
```

**Option B: ClawHub**

```bash
clawhub install translate-book
```

**Option C: Git clone**

```bash
git clone https://github.com/deusyu/translate-book.git ~/.claude/skills/translate-book
```


### 2. Translate a book

In Claude Code, say:

```
translate /path/to/book.pdf to Chinese --style literary
```

Or use the slash command:

```
/translate-book translate /path/to/book.pdf to Japanese
```

The skill handles the full pipeline automatically — convert, chunk, glossary (when needed), optional summary/few-shot/style detection, parallel translation, validation, merge, optional consistency post-validation, and output builds.

### 3. Find your outputs

All files are in `{book_name}_temp/`:

| File | Description |
|------|-------------|
| `book.html` | Assembled full HTML (head + translated body) |
| `book.docx` | Word document |
| `book.epub` | E-book |
| `book.pdf` | Print-ready PDF |

## Pipeline Details

### Step 1: Prepare (single deterministic entrypoint)

```bash
python3 scripts/prepare.py /path/to/book.pdf --olang zh --chunk-size 6000 --style auto --llm-profile full --pdf-engine auto --preserve-svg auto --num-samples 5
```

`prepare.py` is the default orchestration entrypoint for preprocessing. It resolves all deterministic decisions before any LLM step:

- conversion routing (`--pdf-engine auto|calibre|marker`) with Marker-first PDF auto mode
- optional SVG extraction/preservation (`--preserve-svg auto|always|never`)
- extraction to `skeleton.html` + `segments.json`
- dedup grouping (same stripped text + same `footnote_for` context), writing `dedup_map.json` only when aliases exist
- canonical-only chunking with linked-footnote constraints
- glossary candidate extraction (`--min-freq`, `--max-terms`)
- summary/few-shot prompt preparation via `summarize.py` (`--num-samples`, default 5)
- manifest and config generation
- `pipeline_state.json` generation (resolved facts only, no ambiguity)

`pipeline_state.json` fields include:

- `temp_dir`, `input_file`, `target_lang`
- `total_chunks`, `total_segments`, `dedup_segments_skipped`
- `glossary_candidates_count`, `glossary_needed`
- `source_lang`, `summary_needed`, `fewshot_samples_count`
- `style`, `style_detection_needed`
- `llm_profile`, `summary_mode`, `fewshot_enabled`
- `recommended_concurrency`, `sliding_context_before_lines`, `sliding_context_after_lines`
- `consistency_post_validation_enabled`, `translator_prompt_mode`
- `conversion_method`, `svg_extracted`
- `footnote_pairs`, `chunks_with_footnotes`

`convert.py` remains available as a standalone module/CLI for isolated conversion work, but normal skill flow uses `prepare.py`.

PDF behavior:

- For **non-PDF** input, Calibre is used exactly as before.
- For **PDF + auto**, routing no longer depends on heuristics:
  - if `marker_single` is available, route to Marker,
  - if `marker_single` is missing, fall back to Calibre with warning:
    `marker-pdf non installé, utilisation de Calibre pour le PDF. Installer marker-pdf est recommandé pour une meilleure extraction.`
- After engine selection, a lightweight heuristic still analyzes structure using Poppler (`pdfinfo`, `pdftotext -layout` on first pages) for informational logs only:
  - high ratio of strongly-indented lines (proxy for multi-column layout),
  - recurrent footnote-like short numbered lines near page bottoms,
  - repeated header/footer lines across pages.
- SVG handling:
  - Inline `<svg>...</svg>` content is excluded from segment extraction, so SVG XML/text labels are never replaced by `{{Txxxx}}`.
  - Referenced `.svg` assets are copied as-is under `assets/` and URL-rewritten like other resources.
  - In Marker flow, `--preserve-svg auto` tries to extract page-level SVGs (`pdf2svg` first, `mutool` fallback) and replaces Marker PNG `<img>` only when page mapping is unambiguous (single candidate figure on that page).
  - `--preserve-svg always` fails if extraction tools are missing or extraction fails; `--preserve-svg never` keeps current PNG-only behavior.

Output temp directory now includes `pipeline_state.json`, which is the contract consumed by `SKILL.md` to make only simple yes/no branches.

Footnote-aware extraction and chunking:

- The converter detects footnote call ↔ body pairs (e.g. `<sup><a href="#fn1">...</a></sup>` linked to `id="fn1"` blocks).
- `segments.json` remains backward-compatible: regular segments stay string values, while linked footnote body segments may use object values such as `{"text":"...", "footnote_for":"T0015"}`.
- `chunk*.txt` may start with context comments like `# NOTE: T0042 is footnote for T0015`. Translation subagents must ignore `#` lines and must not output them.
- To keep call and note context together, linked footnote segments are forced into the same chunk as their referenced call segment; therefore some chunks can intentionally exceed `--chunk-size`.

**`config.txt`** stores metadata (including output language); **`manifest.json`** records hashes of `segments.json`, `skeleton.html`, and each chunk file.

### Step 2: Glossary (simple conditional)

Read `pipeline_state.json`:

- if `glossary_needed=true`, run one glossary subagent to create `glossary.json`
- otherwise skip glossary generation entirely

### Step 3: Book Summary (conditional)

- if `summary_needed=true`, read `summary_prompt.txt`, run one subagent, write `book_summary.json`
- if `summary_mode=mini`, use a compact 2-sentence summary format
- if `summary_needed=false`, skip this step

### Step 4: Few-shot Examples (conditional)

- if `fewshot_enabled=true`, read `fewshot_prompt.txt`, optionally inject `book_summary.json`, run one subagent, write `fewshot_examples.txt`
- if `fewshot_enabled=false`, skip this step

### Step 5: Style Detection (simple conditional)

Read `pipeline_state.json`:

- if `style_detection_needed=true`, run one style-detection subagent
- otherwise use `style` from `pipeline_state.json` directly

- `prepare.py` accepts `--style` with five values: `formal`, `literary`, `technical`, `conversational`, `auto` (default)
- The detector reads the first source chunks (`chunk0001.txt`, `chunk0002.txt`, `chunk0003.txt` when present) and returns exactly one word: `formal`, `literary`, `technical`, or `conversational`.
- The chosen value is then injected into each translator subagent system prompt.

Register guidance used by translator prompts:

- `formal`: formal, polished register
- `literary`: literary register preserving rhythm and stylistic devices
- `technical`: precise technical register prioritizing clarity and terminology accuracy
- `conversational`: natural everyday conversational register

### Step 6: Translate (parallel subagents)

The skill launches subagents in batches (default from `pipeline_state.recommended_concurrency`). Each subagent prompt is assembled in this order:

1. Register instruction (resolved style)
2. Book summary from `book_summary.json` (only when enabled)
3. Glossary block (only when `glossary.json` exists)
4. Few-shot examples from `fewshot_examples.txt` (only when enabled)
5. Sliding context (before/after) using `sliding_context_before_lines` and `sliding_context_after_lines`
6. Source chunk to translate

Then it writes `output_chunkNNNN.txt` with the same line count and `Txxxx:` prefixes as the source.

If a run is interrupted, re-running skips chunks that already have valid output files. Failed chunks are retried once automatically.

Summary + few-shot add roughly 500-800 tokens per translator subagent. This marginal cost scales with chunk count, but significantly improves cross-chunk coherence and lexical choices.

### Step 7: Merge & Build

```bash
python3 scripts/merge_and_build.py --temp-dir book_temp --title "《translated title》" --olang zh
```

Arguments supported by the script are **`--temp-dir`** (required), **`--title`** (optional), and **`--olang`** (optional; HTML `lang`, defaulting from `config.txt` if omitted).

The script:

- Parses all `output_chunk*.txt` files; when `dedup_map.json` exists, aliases are filled from canonical translations before checking completeness against `segments.json` (`segments.json` values can be plain strings or objects containing `text`)
- Replaces placeholders in `skeleton.html` and writes **`book.html`**
- Runs Calibre `ebook-convert` to produce **`book.epub`**, **`book.docx`**, and **`book.pdf`** (with SVG-friendly CSS; EPUB also uses `--preserve-cover-aspect-ratio` and `--no-svg-cover`)

### Step 8: Optional Consistency Post-Validation

```bash
python3 scripts/validate_consistency.py --temp-dir book_temp --olang zh
```

This detector-only step is typically enabled in `full` profile and disabled in `local-lite` profile by default. It parses every `output_chunk*.txt`, unescapes payloads, expands aliases from `dedup_map.json` when present, and writes:

- **`segments_translated.json`** — full `Txxxx -> translated text` map
- **`consistency_report.txt`** — glossary violations, untranslated segments, and empty translations

If no issue is found, the report is exactly:

```text
No issues found.
```

If issues exist, the orchestrator can run one targeted correction subagent, update only listed `Txxxx` lines in `output_chunk*.txt`, and run `merge_and_build.py` again to rebuild `book.html` and output formats.

This stage is optional and can be disabled when token savings are more important than maximum terminology consistency.

## Local LLM / llama.cpp profile

Use `local-lite` when running on a local 32k-context model (for example Gemma 4 26B A4B IQ4_XS on llama.cpp/llama-server).

Recommended baseline:

- `--llm-profile local-lite`
- `--chunk-size 3000` to `4500`
- `--concurrency 1` (or `2` if stable)
- `--summary-mode off` (or `mini` when coherence drops)
- `--fewshot off`
- `--style technical` and `--style-detection off`
- `--sliding-context-lines 0` (or `1` for difficult narrative continuity)
- `--consistency-post-validation off`

Example local command:

```bash
python3 scripts/prepare.py /path/to/book.pdf --olang fr --llm-profile local-lite --chunk-size 3500 --style technical --style-detection off --summary-mode off --fewshot off --sliding-context-lines 0 --concurrency 1 --consistency-post-validation off
```

Then run the skill using the same effective settings from `pipeline_state.json` for orchestration.

**Note:** `{book_name}_temp/` is a working directory for a single translation run. If you change the title, output language, or image assets, either use a fresh temp directory or delete the existing final artifacts (`book.html`, `book.docx`, `book.epub`, `book.pdf`) before re-running merge.

## Project Structure

| File / artifact | Purpose |
|-----------------|---------|
| `SKILL.md` | Claude Code skill definition — orchestrates the full pipeline |
| `scripts/prepare.py` | Single deterministic preprocessing entrypoint (conversion, dedup, glossary candidates, chunking, manifest, `pipeline_state.json`) |
| `scripts/summarize.py` | Builds summary/few-shot prompts and detects source language from extracted chunks |
| `scripts/convert.py` | Importable conversion primitives and standalone conversion CLI |
| `scripts/glossary.py` | Importable glossary candidate extraction and standalone CLI |
| `scripts/manifest.py` | Chunk manifest: SHA-256 tracking and pre-merge checks |
| `scripts/merge_and_build.py` | Parse translated chunks → reinject skeleton → `book.html` → Calibre exports |
| `scripts/validate_consistency.py` | Build `segments_translated.json` + consistency report (glossary/untranslated/empty) |
| `{name}_temp/skeleton.html` | Body HTML with `{{Txxxx}}` placeholders (never sent to an LLM) |
| `{name}_temp/head.html` | `<head>` inner fragment merged into `book.html` |
| `{name}_temp/segments.json` | Source segment id → original text, or object with `text` + optional `footnote_for` for linked notes |
| `{name}_temp/dedup_map.json` | Segment dedup map (`segment_id -> canonical_segment_id`); chunks include canonical ids only |
| `{name}_temp/config.txt` | Run metadata (`original_title`, `output_lang`, `style`, …) |
| `{name}_temp/pipeline_state.json` | Resolved preprocessing facts consumed by `SKILL.md` |
| `{name}_temp/chunk*.txt` | Per-chunk canonical source lines for subagents (`#` comment lines may appear for linked footnote context) |
| `{name}_temp/glossary_candidates.txt` | Heuristic term list for the glossary sub-agent (may be empty) |
| `{name}_temp/glossary.json` | Optional flat term map for translation prompts (only when candidates exist) |
| `{name}_temp/source_lang.txt` | Source language guessed during summarization prompt preparation |
| `{name}_temp/summary_prompt.txt` | Prompt used by the summary subagent |
| `{name}_temp/book_summary.json` | Structured summary generated by the summary subagent |
| `{name}_temp/fewshot_prompt.txt` | Prompt template for few-shot example generation |
| `{name}_temp/fewshot_examples.txt` | Few-shot examples injected into translator prompts |
| `{name}_temp/output_chunk*.txt` | Per-chunk translated lines from subagents |
| `{name}_temp/segments_translated.json` | Full translated segment map rebuilt from output chunks |
| `{name}_temp/consistency_report.txt` | Optional post-validation findings (`No issues found.` when clean) |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Calibre ebook-convert not found` | Install Calibre and ensure `ebook-convert` is in PATH |
| `No module named 'bs4'` | `pip install beautifulsoup4` |
| `Manifest validation failed` | Source chunks or `segments.json` changed since preprocessing — re-run `prepare.py` |
| `Missing source chunk` | Source chunk file deleted — re-run `prepare.py` to regenerate |
| Incomplete translation | Re-run the skill — it resumes from where it stopped |
| Merge reports missing segment ids | Check `missing_segments.txt` in the temp dir; fix or regenerate the listed `output_chunk*.txt` files |
| Changed title or assets but outputs did not update | Delete `book.html`, `book.docx`, `book.epub`, `book.pdf` in the temp dir, then re-run `merge_and_build.py` |
| PDF generation fails | Ensure Calibre is installed with PDF output support |
| PDF converts poorly in auto mode | Install `marker-pdf` so `marker_single` is available (auto will prefer Marker), or force `--pdf-engine marker` |
| `marker-pdf non installé, utilisation de Calibre pour le PDF...` warning | Install `marker-pdf` so auto mode uses Marker for PDF, or force `--pdf-engine calibre` if you accept lower extraction quality |
| SVGs missing in output | Run with `--pdf-engine marker --preserve-svg auto` and install `pdf2svg` (preferred) or `mutool`; use `--preserve-svg always` to fail fast when SVG extraction is unavailable |
| Figures become rasterized instead of vector | Keep source SVGs in `assets/`, avoid manual raster conversion, and ensure Calibre build runs with default pipeline options from `merge_and_build.py` (EPUB adds SVG-preserving flags) |

## Star History

If you find this project helpful, please consider giving it a Star ⭐!

[![Star History Chart](https://api.star-history.com/svg?repos=deusyu/translate-book&type=Date)](https://star-history.com/#deusyu/translate-book&Date)

## Sponsor

If this project saves you time, consider sponsoring to keep it maintained and improved.

[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-pink?logo=github)](https://github.com/sponsors/deusyu)

## License

[MIT](LICENSE)
