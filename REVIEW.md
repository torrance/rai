# Full-Project Review: `rai` (Research AI)

## Executive Assessment

`rai` is one of the best-designed local research harnesses for non-fiction writing I have seen. The core philosophy—**"Maps point, sources prove"**, **"You talk; it records"**, deterministic CLI builds vs. permission-locked agent interactions, and strict separation of the writer's prose (`drafts/`) from agent notes (`.rai/`) and outputs (`reports/`)—is coherent, well-enforced, and cleanly implemented in ~4,500 lines of readable Python.

Inspecting both the codebase and the live `projects/utopia-lit-review/` workspace shows the system is already producing serious scholarly value, while also exposing several concrete bugs, staleness blind spots, and workflow bottlenecks that appear in real literature reviews.

---

## Part 1: What to Leave Alone (Strengths to Preserve)

Do **not** re-architect these parts of the system:
1. **The OpenCode permission boundary (`opencode.jsonc` + agent frontmatter)**: Enforcing `edit: deny` on `drafts/**` and `.rai/**` at the OpenCode runtime level (and verifying it via `opencode debug agents` and live tests) is far more trustworthy than prompt rules alone.
2. **The five-stage model architecture (`reading`, `summarising`, `research`, `sparring`, `embedding`) with no implicit fallback**: Explicit stage configuration prevents silent quality degradation or accidental cost spikes.
3. **Append-only JSONL ledger (`.rai/ledger/`) as the sole git-tracked machine state**: Rebuilding cards/maps/DB while preserving human decisions in plain-text JSONL is simple, debuggable, and git-friendly.
4. **Three-tier representation of sources (Blocks/Chunks $\rightarrow$ Unit Readings $\rightarrow$ Cards/Project Notes $\rightarrow$ Corpus Map)**: This scales well and matches how scholars actually work with books vs. articles.
5. **Deterministic location and quote checking (`cards.py:check_unit`, `verify.py`)**: Dropping hallucinated locations and fuzzy-correcting (`near`) or dropping (`not_found`) quotes during card generation is a major reason the cards in `utopia-lit-review` are trustworthy.

---

## Part 2: Definite Bugs & Correctness Risks (Prioritized)

### 1. [HIGH] `open_questions()` flattens nested sub-bullets and picks up bullets outside the intended list (`src/rai/cards.py:176-187`)
* **Evidence from `projects/utopia-lit-review`**:
  In `projects/utopia-lit-review/project.md:17-35`, the writer wrote 5 top-level questions (one with 3 nested sub-bullets) and then a paragraph followed by 4 taxonomy bullets ("The explicit utopias...", "The utopia of fiction...").
  Because `open_questions()` uses `re.findall(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", match.group(1), re.MULTILINE)`, it flattened all of them into **Q1 through Q12** (visible in `projects/utopia-lit-review/.rai/sources/sutterluttiMakeCapitalismHistory2023.md:10-19`, where sub-bullets became `Q6`, `Q7`, `Q8` detached from `Q5: For each utopia:`, and the taxonomy bullets became `Q9`–`Q12`!).
* **Why it matters**: Every card relevance call and corpus map call receives corrupted question numbering and detached sub-questions ("Q6: What political/ethical/philosophical tradition is this writing from?" without knowing it applies *per utopia*).
* **Fix**:
  Parse top-level list items together with any indented continuation lines/sub-bullets as a single question block, and stop or require a heading when non-indented prose paragraphs interrupt the list (or attach indented sub-bullets to their parent `Q{i}`).

### 2. [HIGH] `read_location` miscalculates `shown` blocks when a block contains internal `\n\n` (e.g., Markdown tables) (`src/rai/search.py:211-218`)
* **Where**: `src/rai/search.py:211-212`:
  ```python
  text, truncated = format_blocks(blocks, max_tokens)
  shown = blocks[: len(text.split("\n\n"))]
  ```
* **Bug**: `format_blocks` joins formatted blocks with `"\n\n"`. If any block's `text` contains `"\n\n"` (for instance, a Docling table exported via `export_to_markdown`, or a multi-paragraph block), `len(text.split("\n\n"))` is larger than the number of blocks actually included. If `text` is empty (`blocks == []`), `text.split("\n\n")` is `[""]` (length 1), and `shown[-1]` at line 217 would index into an empty list (guarded only by `if blocks else ""`). Worse, when `truncated=True` and an earlier block had `\n\n`, `shown` includes extra blocks that were truncated out, producing a wrong `citation` page range and wrong `seq:` range.
* **Fix**: Have `format_blocks(blocks, max_tokens)` return `(text, shown_blocks, truncated)` directly instead of re-splitting `text` on `"\n\n"`.

