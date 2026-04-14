# Rainman Translate Book

[English](README.md) | 中文

Claude Code Skill，使用并行 subagent 将整本书（PDF/DOCX/EPUB）翻译成任意语言。

> 本项目受 [claude_translater](https://github.com/wizlijun/claude_translater) 启发。原项目以 shell 脚本为入口，配合 Claude CLI 和多个步骤脚本完成分块翻译；本项目则将流程重构为 Claude Code Skill，使用 subagent 按 chunk 并行翻译，并引入 manifest 驱动的完整性校验，将续跑和多格式输出整合为更统一的流水线。由于项目结构和实现方式均与原项目不同，本项目为独立实现，而非 fork。

---

## 工作原理

```
输入文件 (PDF/DOCX/EPUB)
  │
  ▼
prepare.py（确定性预处理）
  │  转换路由（PDF auto 优先 Marker、marker/calibre、SVG 保留）
  │  拆分结构与内容 → skeleton.html + segments.json
  │  内置 dedup → 有别名时写 dedup_map.json
  │  术语候选提取 → glossary_candidates.txt
  │  摘要/ few-shot 提示生成 → summary_prompt.txt + fewshot_prompt.txt + source_lang.txt
  │  chunk0001.txt, chunk0002.txt, …（仅 canonical 行）
  │  manifest.json 记录各 chunk 文件 hash
  │  pipeline_state.json（给 SKILL.md 的已解析事实契约）
  ▼
术语表：仅当 pipeline_state.glossary_needed=true 时启动单个 subagent
  ▼
图书摘要：按条件执行（pipeline_state.summary_needed）→ book_summary.json
  ▼
Few-shot 示例：按条件执行（pipeline_state.fewshot_enabled）→ fewshot_examples.txt
  ▼
风格检测：仅当 pipeline_state.style_detection_needed=true 时启动单个 subagent
  ▼
并行 subagent 翻译（并发可配置，受 profile 驱动）
  │  每个 subagent：若存在则注入术语表 → 读取 1 个 chunk*.txt → 翻译 → 写入 output_chunk*.txt
  │  分批执行，控制 API 速率
  ▼
编排器校验（行数、段 id、manifest 输出非空等）
  │
  ▼
merge_and_build.py：解析 output_chunk*.txt → 对照 segments.json 校验
  │  回填 skeleton.html → 组装 book.html（head.html + body）
  ▼
validate_consistency.py（可选）：构建 segments_translated.json
  │  检测术语不一致 / 未翻译 / 空译文
  │  单个修正 subagent（仅改目标行）→ 回写 output_chunk*.txt → 重建
  ▼
Calibre ebook-convert → book.epub / book.docx / book.pdf
```

每个 chunk 由独立的 subagent 翻译，拥有全新的上下文窗口。这避免了单次会话翻译整本书时的上下文堆积和输出截断问题。

## 功能特性

- **并行 subagent** — profile 驱动并发（full 默认 8，local-lite 默认 1），各自独立上下文
- **可续跑** — chunk 级续跑，重新运行时自动跳过已翻译的 chunk（元数据或资源变更建议全新运行）
- **Manifest 校验** — 对源 chunk 文件做 SHA-256 追踪，避免信任过时输出
- **多格式输出** — `book.html` 以及经 Calibre 生成的 DOCX、EPUB、PDF
- **多语言** — zh、en、ja、ko、fr、de、es（可扩展）
- **多格式输入** — PDF/DOCX/EPUB，Calibre 负责格式转换

## 前置要求

- **Claude Code CLI** — 已安装并完成认证
- **Calibre** — `ebook-convert` 命令可用（[下载](https://calibre-ebook.com/)）
- **Python 3**，并安装 **beautifulsoup4** — `convert.py` 与 `merge_and_build.py` 必需（`pip install beautifulsoup4`）
- **可选（推荐用于 PDF）: Poppler** — 提供 `pdfinfo`、`pdftotext`，用于日志中的 PDF 结构信息分类
- **可选（推荐用于 PDF）: marker-pdf** — 提供 `marker_single`；在 `--pdf-engine auto` 下，PDF 可用时默认走 Marker
- **可选（推荐用于 PDF+Marker 的 SVG 保留）: `pdf2svg` 或 `mutool`** — 在 Marker PNG 映射前尝试提取矢量图

## 快速开始

### 1. 安装 Skill

**方式 A：npx（推荐）**

```bash
npx skills add deusyu/translate-book -a claude-code -g
```

**方式 B：ClawHub**

```bash
clawhub install translate-book
```

**方式 C：Git 克隆**

```bash
git clone https://github.com/deusyu/translate-book.git ~/.claude/skills/translate-book
```


### 2. 翻译一本书

在 Claude Code 中直接说：

```
translate /path/to/book.pdf to Chinese --style literary
```

或使用斜杠命令：

```
/translate-book translate /path/to/book.pdf to Japanese
```

Skill 自动处理完整流程 — 转换、分块、术语表（按需）、可选摘要/few-shot/风格检测、并行翻译、校验、合并、可选一致性复核、生成所有输出格式。

### 3. 查看输出

所有文件在 `{book_name}_temp/` 目录下：

| 文件 | 说明 |
|------|------|
| `book.html` | 组装后的完整 HTML（head + 译后正文） |
| `book.docx` | Word 文档 |
| `book.epub` | 电子书 |
| `book.pdf` | 可打印 PDF |

## 流程详解

### 第一步：准备（单一确定性入口）

```bash
python3 scripts/prepare.py /path/to/book.pdf --olang zh --chunk-size 6000 --style auto --llm-profile full --pdf-engine auto --preserve-svg auto --num-samples 5
```

`prepare.py` 是默认预处理入口，先在 Python 中解析所有确定性分支，再进入 LLM 阶段。它会完成：

- 转换路由（`--pdf-engine auto|calibre|marker`），其中 PDF 的 auto 模式优先 Marker
- SVG 提取/保留（`--preserve-svg auto|always|never`）
- 生成 `skeleton.html` + `segments.json`
- dedup（同文本 + 同 `footnote_for` 上下文），仅在有别名时写 `dedup_map.json`
- canonical-only chunk 切分（保持脚注关联）
- 术语候选提取（`--min-freq`、`--max-terms`）
- 通过 `summarize.py` 生成摘要/few-shot 提示（`--num-samples`，默认 5）
- 生成 manifest/config，以及 `pipeline_state.json`

`pipeline_state.json` 包含已解析事实，例如：

- `temp_dir`、`input_file`、`target_lang`
- `total_chunks`、`total_segments`、`dedup_segments_skipped`
- `glossary_candidates_count`、`glossary_needed`
- `source_lang`、`summary_needed`、`fewshot_samples_count`
- `style`、`style_detection_needed`
- `llm_profile`、`summary_mode`、`fewshot_enabled`
- `recommended_concurrency`、`sliding_context_before_lines`、`sliding_context_after_lines`
- `consistency_post_validation_enabled`、`translator_prompt_mode`
- `conversion_method`、`svg_extracted`
- `footnote_pairs`、`chunks_with_footnotes`

`convert.py` 仍保留为可独立调用的模块/CLI，但正常流程使用 `prepare.py`。

PDF 相关行为：

- **非 PDF** 输入：仍然使用 Calibre，行为不变。
- **PDF + auto**：路由不再依赖启发式判断：
  - 若 `marker_single` 可用，直接走 Marker；
  - 若 `marker_single` 不可用，回退到 Calibre，并给出 warning：
    `marker-pdf non installé, utilisation de Calibre pour le PDF. Installer marker-pdf est recommandé pour une meilleure extraction.`
- 在完成引擎选择后，仍会用轻量启发式（Poppler 的 `pdfinfo` + `pdftotext -layout` 前 5 页）输出结构信息日志（仅信息展示，不影响路由）：
  - 大比例深缩进行（多栏版面代理特征）；
  - 页尾区域重复出现短编号行（脚注代理特征）；
  - 跨页重复的页眉/页脚行。
- SVG 处理：
  - 内联 `<svg>...</svg>` 内容会从分段提取中排除，SVG 内文本标签不会被替换为 `{{Txxxx}}`。
  - 引用的 `.svg` 资源会原样复制到 `assets/`，并与其它资源一样重写 URL。
  - Marker 路径下，`--preserve-svg auto` 会尝试提取按页 SVG（优先 `pdf2svg`，其次 `mutool`），仅在“页内候选图片唯一”时才把 Marker PNG `<img>` 替换为 SVG。
  - `--preserve-svg always` 在工具缺失或提取失败时直接报错；`--preserve-svg never` 保持当前 PNG 行为。

预处理结果会输出 `pipeline_state.json`，`SKILL.md` 仅依据该文件做简单 yes/no 决策。

脚注相关增强：

- 转换器会识别脚注“引用 ↔ 注释正文”配对（例如 `<sup><a href="#fn1">...</a></sup>` 指向 `id="fn1"`）。
- `segments.json` 保持向后兼容：普通段仍是字符串；被识别为脚注正文且存在关联时，可能写成对象，如 `{"text":"...", "footnote_for":"T0015"}`。
- `chunk*.txt` 开头可能出现上下文注释行（如 `# NOTE: T0042 is footnote for T0015`）；翻译 subagent 需忽略 `#` 行，且不要输出这些注释。
- 为保证上下文完整，脚注正文段会强制放入与其引用段相同的 chunk；因此个别 chunk 可能有意超过 `--chunk-size`。

**`config.txt`** 保存元数据（含输出语言）；**`manifest.json`** 记录 `segments.json`、`skeleton.html` 与各 chunk 文件的 hash。

### 第二步：术语表（简单条件）

读取 `pipeline_state.json`：

- 若 `glossary_needed=true`，启动一个术语表 subagent 生成 `glossary.json`
- 否则跳过

### 第三步：图书摘要（条件执行）

- 若 `summary_needed=true`：读取 `summary_prompt.txt`、启动一个 subagent、写入 `book_summary.json`
- 若 `summary_mode=mini`：使用超短摘要格式（最多 2 句）
- 若 `summary_needed=false`：跳过

### 第四步：Few-shot 示例（条件执行）

- 若 `fewshot_enabled=true`：读取 `fewshot_prompt.txt`，可选注入 `book_summary.json`，启动一个 subagent，写入 `fewshot_examples.txt`
- 若 `fewshot_enabled=false`：跳过

### 第五步：风格检测（简单条件）

读取 `pipeline_state.json`：

- 若 `style_detection_needed=true`，启动一个风格检测 subagent
- 否则直接使用 `pipeline_state.json.style`

- `prepare.py` 的 `--style` 支持 `formal`、`literary`、`technical`、`conversational`、`auto`（默认）
- 检测器读取最前面的源 chunk（存在时读取 `chunk0001.txt`、`chunk0002.txt`、`chunk0003.txt`），并且只返回一个词：`formal`、`literary`、`technical` 或 `conversational`。
- 最终风格会注入每个翻译 subagent 的 system prompt。

翻译 prompt 中四种风格的含义：

- `formal`：正式、庄重的书面语风格
- `literary`：文学化表达，保留节奏与修辞
- `technical`：技术型精确表达，强调清晰与术语准确
- `conversational`：自然口语化、接近日常对话

### 第六步：翻译（并行 subagent）

Skill 分批启动 subagent（默认读取 `pipeline_state.recommended_concurrency`）。每个 subagent 的提示按以下顺序组装：

1. 风格指令（解析后的 style）
2. `book_summary.json`（仅在启用时注入）
3. 术语表（仅在 `glossary.json` 存在时注入）
4. `fewshot_examples.txt`（仅在启用时注入）
5. 滑动上下文（前/后），由 `sliding_context_before_lines` / `sliding_context_after_lines` 控制
6. 待翻译 chunk

如果运行中断，重新运行会跳过已有合法输出的 chunk。翻译失败的 chunk 会自动重试一次。

摘要 + few-shot 通常会为每个翻译 subagent 增加约 500-800 tokens。该边际成本会随 chunk 数增长，但对全书一致性与词汇选择准确性有明显提升。

### 第七步：合并与构建

```bash
python3 scripts/merge_and_build.py --temp-dir book_temp --title "《译后书名》" --olang zh
```

脚本仅支持 **`--temp-dir`**（必填）、**`--title`**（可选）、**`--olang`**（可选；HTML `lang`，省略时从 `config.txt` 读取）。

脚本会：

- 解析全部 `output_chunk*.txt`；若存在 `dedup_map.json`，先用 canonical 译文补全 alias，再校验 `segments.json` 完整性（`segments.json` 的 value 可为字符串，或含 `text` 字段的对象）
- 替换 `skeleton.html` 中的占位符并写出 **`book.html`**
- 调用 Calibre `ebook-convert` 生成 **`book.epub`**、**`book.docx`**、**`book.pdf`**（含 SVG 友好 CSS；EPUB 额外使用 `--preserve-cover-aspect-ratio` 与 `--no-svg-cover`）

### 第八步：可选一致性后校验

```bash
python3 scripts/validate_consistency.py --temp-dir book_temp --olang zh
```

该步骤默认在 `full` profile 启用，在 `local-lite` profile 默认关闭。它只做检测，不直接修正。会解析全部 `output_chunk*.txt`（含反转义），若存在 `dedup_map.json` 则补全 alias 译文，然后写出：

- **`segments_translated.json`** — 完整 `Txxxx -> 译文` 映射
- **`consistency_report.txt`** — 术语表违规、未翻译段、空译文

若无问题，报告内容固定为：

```text
No issues found.
```

若存在问题，编排器可启动一个定点修正 subagent，仅回写报告列出的 `Txxxx` 行到 `output_chunk*.txt`，然后再次运行 `merge_and_build.py` 重建 `book.html` 和各格式输出。

此步骤为可选；当更重视 token 成本而非最高一致性质量时，可以关闭。

## Local LLM / llama.cpp profile

当你在本地 32k 上下文模型运行（例如 Gemma 4 26B A4B IQ4_XS + llama.cpp/llama-server）时，推荐使用 `local-lite`。

推荐基线：

- `--llm-profile local-lite`
- `--chunk-size 3000` 到 `4500`
- `--concurrency 1`（稳定后可试 `2`）
- `--summary-mode off`（若连贯性不足可改 `mini`）
- `--fewshot off`
- `--style technical` 且 `--style-detection off`
- `--sliding-context-lines 0`（叙事连续性不足时可设 `1`）
- `--consistency-post-validation off`

本地示例命令：

```bash
python3 scripts/prepare.py /path/to/book.pdf --olang fr --llm-profile local-lite --chunk-size 3500 --style technical --style-detection off --summary-mode off --fewshot off --sliding-context-lines 0 --concurrency 1 --consistency-post-validation off
```

**注意：** `{book_name}_temp/` 是单次翻译运行的工作目录。若修改标题、输出语言或图片资源，建议使用新的 temp 目录，或先删除已有成品（`book.html`、`book.docx`、`book.epub`、`book.pdf`）再重跑合并。

## 项目结构

| 文件 / 产物 | 用途 |
|-------------|------|
| `SKILL.md` | Claude Code Skill 定义 — 编排完整流程 |
| `scripts/prepare.py` | 单一确定性预处理入口（转换、dedup、术语候选、chunk、manifest、`pipeline_state.json`） |
| `scripts/summarize.py` | 基于 chunk 生成摘要/few-shot 提示，并检测源语言 |
| `scripts/convert.py` | 可导入转换原语与独立转换 CLI |
| `scripts/glossary.py` | 可导入术语候选提取与独立 CLI |
| `scripts/manifest.py` | Chunk manifest：SHA-256 追踪与合并前校验 |
| `scripts/merge_and_build.py` | 解析译后 chunk → 回填 skeleton → `book.html` → Calibre 导出 |
| `scripts/validate_consistency.py` | 构建 `segments_translated.json` 与一致性报告（术语/未译/空译） |
| `{name}_temp/skeleton.html` | 正文 HTML 与 `{{Txxxx}}` 占位符（不发给 LLM） |
| `{name}_temp/head.html` | 合并进 `book.html` 的 `<head>` 内联片段 |
| `{name}_temp/segments.json` | 源段 id → 原文，或带 `text` + 可选 `footnote_for` 的对象（用于关联脚注） |
| `{name}_temp/dedup_map.json` | 段去重映射（`segment_id -> canonical_segment_id`），chunk 仅包含 canonical id |
| `{name}_temp/config.txt` | 运行元数据（`original_title`、`output_lang`、`style` 等） |
| `{name}_temp/pipeline_state.json` | 预处理已解析事实，供 `SKILL.md` 使用 |
| `{name}_temp/chunk*.txt` | 供 subagent 翻译的 canonical 源行文件（关联脚注时可能包含以 `#` 开头的上下文注释） |
| `{name}_temp/glossary_candidates.txt` | 供术语表 subagent 的启发式候选列表（可为空） |
| `{name}_temp/glossary.json` | 可选的扁平术语映射，用于翻译提示（仅在有候选时生成） |
| `{name}_temp/source_lang.txt` | 在摘要提示准备阶段检测得到的源语言 |
| `{name}_temp/summary_prompt.txt` | 用于摘要 subagent 的提示 |
| `{name}_temp/book_summary.json` | 摘要 subagent 产出的结构化图书摘要 |
| `{name}_temp/fewshot_prompt.txt` | few-shot 示例生成提示模板 |
| `{name}_temp/fewshot_examples.txt` | 注入翻译 subagent 的 few-shot 示例 |
| `{name}_temp/output_chunk*.txt` | subagent 写回的译后行文件 |
| `{name}_temp/segments_translated.json` | 从 output chunk 重建的完整译文段映射 |
| `{name}_temp/consistency_report.txt` | 可选后校验报告（无问题时为 `No issues found.`） |

## 常见问题

| 问题 | 解决方案 |
|------|----------|
| `Calibre ebook-convert not found` | 安装 Calibre，确保 `ebook-convert` 在 PATH 中 |
| `No module named 'bs4'` | 执行 `pip install beautifulsoup4` |
| `Manifest validation failed` | 预处理后源 chunk 或 `segments.json` 被改动 — 重新运行 `prepare.py` |
| `Missing source chunk` | 源 chunk 被删除 — 重新运行 `prepare.py` 重新生成 |
| 翻译不完整 | 重新运行 Skill，会从中断处继续 |
| 合并提示缺少 segment id | 查看 temp 目录下的 `missing_segments.txt`，修复或重新生成对应的 `output_chunk*.txt` |
| 修改标题或资源后输出未更新 | 删除 temp 目录中的 `book.html`、`book.docx`、`book.epub`、`book.pdf`，然后重跑 `merge_and_build.py` |
| PDF 生成失败 | 确认 Calibre 已安装且支持 PDF 输出 |
| auto 模式下 PDF 转换质量差 | 安装 `marker-pdf` 使 `marker_single` 可用（auto 会优先 Marker），或直接强制 `--pdf-engine marker` |
| 出现 `marker-pdf non installé, utilisation de Calibre pour le PDF...` 警告 | 安装 `marker-pdf` 让 auto 模式对 PDF 使用 Marker；若可接受质量下降，可强制 `--pdf-engine calibre` |
| 输出里缺少 SVG | 使用 `--pdf-engine marker --preserve-svg auto`，并安装 `pdf2svg`（优先）或 `mutool`；可用 `--preserve-svg always` 在缺少工具时快速失败 |
| 图像被栅格化而非矢量 | 保持源 SVG 在 `assets/` 中，不要手动转 PNG，并使用 `merge_and_build.py` 默认参数（EPUB 会自动附加 SVG 相关选项） |

## Star History

如果这个项目对您有帮助，请考虑为其点亮一颗 Star ⭐！

[![Star History Chart](https://api.star-history.com/svg?repos=deusyu/translate-book&type=Date)](https://star-history.com/#deusyu/translate-book&Date)

## 赞助

如果这个项目帮你节省了时间，欢迎赞助支持后续维护和改进。

[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-pink?logo=github)](https://github.com/sponsors/deusyu)

## License

[MIT](LICENSE)
