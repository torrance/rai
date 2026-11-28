# DESIGN.md: how rai works

This is the “what is actually going on?” document. It is written for a
technical reader who does not want to reverse-engineer the code to use the
system well.

## 30-second version

rai (Research AI) is a local research harness for long-form non-fiction. It:

1. Reads source documents from Zotero's storage, or from `sources-local/`.
2. Extracts their text into addressable passages.
3. Embeds those passages so they can be searched by meaning as well as words.
4. Uses stage-specific models to turn each source into a **card** and then into
   project-level maps.
5. Reads your draft to extract its argument, claims and citations.
6. Gives three OpenCode agents tools over all of that:
   - **Assistant**: research, prioritise, search, assess sources.
   - **Sparring**: critique the draft and track objections.
   - **Reader**: deep-read a long text and report back.
7. Records what *you* decide in an append-only **ledger**, so the next
   conversation does not relitigate settled points.
8. Never writes your prose and never edits `drafts/`.

The agent never “knows” your library from model memory. It knows what it has
looked up through tools, and anything it presents as fact about a source must
come from a tool result with a citation and location.

## Contents

1. [The three layers](#1-the-three-layers)
2. [Key technologies](#2-key-technologies)
3. [The project on disk](#3-the-project-on-disk)
4. [The nouns, in plain English](#4-the-nouns-in-plain-english)
5. [The pipelines](#5-the-pipelines)
6. [Model stages](#6-model-stages)
7. [How to use it well](#7-how-to-use-it-well)
8. [Health signals and failures](#8-health-signals-and-failures)
9. [Command and tool map](#9-command-and-tool-map)
10. [Where the code lives](#10-where-the-code-lives)
11. [The mental model to keep](#11-the-mental-model-to-keep)

---

## 1. The three layers

```text
┌──────────────────────────────────────────────────────────────┐
│ 1. Harness (this folder)                                     │
│    CLI: rai new / ingest / cards / map / manuscript / ...    │
│    config.toml, config.local.toml, .env, templates           │
└───────────────────────────────┬──────────────────────────────┘
                                │ reads/writes
┌───────────────────────────────▼──────────────────────────────┐
│ 2. Project (projects/<name>/)                                │
│    project.md, drafts/, sources-local/, reports/             │
│    .rai/  ← SQLite index, notes, maps, candidates, ledger    │
│    .opencode/ ← generated agents and commands                │
└───────────────────────────────┬──────────────────────────────┘
                                │ started by OpenCode
┌───────────────────────────────▼──────────────────────────────┐
│ 3. Agents (OpenCode)                                         │
│    assistant / sparring / reader                             │
│    MCP servers: rai, zotero, papers                          │
│    Models: local Ollama and/or OpenCode Go                   │
└──────────────────────────────────────────────────────────────┘
```

The harness is shared: code, templates and harness-wide settings. Every project
is self-contained: its writing, notes, database and ledger live inside its own
folder.

---

## 2. Key technologies

| Technology | What it does here | Why it matters to you |
| --- | --- | --- |
| **Python 3.12 + Typer** | The `rai` CLI. | `rai ...` commands build and maintain project state. |
| **OpenCode 2** | The chat/TUI and agent runtime. | You talk to the agents here; `rai open` starts it in the project. |
| **MCP** (Model Context Protocol) | Connects OpenCode agents to tools. | `search_passages`, `read_location`, `read_notes`, etc. are MCP tools, not model memory. |
| **Ollama** | Local model server. | Serves the default 8B embedding model and any chat stages you keep local. |
| **OpenCode Go** | Hosted model provider, OpenAI-compatible. | Serves paid/remote chat and embedding stages when configured with `opencode-go/...` refs. |
| **SQLite** | The project index at `.rai/library.db`. | Stores sources, blocks, chunks, notes metadata, claims and embeddings. |
| **SQLite FTS5** | Keyword/full-text search. | Finds exact words and phrases. |
| **sqlite-vec** | Vector search inside SQLite. | Finds passages by meaning; falls back to NumPy if unavailable. |
| **Embeddings** | Turns text into vectors of numbers. | Similar meanings land near each other; this powers semantic search. |
| **NumPy** | Vector maths and fallback similarity search. | Works even when sqlite-vec isn't available. |
| **Docling** | PDF layout analysis on CPU. | Finds headings, paragraphs and tables; no OCR. Scanned PDFs get flagged. |
| **PyMuPDF** | PDF text extraction, page labels, outlines. | Faster fallback, printed page numbers, candidate PDF headings. |
| **ebooklib + BeautifulSoup** | EPUB extraction. | Chapter and paragraph locations. |
| **trafilatura** | HTML article extraction. | Pulls the main text out of web pages. |
| **Pandoc** | Parses Markdown and Typst drafts into an AST. | Gives exact citations and paragraph numbers. |
| **RapidFuzz** | Fuzzy matching. | Quote checking (`near` matches) and matching claims across draft edits. |
| **scikit-learn** | Clustering. | Groups similar source cards before building a large corpus map. |
| **PyYAML** | Frontmatter and sidecars. | Card/note metadata and `sources-local/` metadata files. |
| **httpx** | HTTP calls. | Ollama, OpenCode Go, Zotero, OpenAlex, Unpaywall, arXiv. |
| **Zotero local API** | Read-only access to your Zotero library. | rai never writes to Zotero. You add candidates to Zotero yourself. |
| **paper-search-mcp** | Scholarly database search. | Used by the assistant/Sparring when you ask; sources restricted by config. |
| **TinyFish** | Web search through OpenCode. | Used by the assistant/Sparring only when you ask. |
| **OpenAlex** | Candidate metadata, open-access links. | Titles, authors, citation counts, retractions, OA locations. |
| **Unpaywall** | Finds legal open-access copies. | Full text for candidates where available. |
| **arXiv** | Preprint PDFs. | Candidate full text for arXiv references. |
| **Git** | Your writing history; project ledger tracking. | Draft commits trigger a hook that updates notes and the briefing. |

### Configuration layers

Settings are layered, each overriding the one before:

```text
built-in defaults
  < config.toml                 (the shipped defaults)
  < config.local.toml           (your machine, gitignored)
  < projects/<name>/rai.toml    (this project only)
  < RAI_OLLAMA_HOST             (environment, Ollama address only)
```

- `[models]` has the five stage refs. A ref is `provider/model[#variant]`;
  bare names mean Ollama. There is no `chat` fallback.
- `[opencode]` controls the local Ollama provider block and the optional
  `quick` variant.
- `[scout] paper_sources` is enforced when `paper-search-mcp` is started.
- `.env` holds API keys (`OPENCODE_API_KEY` or `OPENCODE_GO_API_KEY`,
  `TINYFISH_API_KEY`, `OPENALEX_API_KEY`, `UNPAYWALL_EMAIL`). Secrets are not
  written into generated project files.
- After changing agent models, providers or OpenCode settings, run
  `rai sync --all`. After changing the embedding setup, run
  `rai ingest --reembed` in each project.

### Embeddings, in one paragraph

An embedding model turns a piece of text into a fixed-length vector, e.g. 4096
numbers. Texts with similar meaning get vectors that point in similar
directions. rai embeds both the stored chunks and your search query, then asks
which stored vectors are closest. The embedding model, its dimension count and
the query/document text templates must stay consistent; if you change any of
them, the index must be rebuilt with `rai ingest --reembed`.

### Hybrid search

For each search query rai runs two searches:

1. **Vector search** (meaning, via embeddings).
2. **Keyword search** (FTS5/BM25, exact words).

It merges the two ranked lists with reciprocal rank fusion: a result gets
`1 / (60 + rank)` from each list, and the scores are added. That means a
passage found by both meaning and exact words rises to the top. Results are
returned as snippets with a citation and a location.

### Docling and extraction

PDF extraction is the most fragile part of any research pipeline, so it has a
quality gate:

- Docling reads the page layout and emits headings, paragraphs, tables and
  footnotes with page numbers. It runs on CPU-only PyTorch, never on a local
  GPU; the first PDF downloads its layout models into `.cache/`.
- OCR is deliberately **off**. A scanned/image-only PDF is flagged as
  `needs_ocr`; run OCRmyPDF and re-ingest it.
- If Docling is unavailable, PyMuPDF is used.
- Printed page labels (e.g. `xiv`, `79`) are preferred over raw file page
  numbers when the PDF has them.
- Word-count-per-page thresholds flag `needs_ocr` or `low_text` sources so you
  do not silently trust empty/garbage text.

EPUBs become chapter/paragraph locations. HTML becomes article text via
trafilatura. Markdown/text becomes paragraph-numbered blocks.

For drafts, Pandoc is preferred because it reports citations and paragraph
structure exactly. If Pandoc is missing, too old, or cannot read Typst
citations, rai falls back to its own regex parser; citations are then simpler
but the draft still maps.

---

## 3. The project on disk

```text
projects/my-essay/
├── project.md                 # you: thesis, reader, outline, open questions
├── rai.toml                   # project settings: Zotero scope, limits
├── AGENTS.md                  # house rules every agent loads
├── opencode.jsonc             # generated: models, MCP servers, permissions
├── drafts/                    # you: Markdown/Typst. Agents can read, not edit.
├── sources-local/             # PDFs/EPUB/HTML/text not in Zotero (+ .yaml)
├── reports/                   # agents write for you
│   ├── plans/
│   ├── research/
│   ├── feedback/
│   └── reading-list.md        # generated by the candidate tools
├── .opencode/
│   ├── agents/                # assistant.md, sparring.md, reader.md
│   └── commands/              # wrap.md, check-claims.md
├── .githooks/
│   └── post-commit            # updates notes after draft commits
└── .rai/                      # machine state; mostly rebuildable
    ├── library.db             # SQLite index
    ├── brief.md               # generated briefing loaded by every agent
    ├── claims.md              # generated claims ledger
    ├── citations.json         # which draft cites which key, how often
    ├── sync.json              # hashes of generated files
    ├── cards/                 # one card per source (+ .prev.md)
    ├── sources/               # one project note per source
    ├── corpus/
    │   ├── map.md             # themes, debates, coverage, gaps
    │   └── map.json           # cached themes/coverage
    ├── manuscript/
    │   ├── <chapter>.md       # chapter note
    │   └── map.md             # argument/glossary/promises/overlaps
    ├── candidates/
    │   └── <hash>/            # cached candidate metadata + source file
    ├── ledger/                # git-tracked; decisions cannot be rebuilt
    │   ├── objections.jsonl
    │   ├── decisions.jsonl
    │   ├── reading.jsonl
    │   ├── candidates.jsonl
    │   ├── sessions.jsonl
    │   └── notes.jsonl        # the assistant's working notes
    └── logs/
```

### What is tracked by git

The generated project `.gitignore` ignores `.rai/*` except `.rai/ledger/`.
Everything else in `.rai/` can be rebuilt from sources and models. The ledger
contains **your** decisions, so it is the one part that must survive a rebuild.

Your `drafts/`, `project.md`, `reports/` and ordinary project files are yours
to commit as you wish.

### The SQLite tables

`library.db` is not a mystery box; these are its tables:

| Table | Holds |
| --- | --- |
| `sources` | One row per source: internal key, title, authors, year, cite key, DOI/URL, file path/hash, tags, collections, extraction status. |
| `blocks` | The extracted text in reading order, with heading path and page/chapter/paragraph/sequence location. |
| `chunks` | Retrieval groups of blocks: text, heading path, location label, and a raw embedding BLOB. |
| `chunks_fts` | FTS5 keyword index over chunk text and headings. |
| `chunks_vec` | sqlite-vec vector index (when sqlite-vec is available). |
| `units` | Cached per-unit card readings, keyed by source and unit number, with prompt version and model. |
| `notes` | Metadata for every generated note file: kind, input hashes, model, prompt version, generation time. Used for staleness. |
| `working_notes` | Derived, rebuildable search index over `.rai/ledger/notes.jsonl`: title, abstract, body, status, sources/links, hashes and embedding. |
| `working_notes_fts` | FTS5 keyword index over working-note title, abstract and body. |
| `working_notes_vec` | sqlite-vec vector index over working-note title + abstract (when sqlite-vec is available). |
| `claims` | Claims extracted from drafts: chapter, paragraph, claim text, type, cited keys, status, evidence, active flag. |
| `meta` | Schema version and embedding setup (`embed_ref`, model, dimensions, document template). |

The raw embedding BLOB in `chunks` means vector search still works if
sqlite-vec is unavailable: rai loads the vectors into NumPy and computes
similarity itself.

---

## 4. The nouns, in plain English

### Sources and their text

| Noun | What it is |
| --- | --- |
| **Source** | One item rai can search: a Zotero item or a file in `sources-local/`. Stored in `sources` table. |
| **Library** | The set of indexed sources in `library.db`. |
| **Corpus** | The library plus everything derived from it: cards, project notes, corpus map. Informally, “the material you hold”. `.rai/corpus/` is only the map output. |
| **Cite key** | The short key used in citations, e.g. `ruiz2021`. Prefer Better BibTeX's key. If missing, rai makes a temporary one and warns; ask Better BibTeX to refresh keys in Zotero, then re-ingest. |
| **Source key** | Internal identity: `zotero:<itemKey>` or `local:<path>`. Stable even if the cite key changes. |
| **Block** | The smallest extracted passage, in reading order. A heading, paragraph, quote, table or footnote. Has a location. |
| **Chunk** | A group of blocks used for retrieval, roughly 300–800 tokens. Has a text snippet, heading path, location and an embedding. |
| **Unit** | A larger group of blocks (roughly 5k–40k tokens) used for reading a source into a card. The model's per-unit output is cached in the `units` table. |
| **Location** | How rai says where text is: `p. 79`, `pp. 79-81`, `ch. 4 para 12`, `paras 3-7`, `seq:120`, or a heading's words. |
| **Scope** | Which Zotero items belong to a project: a collection, tags, or both (with both, an item must be in the collection and carry the tags). |
| **Sidecar** | A `.yaml` file next to a `sources-local/` file with title/authors/year/cite key. |

### Derived notes

| Noun | What it is |
| --- | --- |
| **Card** | A model-written summary of one source: thesis, structure, concepts, main claims, notable passages, interlocutors, limits. Lives in `.rai/cards/<cite_key>.md`. This is a general description of the source, not yet tied to your project. |
| **Project note** | The card related to *your* open questions, plus where the source is cited in your drafts. Lives in `.rai/sources/<cite_key>.md`. |
| **Unit reading** | The model's structured reading of one unit of a source, cached before cards are combined. |
| **Corpus map** | A map of the whole library: overview, themes, debates, coverage of your open questions, lineage, gaps. Lives in `.rai/corpus/map.md`. Built from cards. |
| **Chapter note** | A model-written map of one draft file: argument steps, claims, terms introduced, threads and TODOs. Lives in `.rai/manuscript/<chapter>.md`. |
| **Manuscript map** | A map across all chapter notes: the argument line, glossary, promises the draft makes, overlaps. Lives in `.rai/manuscript/map.md`. |
| **Brief** | A deterministic summary assembled from the maps, claims, ledger and stale state. Loaded by every agent at session start. No model call. |
| **Claims ledger** | Every substantive claim in your drafts, with type, citation, status and evidence. Lives in `.rai/claims.md` and the `claims` table. |
| **Citations index** | `.rai/citations.json`: which draft cites which cite key, and how often. Used by project notes. |

### Candidates and reading

| Noun | What it is |
| --- | --- |
| **Candidate** | A work the assistant/Sparring found and assessed but that is **not** in the library yet. It gets an ID (`C1`, `C2`, …). |
| **Reading list** | `reports/reading-list.md`: candidates grouped as shortlisted, proposed or dismissed, with the assessment. Generated by the tools; don't edit it, tell the agent “shortlist C3” or “skip C5”. |
| **Candidate cache** | `.rai/candidates/<hash>/`: metadata and any downloaded PDF/HTML. Separate from the library. |
| **Read depth** | What was actually read: full text, profile, sections, or abstract only. |
| **Verdict** | The agent's recommendation: read in full, read sections, or skip, with a reason. |
| **Assessment fields** | Each reading-list entry also records what it serves, the claim it bears on, evidence, standing (venue, peer review, citation count, retraction), fit, access, and read depth. |
| **Candidate ID** | A short hash of the DOI or URL. It is not a citation key. |

A candidate becomes a source only when **you** add it to Zotero (or
`sources-local/`) and run `rai refresh`. rai does not write to Zotero.

### The ledger and decisions

The **ledger** is append-only JSON Lines under `.rai/ledger/`. Each line is an
event; the current state is the latest event for that ID. It is the only
authoritative record of what you decided.

| Noun | What it is |
| --- | --- |
| **Objection** | A critique raised by Sparring, with an ID (`O1`, `O2`, …), file, paragraph range and one-sentence summary. |
| **Decision** | Something you explicitly stated (“keep the Weber comparison”), with an ID (`D1`, …). It can be withdrawn with `revoke_decision`. |
| **Reading status** | How much of a source you say you have read: `not read`, `skimmed`, `read`, `studied`. |
| **Session summary** | A `/wrap` record of one conversation: what was discussed, new understandings, where you left off, reports saved. ID `S1`, … A corrected summary can replace an earlier one with `replaces`. |
| **Working note** | The assistant's durable, free-form knowledge: a title, a maintained abstract, a body, sources, verified evidence and links. ID `N1`, … Active or deleted; deleted notes are an archive. Written only when you ask. |
| **Candidate decision** | Shortlisted, dismissed, or proposed again. |

Objection statuses:

- `open`: raised, unresolved.
- `will_address`: you plan to fix it.
- `answered`: you say it is dealt with.
- `dismissed`: never raise it again.

rai also records whether the draft has changed since an objection was raised or
decided. That is how Sparring knows when an “answered” objection deserves a
second look.

### Reports

| Noun | What it is |
| --- | --- |
| **Report** | A substantial file an agent wrote for you under `reports/`: a plan, cited research findings, saved feedback. These are outputs, not the draft. |
| **Reading list** | A special report generated by the candidate tools. |

Short answers stay in chat. Reports exist for material you want to keep and
re-read.

### Staleness and hashes

| Noun | What it is |
| --- | --- |
| **Frontmatter** | YAML at the top of cards/notes containing hashes, model names and prompt version. rai uses it to decide whether a note is stale; don't edit these files by hand. |
| **Stale** | A note whose inputs changed: source file hash, card hash, open questions, draft file hash, prompt version, or stage model. `rai status` lists these. |
| **Prompt version** | A version marker for the prompt set. Changing it invalidates affected notes. |
| **Model signature** | Which stage models produced a card/note. Changing the stage model makes the note stale. |
| **Sync record** | `.rai/sync.json`, hashes of generated files, so `rai sync` can update untouched files and preserve hand edits. |

---

## 5. The pipelines

### 5.1 Creating a project

`rai new NAME`

- Copies the project template.
- Renders `opencode.jsonc`, `AGENTS.md`, `.opencode/agents/*.md`,
  `.opencode/commands/*.md` and `.githooks/post-commit` from the harness
  settings.
- Records what it generated in `.rai/sync.json`.
- Initialises git and sets `core.hooksPath` unless `--no-git`.

You then edit `project.md` and `rai.toml` (Zotero scope).

### 5.2 Syncing generated files

`rai sync`

- Re-renders generated project files from `config.toml` /
  `config.local.toml` / project `rai.toml`.
- Patches each agent's `model:` line.
- If you edited a generated file and rai also has a new version, it leaves
  yours alone and writes `<file>.rai-new` for you to merge.
- `rai sync --check` only reports drift.

Run it after changing models, providers, OpenCode settings or `rai_command`.

### 5.3 Ingest: sources → blocks → chunks → embeddings

```text
Zotero item / local file
        │
        ▼
 extract (Docling / PyMuPDF / ebooklib / trafilatura / text parser)
        │
        ▼
 blocks with locations
        │
        ├──► make_chunks → chunks + embeddings → SQLite
        │                         │
        │                         ├── FTS5 row (keyword)
        │                         └── vector (meaning)
        │
        └──► (later) card units
```

`rai ingest`:

1. Determines scope: project collection/tags, or `--tag`/`--collection` for one
   run, or `--all`.
2. For each Zotero item, finds the best attachment: PDF, EPUB, HTML, text.
3. Extracts blocks and locations.
4. Chunks the text and embeds each chunk.
5. Writes source, blocks, chunks, FTS rows and vectors atomically.
6. Removes sources that have left the project's configured scope.
7. Reports added/updated/unchanged/removed/flagged/skipped.

`rai ingest --local` does the same for `sources-local/`.

`rai ingest --reembed` leaves all text alone and rebuilds vectors and the
vector table with the current embedding model/dimensions.

### 5.4 Search → read → verify

Agent flow:

```text
search_passages(query, kind="card")
        │  (find which sources are about the topic)
        ▼
search_passages(query, kind="text")
        │  (find passages)
        ▼
read_location(source, location, radius=...)
        │  (read the exact text around the hit)
        ▼
verify_quote(source, quote, location)
        │  (exact / near / elsewhere / not_found)
        ▼
answer or report, with [@cite_key, location]
```

- `search_passages` is the project's only passage search: semantic + keyword
  over the ingested library. The Zotero MCP server covers item metadata,
  annotations and library management; the pinned install omits its optional
  semantic-search tool, and agents are not granted it. It returns snippets, not
  whole sources.
- `read_location` widens around a page or paragraph and returns a citation and
  a `seq` range.
- `verify_quote` normalises punctuation/hyphenation, then checks exactly, then
  fuzzily (≥95 similarity). `near` returns the real wording; `elsewhere` gives
  the right location; `not_found` means do not use it.
- Citations are `[@cite_key, location]`, copied from tool output. In reports,
  quotations are written `"exact words" [@key, p. 79]`; that is the form
  `rai check` verifies.

### 5.5 Cards: a source → a general card → a project note

```text
source text
   │
   ├─ split into units (chapters/sections; ~5k–40k tokens)
   │
   └─ reading model reads each unit → summary, claims, concepts,
      passages, interlocutors, limits (JSON)
            │
            ├─ verify locations and quotes; drop impossible entries
            ▼
      summarising model combines unit readings
            ▼
      card: thesis, structure, concepts, claims, passages,
            interlocutors, limits
            ▼
      index card sections as searchable chunks
            ▼
      project note: relate card to project.md's Open questions
                    + where the source is cited in drafts
```

The card is cached. If the source file, prompt version or `reading`/
`summarising` model changes, the card is stale and rebuilt.

### 5.6 Corpus map: cards → library-level understanding

`rai map` / part of `rai refresh`:

- If there are **≤40 cards**, one model pass reads all cards plus your open
  questions and search hits per question.
- With more cards, similar cards are clustered (scikit-learn), themes are
  written per cluster, then a synthesis pass writes the overview, debates,
  coverage and gaps.
- Writes `.rai/corpus/map.md` and `.rai/corpus/map.json`.
- Inputs include cards, project notes, open questions and the summarising
  model. Changing any of those makes the map stale.

### 5.7 Draft mapping: drafts → chapter notes → claims → manuscript map

```text
drafts/*.md|*.typ
   │
   ├─ pandoc -t json (or fallback parser)
   │     → numbered paragraphs + exact citations
   │
   ├─ summarising model → chapter note:
   │     argument steps, claims, terms, threads
   │
   ├─ claim sync → claims table
   │     fuzzy-match new claims to old ones
   │     reset status if the claim/citation changed
   │
   ├─ citations index → .rai/citations.json
   │
   └─ manuscript map → across all chapter notes:
         argument line, glossary, promises, overlaps
```

`rai manuscript` maps changed draft files. The git hook runs it (and `rai
brief`) in the background after a commit that touches `drafts/`.

### 5.8 Claim checking

Claims are extracted from the draft, not from sources. Each claim has:

- a **type**: `empirical` (about the world), `interpretive` (what a text/event
  means), `normative` (what ought to be), or `definitional` (how a term is
  used);
- the paragraph where it occurs;
- the citations attached to that paragraph;
- a **status**: `unchecked`, `supported`, `partly supported`, `contradicted`,
  `unsupported`;
- **evidence**: citations and notes drawn from passages the agent actually
  read.

`/check-claims` asks the assistant to take a draft file's `unchecked` claims,
search the library, read the relevant passages and call `update_claim` with a
status and evidence. The status reports what the library says, not whether the
argument is good.

The claims summary also flags uncited empirical claims, contradicted claims, and
claims supported by only one source. These are prompts to look, not verdicts.

### 5.9 Briefing

`rai brief` assembles `.rai/brief.md` deterministically:

- project thesis
- draft files, word counts and first argument step
- library size and corpus overview/themes
- claims state
- open objections, decisions
- unknown citations
- stale notes
- working-note count and a pointer to `read_notes("notes")` (no note content)
- “Where we left off” from the latest session summary

It is limited to roughly 3,000 tokens, so it fits in every agent's starting
context. `opencode.jsonc` loads it via `instructions`.

### 5.10 Candidates and the reading list

```text
DOI / URL
   │
   ├─ OpenAlex → metadata, citation counts, retraction, OA links
   ├─ Unpaywall → legal OA copies
   ├─ arXiv → PDF if applicable
   │
   └─ cache under .rai/candidates/<hash>/
         metadata + downloaded PDF/HTML
         full text or profile (abstract, TOC, opening/closing)
```

- `read_candidate` returns metadata + full text if it fits; otherwise a profile
  with a table of contents and sections. It also flags whether the work is
  already in the library or was previously dismissed.
- `read_candidate_section` reads one section of a long candidate.
- `save_candidate` records the assessment and adds it to the reading list.
- `decide_candidate` records shortlisted/dismissed/proposed.
- A dismissed candidate is not proposed again.

### 5.11 Ledger events

Agent tools append JSONL events:

- `raise_objections`, `resolve_objection`
- `record_decision`, `revoke_decision`
- `set_reading_status`
- `propose_candidate`, `decide_candidate`
- `record_session`
- `save_note`, `save_notes`, `revise_note`, `set_note_status` (working notes)

The next session's brief and `read_notes` views reconstruct current state from
the latest events. Never edit ledger files by hand; tell the agent what you
decided and let it record.

### 5.12 An agent session

The assistant is the default primary agent. Sparring is a separate primary
agent you choose; Reader is a subagent the other two can launch for one reading
task.

```text
OpenCode starts in the project
  │
  ├─ loads AGENTS.md (house rules)
  ├─ loads .rai/brief.md (whole-project summary)
  ├─ loads the chosen agent's instructions and permissions
  └─ starts MCP servers: rai, zotero, papers
        │
        ▼
  you ask a question
        │
        ├─ assistant: search library, read, assess, plan, candidates
        ├─ sparring: read draft, check settled objections, raise new ones
        └─ reader: deep-read a source/draft and report back briefly
        │
        ▼
  you respond; the agent records decisions/objections/reading
        │
        ▼
  /wrap → record_session → brief rebuilt for next time
  /remember → save_notes → working notes and their index
```

Permissions enforce the “writer writes” rule:

- Globally, edit/shell/execute/webfetch are denied.
- The assistant may write in `reports/plans/` and `reports/research/`, and may
  propose changes to `project.md` with your approval.
- Sparring may write in `reports/feedback/` only if you ask it to save feedback.
- Reader writes nothing.
- Every agent must deny edits to `drafts/**`.

You normally do not edit reports or ledger files directly. Talk to the agent;
it records the outcome through the tools.

### 5.13 Adversarial mode: how Sparring works

Sparring is a separate primary agent, not a mode switch inside the assistant.

1. It reads the draft file and that file's chapter note.
2. It checks the ledger first: existing objections and decisions. It never
   raises a dismissed objection again.
3. It looks for the weakest step in the argument, the strongest objection a
   critic would make, and the evidence a sceptical reader would want. It keeps
   to the few points that matter (at most five), strongest first.
4. It can search the web and scholarly databases when that sharpens an
   objection, and can read candidates before relying on them.
5. It registers objections with `raise_objections`, which gives each an ID,
   paragraph range and one-sentence summary.
6. When you reply, it records the outcome: `dismissed`, `will_address`,
   `answered` or `open`.
7. An `answered` objection is rechecked only if the draft has changed since it
   was decided.

It never suggests wording and never rewrites a sentence.

### 5.14 Git hook

`.githooks/post-commit` checks whether the commit touched `drafts/`. If so, it
runs in the background:

```bash
rai --project . manuscript && rai --project . brief
```

Output goes to `.rai/logs/hook.log`. Your commit does not wait for it.

### 5.15 What `rai refresh` does, in order

`rai refresh` is the “bring everything up to date” command. It runs:

1. Ingest Zotero items in the project's configured scope (unless
   `--no-ingest`).
2. Ingest `sources-local/` if it contains files.
3. Rebuild stale cards.
4. Rebuild stale project notes.
5. Rebuild the corpus map if its inputs changed.
6. Map changed drafts, extract claims, rebuild the manuscript map.
7. Rebuild the working-note search index from the ledger.
8. Rebuild `.rai/brief.md`.

Targeted commands do one part only: `rai ingest`, `rai cards`, `rai map`,
`rai manuscript`, `rai brief`.

Only a run using the project's own scope removes sources that have left it.
One-off `--tag`/`--collection` runs never delete anything.

---

## 6. Model stages

There are five model slots. They are all explicit; there is no fallback.
The shipped defaults are:

- `reading`: `opencode-go/deepseek-v4.1-flash`
- `summarising`: `opencode-go/claude-haiku-5-5`
- `research`: `opencode-go/gemini-4-argon`
- `sparring`: `opencode-go/gemini-4-argon`
- `embedding`: `ollama/qwen3-embedding:8b-q4_K_M` at 4096 dimensions

| Stage | Used by | Typical job |
| --- | --- | --- |
| `reading` | Card unit extraction; the `reader` subagent | Read raw text, return structured notes/JSON |
| `summarising` | Card combine, project-note relevance, corpus map, chapter notes, manuscript map | Synthesize over material already read |
| `research` | The `assistant` agent | Interactive research, planning, source assessment |
| `sparring` | The `sparring` agent | Adversarial critique, objections, counter-evidence |
| `embedding` | Chunk, card-section and query vectors | Semantic search |

Model references are `provider/model[#variant]`:

- `ollama/<model>`: local Ollama.
- `opencode-go/<model>`: OpenCode Go, using `OPENCODE_API_KEY` or
  `OPENCODE_GO_API_KEY`.
- A bare name means Ollama.
- Variants (`#low`) apply to OpenCode agent turns. rai's own pipeline calls
  use the base model. Embeddings take no variant.

### Pipeline calls vs agent calls

- **Pipeline calls** are made by rai itself. They are one-shot, structured
  (JSON where needed), have no tools/conversation, and write notes. These use
  `reading` and `summarising`.
- **Agent calls** are OpenCode turns. They have tools and conversation. These
  use `research`, `sparring`, and `reading` for the reader subagent. rai itself
  never calls `research` or `sparring`; they exist only inside OpenCode.

### Where the cost is

| Operation | Model work | Cost shape |
| --- | --- | --- |
| Ingest | Embeddings only | Proportional to chunks; no chat model |
| Card build | One `reading` call per unit + one `summarising` combine + one relevance call when there are open questions | Many calls; usually the biggest per-source cost |
| Corpus map | One pass for ≤40 cards; clusters + synthesis above that | Grows with library size |
| Chapter note | One `summarising` call per changed draft | Per changed file |
| Manuscript map | One `summarising` call over all chapter notes | Grows with draft size |
| Brief | No model | Cheap |
| Search | One query embedding + SQLite | Cheap |
| Agent conversation | `research`/`sparring` per turn; growing context | Can be expensive; `/new` resets |
| Reader subagent | `reading` over source text | Depends on how much is read |

The design intent is: a cheaper/faster `reading` model, a capable
`summarising` model, a strong `research` model, the strongest `sparring` model,
and a local `embedding` model. The research agent works from the brief, maps
and cards; it should delegate raw reading to `reader` rather than loading whole
sources into a costly model.

---

## 7. How to use it well

### Start a project

1. Fill in `project.md`: thesis, reader, outline, and especially
   **Open questions**.
2. Set the Zotero collection/tags in `rai.toml` (or `sources-local/`).
3. `rai refresh` or `rai ingest` + `rai cards` + `rai map`.
4. Read `.rai/brief.md` once to see what the system thinks it knows.
5. Ask the assistant to orient you: what the library covers, what is thin, what
   to read first.

### Research with the assistant

- Start with `search_passages(kind="card")` to find which sources are about the
  topic, then `kind="text"` for passages.
- Use `read_location` around hits; do not trust snippets as complete context.
- Ask for a report only for material you want to keep; otherwise chat is fine.
- If you want new sources, ask explicitly. The assistant stays in the library
  by default.
- When the assistant finds a candidate, read its verdict and decide:
  shortlist C3, skip C5, etc.

### Write and critique

- The agents never write prose and cannot edit `drafts/`.
- Use the Sparring agent when you want a hard critique. It raises objections
  with IDs and checks whether they were already dismissed.
- Answer objections in the chat. Dismissed objections are never raised again;
  “answered” ones are only rechecked if the draft changed.
- Run `/check-claims` on a changed draft to check its claims against the
  library.
- Commit drafts when you are ready; the hook updates chapter notes, claims and
  the brief in the background.

### Keep the system healthy

- Run `rai status` after a refresh. It flags OCR problems, stale notes, unknown
  citations, unresolved objections and claim state.
- Do not delete `.rai/ledger/`; everything else under `.rai/` can be rebuilt.
- If you change `embedding`, `embedding_dims` or the embedding templates, run
  `rai ingest --reembed` in each project.
- If you change `reading` or `summarising`, the affected cards/maps/notes become
  stale; run `rai refresh`.
- If you change `research`/`sparring`/`reader` models, run `rai sync --all`.
- If a PDF is `needs_ocr`, OCR it with OCRmyPDF and re-ingest; otherwise the
  library only contains page images, not text.
- Keep cite keys stable in Zotero. If Better BibTeX changes a key, rai follows
  it and moves the card/project note.
- Use `/wrap` at the end of a session. The next conversation starts with a
  “where we left off” brief instead of a blank slate.

### What not to expect

- The agents do not know your library from training. They only know what tools
  return.
- A card is a model's reading, not the source itself. For facts and quotes,
  read the location and verify.
- The corpus map is a map, not proof. Use it to decide where to look.
- The system does not write or rewrite your prose.
- Zotero is read-only. Adding a candidate to the library is your action.
- Web and paper search are opt-in; the assistant does not browse on its own.
- Text fetched from the web or from a source is material to assess, never
  instructions for the agent to follow.

---

## 8. Health signals and failures

| Signal | Meaning | Fix |
| --- | --- | --- |
| `needs_ocr` | PDF has too little text per page | OCR with OCRmyPDF; re-ingest |
| `low_text` | Extraction found suspiciously little text | Inspect with `rai inspect`; check the PDF |
| Stale notes | Inputs/models changed since notes were built | `rai refresh` (or targeted `rai cards`/`rai map`) |
| Unknown citations | Draft cites a key not in the index | Typo, key changed in Zotero, or item out of scope |
| `unchecked` claims | Not yet checked against the library | Run `/check-claims` |
| `contradicted` claims | Library evidence goes against a claim | Read the evidence; revise or address it |
| Embedding mismatch | Index vectors predate the current embedding setup | `rai ingest --reembed` |
| `opencode.jsonc` drift | Settings changed but project files not regenerated | `rai sync --all` |
| Missing OpenCode Go model/key | Wrong model ID, no key, or provider not connected | `rai doctor`; `/connect` if needed |
| Empty paper source results | Database needs a key or is rate-limited | Adjust `[scout] paper_sources`; use `rai selftest --only paper_sources` |

Useful commands:

- `rai doctor`: dependencies, keys, models, Zotero, OpenCode, sync drift.
- `rai status`: project health, stale notes, claims, objections, unknown
  citations.
- `rai inspect <cite_key>`: extracted blocks and locations; useful for OCR/text
  problems.
- `rai check`: verify quoted passages in reports/cards.
- `rai selftest`: live end-to-end tests against the real services.
- Logs: `.rai/logs/rai.log`, `.rai/logs/server.log`, `.rai/logs/hook.log`.

---

## 9. Command and tool map

### CLI

| Command | What it does |
| --- | --- |
| `rai new` | Create a project from the template |
| `rai refresh` | Ingest, rebuild stale notes/maps, map drafts, rebuild brief |
| `rai ingest` | Index Zotero sources; `--local` for `sources-local/`; `--reembed` rebuild vectors |
| `rai cards` | Build/refresh cards; `--notes-only` for project notes; `--all` for every card |
| `rai map` | Rebuild the corpus map |
| `rai manuscript` | Map changed drafts, extract claims, rebuild manuscript map |
| `rai brief` | Rebuild the briefing |
| `rai status` | Flagged sources, stale notes, claims, objections, decisions, unknown citations |
| `rai check` | Verify quotes in reports and cards |
| `rai show` | Print internal notes (`brief`, `corpus_map`, `manuscript_map`, etc.) |
| `rai inspect` | Print a source's extracted blocks and locations |
| `rai config` | Show every setting in force and which file set it |
| `rai sync` | Regenerate OpenCode config/agents/hook from settings |
| `rai doctor` | Check the whole installation |
| `rai open` | Start OpenCode in the project with `.env` loaded |
| `rai serve` | Run the MCP server (OpenCode starts it) |
| `rai selftest` | Run the live tests |

### MCP tools available to agents

| Group | Tools |
| --- | --- |
| Search/read | `search_passages`, `read_location`, `verify_quote` |
| Notes | `read_card`, `read_notes`, `read_note`, `search_notes`, `refresh_card`, `refresh_chapter` |
| Working notes (assistant writes) | `save_note`, `save_notes`, `revise_note`, `set_note_status`; Sparring gets `read_note`/`search_notes` only |
| Claims | `update_claim` |
| Decisions | `raise_objections`, `resolve_objection`, `record_decision`, `revoke_decision`, `set_reading_status`, `record_session` |
| Candidates | `read_candidate`, `read_candidate_section`, `save_candidate`, `save_candidates`, `decide_candidate` |

### Slash commands

| Command | What it does |
| --- | --- |
| `/wrap` | Record unrecorded decisions and summarise the conversation for next time |
| `/remember [focus]` | Record durable working knowledge from the conversation as revisable notes |
| `/tend` | Review active notes for duplication and propose a consolidation plan |
| `/check-claims` | Check unchecked claims in a draft file against the library |

---

## 10. Where the code lives

| Module | Responsibility |
| --- | --- |
| `config.py` | Settings layers, model refs, project paths |
| `cli.py` | `rai` commands |
| `server.py` | MCP tools |
| `zotero.py` | Read-only Zotero local API |
| `extract.py` | PDF/EPUB/HTML/text → blocks with locations |
| `chunk.py` | Blocks → chunks and card units |
| `embed.py` | Ollama/OpenCode Go embeddings |
| `store.py` | SQLite schema and reads/writes |
| `search.py` | Hybrid search, location resolution, reading |
| `verify.py` | Quote verification |
| `cards.py` | Unit reading → card → project note |
| `corpus_map.py` | Library themes, debates, coverage, gaps |
| `manuscript.py` | Draft parsing, chapter notes, claims, manuscript map |
| `claims.py` | Claims ledger rendering/status |
| `ledger.py` | JSONL objections/decisions/reading/candidates/sessions/working notes |
| `note_search.py` | Derived working-note index and hybrid note search |
| `candidates.py` | Candidate lookup, full text, reading list |
| `brief.py` | Deterministic session briefing |
| `notes.py` | Read-only views of internal notes and working notes |
| `render.py` | OpenCode config/agent/hook generation and sync |
| `status.py` | Staleness and health summary |

---

## 11. The mental model to keep

- **Blocks are raw text; chunks are for finding; units are for reading;
  cards are a model's summary; project notes relate cards to your questions.**
- **The corpus map is a map of the library; the manuscript map is a map of your
  draft.**
- **Candidates are outside the library until you add them.**
- **The ledger is your memory; the brief is what the agents read at startup.**
- **Sessions are episodic; working notes are semantic; decisions are
  authoritative.** A session summary says what happened in one conversation; a
  working note holds what the assistant has come to understand and can be revised
  across conversations; a decision is yours and binding.
- **Maps point, sources prove.** Search and cards tell you where to look;
  `read_location` and `verify_quote` establish what is actually there.
- **The writer writes; the agents research, critique and record.**