### 3. [HIGH] `delete_source` and citation-key renames leave orphaned card/project-note files and `notes` table rows (`src/rai/store.py:228-237`, `src/rai/ingest.py:225-230, 287-291`)
* **Where**: `store.delete_source(source_id)` deletes from `chunks_fts`, `chunks_vec`, and `sources` (cascading to `blocks`, `chunks`, `units`), but:
  1. It does **not** remove `.rai/cards/<cite_key>.md`, `.rai/cards/<cite_key>.prev.md`, or `.rai/sources/<cite_key>.md`.
  2. It does **not** remove rows in the `notes` table for those paths.
  3. In `ingest.py:_index_file` (lines 78-105), if an existing source's **file content changes** AND its `cite_key` in Zotero changed at the same time, `_refresh_metadata` is skipped (because `existing["file_sha256"] != digest`), `write_source` deletes and re-inserts the source with the new `cite_key`, and the old card/note files under the old `cite_key` are orphaned on disk.
* **Fix**: Clean up `.rai/cards/<old_key>*.md`, `.rai/sources/<old_key>.md`, and `notes` rows when a source is deleted, and run `rename_source_notes` in `_index_file` whenever `existing and existing["cite_key"] != meta["cite_key"]`.

### 4. [HIGH] `PAGE_RE` in `resolve_location` fails on uppercase Roman numerals in the end of a range and alphanumeric page labels like `pp. C1-29` (`src/rai/search.py:92-95, 119-132`)
* **Where**:
  ```python
  PAGE_RE = re.compile(
      r"^(?:pp?\.?\s*)?([\divxlcIVXLC]+)(?:\s*[-\u2013]\s*([\divxlc]+))?$"
  )
  ```
* **Bugs**:
  1. Group 2 (`[\divxlc]+`) omits uppercase `IVXLC`, so `pp. IV-VIII` fails to match.
  2. Real PDFs in `projects/utopia-lit-review` have cover page labels like `C1` (see `sutterluttiMakeCapitalismHistory2023.md:24`: `pp. C1-29`). `PAGE_RE` rejects `C1`, so `_location_ok` drops every claim/concept/passage on cover-labeled pages or multi-range strings (`pp. 5, 91-92`), and `read_location(source, "pp. C1-29")` fails with `"Couldn't find location"`.
  3. If a model cites `pp. 79-81` and page `80` has blocks but page `81` had no text blocks (e.g. a full-page figure), `last` query `WHERE page_label='81'` returns `None`, and line 130 falls back to `first["s"]` (only page 79!) instead of all blocks up to page 81.
* **Fix**:
  - Broaden `PAGE_RE` (or resolve against actual `page_label` values in `blocks` for that `source_id`).
  - When a range `first_label`–`last_label` is given, look up `min(page_index)` for `first_label` and `max(page_index)` for `last_label` (or numeric fallback) and select `WHERE page_index BETWEEN ? AND ?`.
  - Support comma-separated locations (e.g. `pp. 5, 91-92`) by resolving the first or union span.

### 5. [MEDIUM] Card combiner (`card_combine`) output is never location- or quote-checked before writing the card (`src/rai/cards.py:356-366`)
* **Where**: `build_card` runs `check_unit()` on each `reading` stage unit output, then passes all units to `chat_json(..., load_prompt("card_combine"), ..., stage="summarising")`, and immediately renders `combined` to `.rai/cards/<cite_key>.md`.
* **Bug**: The `summarising` model can (and sometimes does) alter a quote slightly, merge two locations into an unresolvable string, or hallucinate a quote when combining units. Because `check_unit` only runs on unit readings and not on `combined`, bad quotes can slip into `.rai/cards/<cite_key>.md` and later fail `rai check`.
* **Fix**: Run a `check_combined_card(store, source["id"], combined)` pass after `card_combine` to verify all `passages` quotes via `verify_quote` (replacing `near` with `actual_wording` and clearing `not_found`).

### 6. [MEDIUM] `card_Combine` prompt can exceed output token limits or context window on very long books (`src/rai/cards.py:357-365`)
* **Evidence from `projects/utopia-lit-review`**:
  In `wrightEnvisioningRealUtopias2010.md`, the book had ~35 units (512 checked locations!). All 35 JSON unit outputs were serialized into a single `card_combine` prompt.
* **Risk**: For a 600–800 page book (40–60 units), `json.dumps(outputs)` can reach 40k–80k tokens of dense JSON, and `card_combine` asks for a single JSON reply. Furthermore, `render_card` expects `s['part']`, `s['locations']`, `s['argument']`, which if containing pipe characters `|` breaks the Markdown table in `## Structure` (`src/rai/cards.py:242`).
* **Fix**:
  - Escape `|` as `/` in `render_card` table cells (`s['part']`, `s['locations']`, `s['argument']`) and in `corpus_map.py:323` (`render_map` Coverage table), just as `claims.py:91` already does (`claim.replace("|", "/")`).
  - Strip empty arrays or verbose fields if unit count > 25, or hierarchical-combine if unit count is huge.

### 7. [MEDIUM] Project notes do not go stale when draft citations change (`src/rai/cards.py:467-482, 513-528`)
* **Where**: `build_project_note` writes `## Cited in drafts` using `.rai/citations.json`, which is written by `build_manuscript`.
* **Bug**:
  1. In `rai refresh` (`src/rai/cli.py:738-749`), `stale_project_notes` runs **before** `build_manuscript` updates `citations.json`.
  2. `stale_project_notes` only checks `card_hash`, `questions_hash`, and `model`—not whether `citations.json` entries for `cite_key` changed. Thus, `## Cited in drafts` in `.rai/sources/<cite_key>.md` remains permanently stale (`- (run rai manuscript to fill this in)` or old counts) unless the source card or open questions also change.
* **Fix**: Include the hash of `citations.json`'s entry for `cite_key` in `project_note`'s `meta` and `stale_project_notes()`, or append `Cited in drafts` dynamically in `read_card()` from `citations.json` instead of baking it into the static file before `manuscript` runs. (Dynamic assembly in `read_card()` is even cleaner and avoids an LLM call when only draft citations changed!)

### 8. [MEDIUM] `corpus_map.py` renders empty `## Gaps\n-` and drops `lineage` in large libraries (>40 cards) (`src/rai/corpus_map.py:94-97, 250-263, 335`)
* **Evidence from `projects/utopia-lit-review`**:
  Look at `projects/utopia-lit-review/.rai/corpus/map.md:69-70`:
  ```markdown
  ## Gaps
  -
  ```
  The model returned `"gaps": []` (even though it wrote the gaps in `overview` and `coverage`), and line 335 rendered `"\n".join(f"- {g}" for g in m.get("gaps", [])) or "-"`, which produces a dangling bullet `-`.
  Additionally, in the `> 40` cards branch (`corpus_map.py:250-260`), the synthesis prompt only passes `Themes` and `_claims_section(c["card"])`, omitting the `## Interlocutors` section of each card, even though `corpus_map.md` instructs: `"lineage": which sources respond to which (from the cards' interlocutors)`.
* **Fix**:
  - In `render_map`, use `or "- (none noted)"` instead of `or "-"`, and add `"minItems": 1` to `gaps` in `MAP_SCHEMA` (or prompt reinforcement).
  - In the `> 40` cards synthesis branch, include `_interlocutors_section(c["card"])` alongside `_claims_section(c["card"])`.

### 9. [MEDIUM] `refresh_chapter` MCP tool allows path traversal outside `drafts/` (`src/rai/server.py:121-128`)
* **Where**:
  ```python
  @mcp.tool()
  def refresh_chapter(path: str) -> str:
      target = (project.root / path).resolve()
      if not target.exists():
          return f"No draft file {path!r}."
      done = build_manuscript(project, only=target, force=True, say=log.info)
  ```
* **Bug**: If an agent passes `path="project.md"` or `path="../something.md"`, `target.exists()` is True, and `map_chapter` will call `chapter_id(path, project)` which calls `path.relative_to(project.drafts_dir)` and raises an unhandled `ValueError` crashing the tool call—or if `target` is a non-draft file inside `drafts/` (e.g. `.gitkeep`), `DRAFT_SUFFIXES[path.suffix.lower()]` raises `KeyError`.
* **Fix**: Use `ledger.draft_rel(project, path)` and verify `target.is_relative_to(project.drafts_dir.resolve())` and `target.suffix.lower() in DRAFT_SUFFIXES` before calling `build_manuscript`.

### 10. [LOW-MEDIUM] `check.py` quote regexes fail on quotes containing internal quotation marks or newlines (`src/rai/check.py:19-22`)
* **Where**:
  - `NOTE_QUOTE = re.compile(r"[\"\u201c]([^\"\u201d]{12,}?)[\"\u201d]\s*\[@([\w:.\-]+)(?:,\s*([^\]]+))?\]")`
  - `CARD_QUOTE = re.compile(r"^- ([^:]+):.*?Quote: \"([^\"]{12,})\"", re.MULTILINE)`
* **Evidence from `projects/utopia-lit-review`**:
  In `wrightEnvisioningRealUtopias2010.md:80`:
  `Quote: "What we need, then, is "real utopias": utopian ideals..."`
  Because `render_card` wraps `p["quote"]` in unescaped `"..."`, any quote containing internal double quotes truncates `CARD_QUOTE` at the first internal `"`, causing `rai check` to check a fragment or miss the quote. And in reports, if an agent writes a multi-citation `[@key1, p. 4; @key2, p. 8]` or puts a period after the closing quote `"quote." [@key, p. 4]`, `NOTE_QUOTE` or `verify_quote` can misfire.
* **Fix**:
  - In `CARD_QUOTE`, match until the end of the line: `r'^- ([^:]+):.*?Quote: "(.*)"$'`.
  - In `verify_quote`, strip trailing sentence punctuation (`.,;:`) on retry if the exact match fails (writers often add or omit a trailing period inside quotes).

### 11. [LOW] `brief.py` line-trimming loop mutates `parts[3]` by hardcoded index (`src/rai/brief.py:120-127`)
* **Where**: `parts[3]` happens to be `"## Draft\n..."` only because `parts.insert(6, latest)` inserts after index 3. If a section is ever added before `Draft`, `parts[3]` will clobber the wrong section. Also, if `draft_lines` is empty and the brief still exceeds `BUDGET_TOKENS` (e.g. a very long `Thesis` in `project.md` or many themes/stale notes), the `while` loop terminates above budget without clipping `thesis` or `stale_notes`.
* **Fix**: Clip `thesis` (e.g. `_clip(thesis, 1200)`) and `stale_notes` when assembling `parts`, and reference the draft section by variable rather than magic index `parts[3]`.

### 12. [LOW] SQLite connection timeout / WAL concurrency when git hook and MCP server run simultaneously (`src/rai/store.py:116`)
* **Where**: `sqlite3.connect(project.db_path)` uses the default 5-second timeout. When a writer commits a draft while an agent or `rai refresh` is running long transactions (`write_source` or `reembed`), concurrent writes can hit `sqlite3.OperationalError: database is locked`.
* **Fix**: Pass `timeout=30.0` to `sqlite3.connect(project.db_path)` and add `PRAGMA busy_timeout = 30000`.

---

## Part 3: High-Value Features Worth Adding for Literature-Heavy Non-Fiction

Looking closely at how `rai` was used in `projects/utopia-lit-review` (where 4 books/chapters were ingested, 87 candidates were harvested into `reading-list.md`, and 3 synthesis reports were written), several concrete gaps emerge between **what the writer needs** and **what the tools currently provide**:

### 1. Structured Reference / Bibliography Harvesting from Library Sources
* **What happened in `utopia-lit-review`**: The writer asked the assistant to pull candidate sources out of Sutterlütti & Meretz (2023). The assistant had to call `read_location` manually across 7 different chapter-end reference blocks (`pp. 37-39, 71-72, 87-88...`) and manually search/record 87 candidates (`C1`–`C87`).
* **Improvement**:
  - During extraction/card-building, detect bibliography/reference sections (or extract inline citations/footnotes) into a `source_references` table or `.rai/cards/<cite_key>.refs.json`.
  - Even simpler: add an MCP tool `extract_citations_from_source(cite_key, location="")` or index reference blocks with `kind="reference"` so the assistant can pull a book's bibliography in one deterministic tool call rather than hunting page by page.

### 2. Cross-Source Concept & Claim Comparison Matrix (`compare_sources` or `search_passages` filtering)
* **Why**: In a literature review or book project, the writer constantly asks: *"How do Wright, Hahnel, and Sutterlütti & Meretz each define social ownership / planning / prefiguration?"*
* **Current limitation**: `search_passages(query, kind="text", limit=8)` searches across the **entire** library globally. If one long book (e.g. Wright at 500 blocks) dominates the top 8 BM25/vector scores for a query, shorter sources (like Kinna's 18-page chapter) are crowded out of the top 8 hits!
* **Improvement**:
  1. **Add `source: str = ""` (or `sources: list[str]`) and `per_source_limit: int = 0` parameters to `search_passages`**:
     - `source="wrightEnvisioningRealUtopias2010"` lets the agent search *within* a single book (essential when checking a specific author's view or finding a passage in a 400-page book).
     - `per_source_limit=2` (or diversity re-ranking) prevents one 400-page book from taking all 8 slots in a library-wide search.

### 3. Candidate-to-Library Promotion Lifecycle
* **What happened in `utopia-lit-review`**:
  In `utopia-lit-review`, `C24` (Wright, *Envisioning Real Utopias*) and `C84` (Hahnel, *Of the People, By the People*) were proposed and shortlisted on the reading list. Later, the writer added them to Zotero and ingested them (`wrightEnvisioningRealUtopias2010`, `hahnelPeoplePeopleCase2012`).
  **However, `reports/reading-list.md` still lists `C24` and `C84` under `## Shortlisted (68)` as unread candidates!**
* **Improvement**:
  - In `ingest.py` / `render_reading_list`, cross-check candidates against `store.sources()` (by normalized DOI, URL, or high-similarity title+author).
  - Automatically mark or group candidates that are now in the library under `## In library` (with their `cite_key`) in `reports/reading-list.md` so the reading list stays an accurate queue of what *still* needs to be acquired/read.

### 4. Zotero Annotations & Highlights Ingestion
* **Why**: The core rule of `rai` is that the writer reads the primary sources themselves. When a writer reads a PDF/EPUB in Zotero 7/8, they highlight passages and write margin notes. Zotero's local API exposes child items with `itemType == "annotation"` (containing `annotationText`, `annotationComment`, `annotationPageLabel`) and `itemType == "note"`.
* **Current state**: `zotero.py:116` explicitly skips `{"attachment", "note", "annotation"}` and never reads the writer's own Zotero highlights/notes.
* **Improvement**:
  - Ingest Zotero annotations and notes for each source into a `writer_annotations` table (or append a `## Your Zotero highlights & notes` section to `.rai/sources/<cite_key>.md`).
  - This creates a direct feedback loop: the assistant directs the writer to read Chapter 4; the writer reads and highlights it in Zotero; `rai refresh` pulls the writer's highlights and margin comments so the assistant and Sparring partner immediately see what the writer found salient or dubious.

### 5. First-Class Table of Contents / Structure Browsing Tool (`list_sources` / `source_outline`)
* **Current limitation**: If an agent wants to know what sources are in the library (titles, authors, years, reading status, page spans) or wants the heading tree of a 350-page book, it only has `corpus_map` (prose summary) or `read_card(cite_key)` (which requires already knowing the exact `cite_key`). Notice in `utopia-lit-review` one source got key `zotero-item-7`—if an agent doesn't already see that key in `corpus_map`, it has no `list_sources` tool in `rai`!
* **Improvement**:
  - Add `"sources"` to `read_notes(what="sources")` (or a `list_sources` tool) returning a compact table of every indexed source: `cite_key | author (year) | title | kind | blocks/pages | writer reading status | cited in drafts`.
  - Allow `read_location(source, "toc")` (or include the extracted heading hierarchy with page/para locations) so the agent or `reader` subagent can navigate a book's actual section headings directly.

---

## Part 4: Agent & MCP Tool Design Review

### 1. Session Startup Token Overhead in `assistant.md`
* **Where**: `src/rai/templates/project/.opencode/agents/assistant.md:46-55`:
  > 1. Your briefing should already be in your context; if not, `read_notes("brief")`.
  > 2. Read `project.md` (the read tool), then `read_notes` for `manuscript_map`, `corpus_map` and `claims`, and for `decisions`, `reading`, `candidates` and `objections` (status "unresolved").
* **Problem**: Look at `projects/utopia-lit-review`:
  - `.rai/brief.md` is already auto-loaded into the system context and contains the thesis, draft summary, corpus overview/themes, claims summary, objections/decisions count, and latest session summary.
  - If the assistant obeys step 2 literally on every new session, it fires **8 tool calls** before answering the user's first question. In `utopia-lit-review`, `read_notes("candidates")` alone returns 87 candidates (~3,500 tokens), `corpus_map` is ~2,500 tokens, etc.—burning 10k–20k tokens and 15–30 seconds of tool-call latency on turn 1 even for a simple question!
* **Fix**:
  Change `## Start of a session` in `assistant.md` to **lazy loading**:
  - `.rai/brief.md` is *already* in context.
  - Instruct the assistant: *"Start from your briefing. Read `project.md` and call `read_notes(...)` only for the maps/ledgers relevant to the writer's question (e.g., `corpus_map` and `reading` for library questions; `manuscript_map`, `chapter_note`, `claims`, and `objections` for draft/planning questions; `candidates` before proposing new works; `decisions` before making structural recommendations)."*
  - Include active `decisions` (usually <10 short lines) directly in `.rai/brief.md` so agents never miss a settled decision even without a tool call.

### 2. Asymmetric Permissions Between `assistant` and `sparring`
* **Look at `sparring.md` permissions (`src/rai/templates/project/.opencode/agents/sparring.md:5-25`)**:
  1. **Missing `rai_verify_quote`**: `AGENTS.md` (loaded by *every* agent) states: *"Always: ... quotations pass `verify_quote` before you use them."* Yet `sparring.md` is **denied** `rai_verify_quote`! If Sparring quotes a source to challenge the draft, it cannot obey `AGENTS.md`.
  2. **Missing `rai_decide_candidate`**: Sparring is granted `rai_read_candidate` and `rai_save_candidate` (so it can propose a contrary source, e.g. `C88`, in chat). When the writer immediately replies to Sparring, *"Good find, shortlist C88"* or *"Skip C88"*, Sparring is **denied** `rai_decide_candidate` and cannot record the writer's decision!
* **Fix**: Grant `rai_verify_quote` and `rai_decide_candidate` to `sparring.md` (and update `test_live_mcp_servers.py:136` / `test_live_opencode.py:181` to match).

### 3. `reader` Subagent Ergonomics
* **Where**: `src/rai/templates/project/.opencode/agents/reader.md`
* **Issue**: `reader` has `rai_read_location`, `rai_read_card`, `rai_read_notes`, `rai_search_passages`, plus file `read`. However:
  1. It does **not** have `rai_verify_quote`, so quotes it returns to the assistant aren't pre-verified.
  2. It does **not** have `rai_read_candidate` / `rai_read_candidate_section`, even though delegating the deep-reading of a 50-page open-access candidate PDF to `reader` is one of the best ways to keep the primary `research` agent's context clean!
* **Fix**: Grant `rai_verify_quote`, `rai_read_candidate`, and `rai_read_candidate_section` to `reader.md`.

### 4. `save_candidate` Tool Signature & Batch Ergonomics (`src/rai/server.py:251-281`)
* **Where**: `save_candidate` takes 10 required positional strings (`title`, `url_or_doi`, `serves`, `claim`, `evidence`, `standing`, `fit`, `read_depth`, `access`, `verdict`).
* **Observation from `utopia-lit-review`**:
  In `utopia-lit-review/.rai/ledger/candidates.jsonl`, the agent recorded 87 candidates, many of which were "metadata/bibliographic confirmation only" (books harvested from a bibliography where no full text is online). Because `read_candidate` wasn't called or didn't find full text, the agent passed free-form strings for `read_depth` ("not read (metadata only)") and sometimes bundled 3 books into a single candidate ID (e.g. `C14: Bloch + Adorno + Benjamin`, `C25: More + Le Guin + Bogdanov`, `C30: Albert + Cockshott & Cottrell + Dieterich`).
* **Why bundling happens**: Calling a 10-argument tool 87 separate times one by one is slow and tedious for the model, so it groups multiple works into single `C` IDs—which later breaks `decide_candidate` ("shortlist Le Guin but skip Bogdanov" becomes impossible when both are `C25`) and breaks DOI matching!
* **Fix**:
  - Instruct the assistant in `assistant.md`: *"One work per candidate entry (never bundle multiple books/articles into a single candidate ID)."*
  - Optionally support `save_candidates(candidates: list[dict])` (or keep `save_candidate` and allow a batch list) so harvesting 15 references from a bibliography takes 1 tool turn instead of 15 serial turns.

---

## Part 5: Model Selection Advice

The five-stage split (`reading`, `summarising`, `research`, `sparring`, `embedding`) is well-suited to optimizing quality vs. cost. Here is an evaluation of the current defaults and concrete profiles.

### 1. Evaluation of Shipped Defaults (`config.toml`)
| Stage | Shipped Default | Assessment |
| --- | --- | --- |
| `reading` | `opencode-go/deepseek-v4.1-flash` | **Good cost/speed choice.** `card_unit` is high-volume (10–35 calls per book) and has a deterministic post-filter (`check_unit`) that catches bad locations/quotes. Fast flash-tier models shine here. |
| `summarising` | `opencode-go/claude-haiku-5-5` | **Solid for `card_combine` and `card_relevance`, slightly light for large `corpus_map` / `manuscript_map`.** Comparing the cards in `utopia-lit-review`: the card built with `qwen3.8-flash-next` (`wrightEnvisioningRealUtopias2010.md`) and the cards built with `deepseek-v4.1-flash` + `claude-haiku-5-5` (`sutterluttiMakeCapitalismHistory2023.md`, `hahnelPeoplePeopleCase2012.md`) are both high quality, though Haiku left `## Gaps` empty in `corpus_map`. |
| `research` | `opencode-go/gemini-4-argon` | **Strong fit.** Needs large context window, disciplined multi-step tool calling, and adherence to negative constraints ("never write prose", "stay in library unless asked"). |
| `sparring` | `opencode-go/gemini-4-argon` | **Strong fit, or upgrade to a frontier reasoning/critique model.** Sparring is low-token-volume compared to `reading`, and its value depends entirely on intellectual sharpness—finding non-obvious structural gaps in an argument. |
| `embedding` | `ollama/qwen3-embedding:8b-q4_K_M` (4096 dims) | **High quality, with a storage/latency tradeoff to note.** `qwen3-embedding:8b` is top-tier on MTEB, and supports Matryoshka dimensions (MRL). At 4096 dims, each vector is 16 KB (trivial for 50 sources / 10k chunks = 160 MB, larger at 500 books). If the user has an Ollama server, keeping embeddings local avoids API round-trips on batch ingests. |

### 2. Recommended Profiles (Where to Spend vs. Save)

* **Where to SAVE**:
  - `reading`: Accounts for ~70–80% of all pipeline tokens during `rai cards`. Because every output is constrained by `UNIT_SCHEMA` and verified by `check_unit()` against the SQLite `blocks` table, you do **not** need a frontier model here.
* **Where to SPEND**:
  - `sparring` and `research`: Interactive turns where deep reasoning, subtle philosophical distinctions, and reliable tool use directly determine the writer's experience.
  - `summarising` (specifically if you notice shallow `corpus_map` debates or missed `claims` in `chapter_note`): `corpus_map` and `manuscript_map` run only once per refresh; their token volume is small, so they benefit disproportionately from a strong model.

* **Profile A: OpenCode Go Quality-First (Recommended for complex book/theory projects)**
  - `reading = "opencode-go/deepseek-v4.1-flash"`
  - `summarising = "opencode-go/claude-haiku-5-5"` (or `gemini-4-argon` if library > 20 books)
  - `research = "opencode-go/gemini-4-argon"`
  - `sparring = "opencode-go/claude-sonnet-4-6"` or `"opencode-go/gemini-4-argon"` (using a different model family for `sparring` than `research` also reduces correlated blind spots!)
  - `embedding = "ollama/qwen3-embedding:8b-q4_K_M"` (or `ollama/qwen3-embedding:4b` at `2560` dims on smaller GPUs)

* **Profile B: 100% Local Ollama (4x A100 / high-VRAM server)**
  - Calling `ollama` with `num_ctx = 262144` for *multiple different models* causes VRAM thrashing if `OLLAMA_MAX_LOADED_MODELS=2` and more than one chat model + one embedding model are configured.
  - **Crucial advice for local Ollama users (worth adding to `README.md`)**: If running chat stages on local Ollama, use the **same** model weight for all local chat stages (or at most 1 chat model + 1 embedding model) so Ollama never evicts a 70GB–120GB model from VRAM between `reading` and `summarising` during `rai refresh`!

### 3. Model-Family Risks in `llm.py`
* **Thinking tags in OpenAI-compatible responses (`src/rai/llm.py:147-163, 200-209`)**:
  Some models (especially DeepSeek/Qwen reasoning variants on OpenAI-compatible endpoints or when `response_format` falls back to `_without_schema`) prepend `<think>...</think>` blocks before the JSON payload or wrap the JSON in text before/after a ```` ```json ```` fence.
  Currently `_loads(text)` (`src/rai/llm.py:200-209`) only strips a leading/trailing ```` ``` ```` if `text.startswith("```")`. If a model outputs `<think>...</think>\n```json\n{...}\n```` or a preamble line before `{`, `_loads` raises `JSONDecodeError`.
  - **Fix**: In `_loads(text)`, strip any leading `<think>.*?</think>` block (`re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.DOTALL)`), and if `json.loads` fails, extract the first `{...}` block or fenced ```` ```json ... ``` ```` block before triggering an expensive LLM retry.

---

## Part 6: Docs, CLI & UX Improvements

1. **Outdated comment in `projects/utopia-lit-review/rai.toml` and `src/rai/templates/project/rai.toml`**:
   - `projects/utopia-lit-review/rai.toml:19-23` still has the old `[models] chat = "qwen-flash-128k"` and `# the Scout reads texts...` comment (which is actually forbidden now by `REPLACED_MODEL_KEYS` if uncommented!).
2. **`zotero-item-7` style citation keys (`projects/utopia-lit-review/.rai/cards/zotero-item-7.md`)**:
   - In `utopia-lit-review`, Ruth Kinna (2017) has `cite_key: zotero-item-7`. Why? Because Zotero or Better BibTeX had `zotero-item-7` in the citationKey field, so `zotero.py:171` treated `key_source = "zotero"` (valid) and never warned the user!
   - **Fix**: In `zotero.py` and `test_live_zotero.py`, flag placeholder keys matching `^zotero-item-\d+$` or `^item-\d+$` just like `generated` keys so `rai ingest` and `rai doctor` warn the writer to refresh Better BibTeX keys.
3. **`rai status` should check `sync_project(project, check=True)` and embedding setup**:
   - Currently `rai status` checks stale notes and claims, but does **not** warn if `opencode.jsonc`/agents are out of sync with `config.toml`/`rai.toml` or if the embedding config changed and needs `rai ingest --reembed`. Adding a one-line warning to `rai status` saves the user from discovering drift only when running `rai doctor`.
4. **Progress visibility during long `rai cards` / `rai refresh` runs**:
   - Building cards for a 300-page book takes 10–35 `reading` calls + 1 `combine` + 1 `relevance`. Printing elapsed time per unit and dropped/kept location counts (`say(f"    {ok} locations verified, {dropped} dropped ({elapsed:.1f}s)")`) gives immediate feedback on whether extraction/locations are healthy instead of waiting 15 minutes for the card to finish.

---

## Part 7: Testing & Quality Gaps

The test suite (`136 passed` fast tests + comprehensive `tests/live/` suite) is unusually well-crafted. The highest-value additions are:

1. **Test `open_questions()` with nested sub-bullets and trailing prose (`tests/test_pipelines.py`)**:
   - Reproduce the exact `projects/utopia-lit-review/project.md` structure (nested bullets under `For each utopia:` followed by explanatory paragraphs and taxonomy bullets) to prevent the `Q1`–`Q12` flattening bug (#1 in Part 2).
2. **Test `read_location` truncation when blocks contain internal `\n\n` (e.g., Markdown tables) (`tests/test_index.py`)**:
   - Verify that `read_location` with a small `max_tokens` returns the exact `range` and `citation` corresponding to the non-truncated blocks (#2 in Part 2).
3. **Test `resolve_location` on real-world PDF page labels (`tests/test_index.py`)**:
   - Test uppercase Roman numeral ranges (`pp. IV-VIII`), cover prefixes (`pp. C1-29`), and page ranges where the end page has no text block (#4 in Part 2).
4. **Test `_loads()` resilience to `<think>...</think>` tags and prose-wrapped JSON fences (`tests/test_llm_backends.py`)**:
   - Verify `chat_json` parses `<think>reasoning</think>\n```json\n{"a": 1}\n```` without wasting a retry call.
5. **Test source deletion and file-updated key renames clean up `.rai/cards/` and `.rai/sources/` (`tests/test_index.py`)**:
   - Verify `delete_source` removes the card, `.prev.md`, project note, and `notes` table rows (#3 in Part 2).

---

## Summary Priority Roadmap

1. **Immediate Bug Fixes (Low effort, high correctness gain)**:
   - Fix `open_questions()` nested-bullet parsing (`cards.py:176`).
   - Fix `read_location` `shown` block slicing (`search.py:211`).
   - Fix `PAGE_RE` and range resolution for uppercase Roman numerals, cover labels (`C1`), and empty end-pages (`search.py:92, 119`).
   - Clean up orphaned card/project-note files and `notes` rows on source deletion/rename (`store.py:228`, `ingest.py:78`).
   - Escape `|` in Markdown table renderers (`cards.py:242`, `corpus_map.py:323`) and fix empty `## Gaps` bullet (`corpus_map.py:335`).
   - Validate path inside `drafts/` in `refresh_chapter` (`server.py:121`).
   - Strip `<think>...</think>` in `llm._loads()` (`llm.py:200`).
2. **Agent & Prompt Tuning**:
   - Make `assistant.md` session-start `read_notes` calls lazy (on-demand) instead of firing 8 tool calls on turn 1; put active `decisions` directly into `.rai/brief.md`.
   - Grant `rai_verify_quote` and `rai_decide_candidate` to `sparring.md`, and `rai_verify_quote` + `rai_read_candidate*` to `reader.md`.
   - Instruct `assistant.md` never to bundle multiple works into a single `C` candidate ID.
3. **High-Value Research Features**:
   - Add `source` filter and per-source diversity cap to `search_passages`.
   - Add `read_notes("sources")` (library inventory table) and TOC browsing.
   - Auto-detect when shortlisted candidates in `reading-list.md` have been ingested into `library.db`.
   - Ingest the writer's own Zotero PDF highlights and annotations.
