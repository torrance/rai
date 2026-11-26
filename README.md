# nfh: a non-fiction research and writing harness

nfh gives a set of models the tools and memory to help you plan, research and
argue about a piece of non-fiction, while leaving the writing to you. Each
stage has its own model: raw reading, higher-level summarising, the research
assistant, the Sparring partner and embeddings. Models can be local Ollama
models or paid OpenCode Go models, mixed freely. You write in your own editor
and talk to the agents in a chat. You never edit their notes. It runs inside
OpenCode as three agents:

| Agent | What it does | What it can write |
| --- | --- | --- |
| **Assistant** | Your day-to-day agent: analyses the texts you hold and what to read in full, pulls sources out of them, plans and bounces ideas, checks claims, and, only when you ask, searches for and assesses new works | Plans and cited findings in `reports/`; the reading list, through its tools; `project.md` only with your approval |
| **Sparring** | Argues with your draft, in the chat: objections and weak steps, never wording. It may search the web and scholarly databases itself to check a claim or find the strongest contrary view | `reports/feedback/`, only if you ask it to save something |
| **Reader** (subagent) | Deep reads for the others, reporting back briefly | Nothing |

No agent can edit `drafts/`, and none can edit `.nfh/` directly. That's enforced
by OpenCode permissions, not by asking nicely.

Four rules run through everything:

- **You talk; it records.** Decisions ("that objection is fine", "I've read
  Arendt", "shortlist C3") go to the agent in plain language, and the agent
  records them. You never edit a notes file to make a decision.
- **Maps point, sources prove.** Agents start each session knowing your draft
  and library from cards and maps, but anything they tell you about a source
  comes from reading the source, and every quotation is checked by code.
- **Deterministic jobs are commands you run.** Ingesting, indexing and
  building notes are `nfh` commands, not agent actions.
- **The harness and your projects are separate.** This folder holds code and
  templates. Each project folder holds everything it produces.

---

## Contents

1. [Layout](#layout)
2. [One-time setup](#one-time-setup) and [testing it](#testing-your-setup)
3. [Configuration](#configuration)
4. [Starting a project](#starting-a-project)
5. [Working with the agents](#working-with-the-agents)
6. [Writing your drafts](#writing-your-drafts)
7. [Who changes what](#who-changes-what)
8. [Command reference](#command-reference)
9. [Troubleshooting](#troubleshooting)
10. [Developing the harness](#developing-the-harness)
11. [Known limits](#known-limits)

---

## Layout

```text
nfh/                          # the harness: code and templates only
  README.md  pyproject.toml  uv.lock  .python-version
  config.toml                 # settings: Ollama address, stage models, context, providers
  config.local.toml           # your own changes to those (optional; gitignored)
  .env                        # your API keys (copy from .env.example; gitignored)
  src/nfh/                    # the Python package
    prompts/                  # every prompt the pipelines use
    templates/project/        # what `nfh new` copies into a new project
  tests/
  projects/                   # your projects (ignored by the harness's git)
    my-essay/
      project.md  drafts/  sources-local/  references.bib  nfh.toml   # yours
      reports/                # what the agents write for you to read
      .nfh/                   # internal: index, cards, notes, your recorded decisions
      opencode.jsonc  AGENTS.md  .opencode/  .githooks/   # how OpenCode runs the agents
```

A project is complete on its own: copy, archive or delete its folder and
nothing is left behind. Two projects that share a source each index it
separately. The only things outside this folder are the tools themselves
(Ollama's models on the server, uv's package cache, OpenCode, Zotero and its
library).

### What each folder in a project is for

A project has three zones, and each has one rule.

**Yours: you write and edit these.**

| Path | What it is |
| --- | --- |
| `drafts/` | Your writing. Agents can read it and can never edit it. |
| `project.md` | Your thesis, reader, outline and **open questions**. Every source is related to the open questions, so keep them current. The assistant may propose edits; you approve each. |
| `nfh.toml` | This project's settings: which Zotero items belong to it, and any overrides. |
| `references.bib` | Your bibliography, exported from Zotero (see [Starting a project](#starting-a-project)). |
| `sources-local/` | Your own material that isn't in Zotero (interviews, notes), indexed with `nfh ingest --local`. Optional. |

**For you to read: `reports/`.** The agents write here; you read. You never edit
these files. To change something, say so in the chat.

| Path | Written by | What it is |
| --- | --- | --- |
| `reports/plans/` | Assistant | Plans, gaps, next steps |
| `reports/research/` | Assistant | Cited findings on a question, with pointers to what to read in full |
| `reports/feedback/` | Sparring | Feedback you asked it to save (normally objections stay in the chat) |
| `reports/reading-list.md` | Assistant, Sparring | New sources it found and assessed, with ids (C1, C2...) and your decisions on them |

**Internal: `.nfh/`.** The agents' memory. You never need to open it, and you
can look at any of it with `nfh show` (below).

| Path | What it is |
| --- | --- |
| `library.db`, `cards/`, `candidates/`, `logs/` | The search index, one card per source, texts fetched for candidate works, logs |
| `sources/` | How each source bears on your open questions |
| `corpus/` | An overview of your library: themes, debates, coverage, gaps |
| `manuscript/` | A short note on each draft file, plus a map of the whole draft |
| `claims.md`, `brief.md` | The claims in your drafts and whether the library supports them; the summary every agent reads at the start |
| `ledger/` | **Your decisions**: dismissed objections, decisions you stated, what you've read, what you shortlisted, and a summary of each conversation you `/wrap`. Plain text, and the only part of `.nfh/` that git tracks, because everything else can be rebuilt and this can't. |

Everything in `.nfh/` except `ledger/` is rebuilt by `nfh refresh`, but slowly
(every card is a model run), so don't delete it casually.

**Setup files.** `opencode.jsonc`, `AGENTS.md` (house rules every agent follows),
`.opencode/` (the agents' instructions and commands) and `.githooks/` tell
OpenCode how to run the agents. They're generated from your settings and
templates; see [Configuration](#configuration).

**The git hook.** There is one, `.githooks/post-commit`. After any commit that
touches `drafts/`, it runs `nfh manuscript` and then `nfh brief` in the
background: it re-maps the changed draft files, updates the claims list and
rebuilds `.nfh/brief.md`, so the agents see your latest draft. That uses the
model, so it takes a while, and your commit doesn't wait for it. Output goes to
`.nfh/logs/hook.log`; if Ollama is unreachable the failure is logged and
nothing else breaks. It only fires on commits (including the first), so after
uncommitted edits run `nfh manuscript` or `nfh refresh` yourself. `nfh new`
turns it on with `git config core.hooksPath .githooks`; a fresh clone needs
that once. To turn it off: `git config --unset core.hooksPath`.

---

## One-time setup

These steps assume Linux, as in the design sketch, and two machines:

- **The Ollama server** runs the chat and embedding models on its GPUs.
- **Your writing machine** runs everything else: the harness, OpenCode, Zotero
  and pandoc. It reaches Ollama over the network (for example a port forward)
  and **runs no GPU code**. The only local model work is Docling's PDF layout
  analysis, on the CPU, from a CPU-only build of PyTorch.

| Runs on the Ollama server | Runs on your writing machine |
| --- | --- |
| Any stage models you keep local (reading, summarising, research, sparring) | OpenCode and the agents' tool calls |
| A local embedding model, if you use one | nfh: index (SQLite), search, quote checks, notes |
| | Zotero and its local API; pandoc |
| | Docling's PDF layout models (CPU only) |
| | zotero-mcp, paper-search-mcp (no models) |

Stage models that use OpenCode Go run on OpenCode's servers instead and need
`OPENCODE_API_KEY` in `.env`.

If Ollama does run on the same machine, nothing changes except that the
forward isn't needed.

### 1. System tools

```bash
sudo apt install git pandoc
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
```

pandoc reads your drafts. Distribution packages can be old: if you'll write in
Typst, install a recent release from pandoc's GitHub releases page. nfh falls
back to its own Typst citation parser if pandoc can't read Typst citations.

### 2. Ollama on the server (four A100s)

Run these commands on the server, or on your machine with the `ollama` CLI and
`OLLAMA_HOST` pointing at your forward. If you can't change the server's
settings (no sudo), see [Without admin rights on the server](#without-admin-rights-on-the-server)
below.

Set the server's environment with a systemd override:

```bash
sudo systemctl edit ollama.service
```

Add:

```ini
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=262144"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=30m"
```

Then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
ollama pull qwen3.8-flash-next:125b-a6b-q4_K_M
ollama pull qwen3-embedding:0.6b
ollama run qwen3.8-flash-next:125b-a6b-q4_K_M "hello" && ollama ps
```

`ollama ps` should show the model at **100% GPU**. If part of it is on the
CPU, lower `OLLAMA_CONTEXT_LENGTH` to 196608, then 131072, and set
`num_ctx` to match in `config.local.toml` ([Configuration](#configuration)).

With one parallel slot, agents, subagents and `nfh` jobs take turns step by
step. Nothing blocks for good, but expect pauses when they switch.

#### Reaching Ollama from your writing machine

Forward the server's port 11434 to a free local port. If you also run Ollama
locally, it already holds 11434, so use another, for example:

```bash
ssh -N -L 11435:127.0.0.1:11434 you@ollama-server
```

Use `autossh` (or a systemd user unit) so the forward reconnects if it drops:
a card build that loses the connection mid-way fails and has to be rerun.
Long prompts cross the forward, but even a 262K-token prompt is only about
1 MB of text.

You'll record this address in nfh's settings in step 3, once the harness is
installed. If the forward uses 11434 (nothing else on that port), the default
already matches and there is nothing to record.

#### Without admin rights on the server

The context length can be attached to a model instead of the server.
`ollama create` needs no admin rights and copies no weights:

```bash
cat > Modelfile <<'EOF'
FROM qwen3.8-flash-next:125b-a6b-q4_K_M
PARAMETER num_ctx 131072
EOF
ollama create qwen-flash-128k -f Modelfile
```

You'll set the stages that stay on Ollama to `ollama/qwen-flash-128k` and
`num_ctx = 131072` in nfh's settings in step 3, so nfh and OpenCode both use
that name and the same context.

Without the server's q8_0 KV cache the context needs more VRAM, so start at 128K
and let `nfh selftest --only ollama` tell you whether it fits on the GPUs. If
you'd rather not repeat the number, set `num_ctx = 0`: nfh then asks for no
particular context and uses whatever the model or server defaults to, which is
also what OpenCode gets.

### 3. The harness

```bash
cd ~/writing                      # wherever you keep it
unzip nfh.zip && cd nfh           # or git clone
uv sync                           # creates .venv/ inside this folder
cp .env.example .env              # then fill it in (below)
```

`uv sync` takes PyTorch from PyTorch's **CPU-only** package index
(`download.pytorch.org/whl/cpu`, configured in `pyproject.toml`), so no
NVIDIA or CUDA libraries are installed. Your machine needs to reach that
address. The first sync also updates `uv.lock` to match.

If you synced an earlier version that installed `nvidia-*` packages, run
`uv sync` again: it removes them from `.venv/`. Their downloads stay in uv's
cache until you run `uv cache clean`.

The first PDF you ingest downloads Docling's layout models (a few hundred
MB) into `.cache/huggingface/` inside this folder.

To run `nfh` from anywhere, add an alias to your shell's rc file (this keeps
everything inside the folder, unlike `uv tool install`):

```bash
alias nfh='uv run --project ~/writing/nfh nfh'
```

Fill in `.env`:

| Key | What it's for | Where to get it |
| --- | --- | --- |
| `OPENCODE_API_KEY` (or `OPENCODE_GO_API_KEY`) | OpenCode Go stage models and embeddings | OpenCode Console > Account > API keys (Go subscription) |
| `TINYFISH_API_KEY` | The web search (through OpenCode) | tinyfish.ai; free, no card |
| `OPENALEX_API_KEY` | Scholarly metadata for candidates | openalex.org > Settings > API; free |
| `UNPAYWALL_EMAIL` | Finding open-access full text | Any address you read |

#### Your settings

The Ollama address, the models and the context aren't in `.env`. They are in
`config.toml`, already filled in with the defaults. Put only what differs for
your setup in a new file, `config.local.toml`, in this folder. For example,
with a port forward on 11435 and the 128K model variant from step 2:

```toml
[ollama]
host = "http://127.0.0.1:11435"

[models]
reading = "opencode-go/deepseek-v4.1-flash"
summarising = "opencode-go/claude-haiku-5-5"
research = "opencode-go/gemini-4-argon"
sparring = "opencode-go/gemini-4-argon"
embedding = "ollama/qwen3-embedding:8b-q4_K_M"
embedding_dims = 4096
num_ctx = 131072
```

Model references are `provider/model`, optionally with `#variant`; a bare name
means Ollama. The providers are `ollama` and `opencode-go`. All five stages are
required: nfh never falls back from one to another.

If the defaults suit you, skip the file. Check what is in force:

```bash
nfh config
```

You don't need `nfh sync` yet: no project exists, and `nfh new` reads these
settings when it creates one. See [Configuration](#configuration) for every
setting and for when `nfh sync` is needed later.

### 4. Zotero

Use **Zotero 8 or later**.

1. Install the Zotero desktop app and the **Zotero Connector** browser extension.
2. **Settings > Advanced:** tick *Allow other applications on this computer to
   communicate with Zotero*. nfh reads your library through this local API,
   read-only.
3. Install the **Better BibTeX** plugin, for two jobs Zotero doesn't do itself:
   - **Generating citation keys.** Zotero now has its own *Citation Key* field,
     which stores and syncs keys, but it doesn't create them. Better BibTeX
     fills that field (such as `ruiz2021platform`), and nfh, your drafts and the
     cards all use it. In Better BibTeX's settings, check that *Automatically
     fill citation key after* is above 0, so new items get a key without you
     asking. For items already in your library, select them and choose
     *Better BibTeX > Refresh BibTeX key*.
   - **Keeping `references.bib` up to date** (*Keep updated* on export; see
     [Starting a project](#starting-a-project)).
4. If your Zotero data isn't in `~/Zotero`, set `data_dir` in each project's
   `nfh.toml`.

If an item has no key, nfh makes a temporary one and says so at ingest. Once
Better BibTeX fills in the real key, the next ingest picks it up and moves the
item's card and project note to match. Upgrading from Zotero 7: keys pinned in
the *Extra* field still work, and Better BibTeX's migration moves them into
the *Citation Key* field.

### 5. OpenCode

Install **OpenCode 2**. The project files use its config format, and OpenCode 1
rejects them (`V2 permissions are not supported by OpenCode V1`).

```bash
curl -fsSL https://opencode.ai/v2/install | bash
# or with npm (add --prefix ~/.local if the global folder isn't writable,
# and put ~/.local/bin on your PATH):
npm install -g @opencode/cli
```

Check it took:

```bash
opencode --version      # must print v2.x
```

Three traps:

- **The older install routes give you OpenCode 1.** The `opencode.ai/install`
  script (no `v2` in the address), the `opencode-ai` npm package and the plain
  Homebrew formula all install version 1. Both versions use the `opencode`
  command, so remove version 1 first, the same way you installed it (for
  example `npm uninstall -g opencode-ai`).
- **The desktop app is a separate install.** The `.deb` and AppImage downloads
  are the desktop app and don't put `opencode` on your PATH. nfh needs the
  terminal command.
- **Pin a version while you work.** The harness was verified against OpenCode
  2.3.2 (`npm install -g @opencode/cli@2.3.2`). Its configuration format has
  changed between major versions, so avoid upgrading mid-project and rerun
  `nfh selftest` after any upgrade.

The two community MCP servers (`zotero-mcp` and `paper-search-mcp`) start
automatically through `uvx` the first time OpenCode opens a project. Their
versions are pinned in each project's `opencode.jsonc`. OpenCode 2 also runs a
background service, so the first command in a project can take a moment before
its servers show as connected.

### 6. Check everything

```bash
nfh config                        # the settings in force, and where each came from
nfh doctor                        # run inside a project to check Zotero too
```

It reports each dependency as `ok` or `FIX`, with the fix. Then test the
whole setup properly with `nfh selftest` (next section).

---

## Testing your setup

`nfh doctor` checks that things are installed. `nfh selftest` checks that they
work together, by running the live test suite in `tests/live/` against your
real models (Ollama and/or OpenCode Go), Zotero, APIs and OpenCode. Run it after setup, after upgrading
anything, and whenever something seems off.

```bash
nfh selftest                              # the setup, ~5-15 minutes
nfh --project projects/my-essay selftest  # also that project's scope and files
nfh selftest --slow                       # + real pipeline and long-context recall
nfh selftest --agents                     # + OpenCode agent runs (drafts guard)
nfh selftest --only ollama                # one area (a pytest -k expression)
```

Every test explains what to fix when it fails. Non-fatal findings appear as
warnings in the summary at the end.

| Area | What's verified |
| --- | --- |
| **Local machine** | No CUDA packages installed; PyTorch is a CPU-only build (not CUDA or ROCm); Docling's models are cached inside the harness; which Ollama server the harness reaches |
| **Models** | Every local stage model is pulled; the research model has tool and completion capabilities and sits 100% on the GPU; Ollama's default context equals `[models] num_ctx` (skipped when it is 0) (otherwise the model reloads every time agents and jobs take turns); JSON-schema output; 9/10 well-formed tool calls through `/v1`, the endpoint OpenCode uses; the `reasoning_effort` field the `quick` variant sends; embedding dimensions and sense. `--slow`: recall of a fact buried mid-way through a long prompt |
| **Zotero** | Local API answers; storage folder exists. With `--project`: scope isn't empty, items have files on disk, citation keys present in Zotero's own field (not made up by nfh) and unique, `tag_match` modes consistent, `references.bib` contains every key |
| **Extraction** | Docling installed and used, with printed page labels kept. With `--project`: a real PDF from the scope extracts with headings |
| **Scholarly APIs** | OpenAlex key accepted; Unpaywall email works; TinyFish key present; the candidate reader fetches a real open-access article in full |
| **MCP servers** | nfh, zotero-mcp and paper-search-mcp each start exactly as `opencode.jsonc` starts them. Every agent's allow rules are matched against the tools the servers really expose: no agent may call a Zotero write tool, and the assistant and Sparring get only `search_papers`. The paper search really is restricted to your `[scout] paper_sources` (asking for arXiv or PubMed gets "No valid sources", and `all` means only your list), and `--only paper_sources` also tries each configured database and reports which return results |
| **Pipeline** (`--slow`) | A synthetic source through ingest, real-embedding search, `read_location`, quote checks, a card built by the real model (locations kept, no unverifiable quotes), the corpus map, a draft's claims and the briefing |
| **OpenCode** | It is version 2 (version 1 rejects these projects); all three MCP servers connect (polled, since OpenCode 2 starts a background service on first use); OpenCode's own resolved permissions (`opencode debug agents`) keep every agent out of `drafts/` and `.nfh/` and give each exactly its intended tools; and, with a stand-in model endpoint so no real model is needed, what OpenCode actually sends: the briefing reaches the model, each agent is offered exactly its own tools (denied ones are hidden), and the quick variant applies only where intended. `--agents`: the model answers through OpenCode; an agent *can* write where allowed; the assistant and Sparring are each told to fix a typo in a draft and must fail; an `nfh_` tool permission works; the briefing is in the model's context; Sparring records an objection and its dismissal in the ledger |

The `--agents` tests need `opencode run --agent`. If your OpenCode version
lacks it, they skip; then check the drafts guard by hand (Troubleshooting).

---

## Configuration

Everything that depends on your setup lives in one prepopulated file,
`config.toml` in the harness folder, which ships with the defaults. Settings
are layered, each layer overriding the one before:

| Layer | What it is |
| --- | --- |
| built-in defaults | the same values as `config.toml`, used if it's missing |
| `config.toml` | the shipped, fully populated defaults |
| `config.local.toml` | **your** changes, same format, gitignored. Put them here so replacing `config.toml` with a newer one never overwrites them |
| a project's `nfh.toml` | repeat any section to change it for that project only |
| `NFH_OLLAMA_HOST` | an environment variable, for the Ollama address only |

Unknown settings, wrong types and invalid values are errors that say which
file to look at. `nfh config` prints every setting in force and the layer that
set it.

| Setting | Default | What it controls |
| --- | --- | --- |
| `[ollama] host` | `http://127.0.0.1:11434` | Where nfh reaches Ollama (the scheme is optional; a trailing `/v1` is ignored) |
| `[ollama] opencode_base_url` | empty | Where OpenCode reaches it; empty means `<host>/v1` |
| `[models] reading` | `opencode-go/deepseek-v4.1-flash` | Reads raw source text into card units; also the reader subagent |
| `[models] summarising` | `opencode-go/claude-haiku-5-5` | Higher-level synthesis: cards, corpus map, chapter and manuscript maps |
| `[models] research` | `opencode-go/gemini-4-argon` | The assistant agent |
| `[models] sparring` | `opencode-go/gemini-4-argon` | The adversarial Sparring agent |
| `[models] embedding`, `embedding_dims` | `ollama/qwen3-embedding:8b-q4_K_M`, `4096` | The embedding model and the length of its vectors |
| `[models] num_ctx` | `262144` | The context nfh asks for on Ollama calls. It must equal what Ollama allocates for OpenCode (which can't ask) or Ollama reloads the model whenever the two take turns. `0` asks for nothing; OpenCode Go ignores it |
| `[models] query_instruction`, `query_template`, `document_template` | Qwen3-embedding's format | How text is wrapped before embedding. Other models want other formats: see their model cards |
| `[opencode] provider`, `provider_package`, `quick_variant`, `quick_reasoning_effort` | `ollama`, `@opencode/ai/providers/openai-compatible`, `quick`, `low` | The local provider nfh configures for OpenCode, and the reasoning-turned-down variant added to local models. No agent uses it by default; choose it in OpenCode for faster, shallower answers. An empty effort omits the field |
| `[scout] paper_sources` | `openalex`, `crossref`, `doaj`, `core`, `ssrn` | The scholarly databases the paper search may use (the assistant and Sparring both have it). **Enforced**: the search tool is started knowing only these, so no agent can reach the others even if it asks. The default leaves out arXiv, bioRxiv, medRxiv, PubMed, PMC, Europe PMC, dblp, IACR and CiteSeerX, which drowned out social science, humanities and economics results. Other options: `base`, `hal`, `openaire`, `semantic`, `zenodo` |
| `[opencode] nfh_command` | empty | The command OpenCode and the git hook use to start nfh. Empty means `uv run --project <this folder> nfh` |

### Common changes

- **A different model for a stage:** set that stage under `[models]` in
  `config.local.toml`, then run `nfh sync --all`. For example, keep summarising
  local and send Sparring to OpenCode Go:
  ```toml
  [models]
  summarising = "ollama/qwen3.8-flash-next:125b-a6b-q4_K_M"
  sparring = "opencode-go/<strong-model-id>"
  ```
  All five stages must be present; there is no `chat` fallback. For everything
  on OpenCode Go, give all five stages an `opencode-go/...` reference and set
  `embedding_dims` to the embedding model's native size.
- **Using OpenCode Go:** put `OPENCODE_API_KEY` (or `OPENCODE_GO_API_KEY`) in
  `.env` and name a stage `opencode-go/<model-id>`. The same key serves
  OpenCode's built-in
  `opencode-go` provider and nfh's own pipeline and embedding calls. If
  OpenCode doesn't pick the key up, run `/connect` once and choose OpenCode Go;
  `nfh open` loads `.env` into its environment. A variant can be selected with
  `#`, for example `opencode-go/<model-id>#low`. Variants apply to OpenCode
  agent turns; nfh's own pipeline calls use the base model. The embedding
  stage takes no variant.
- **A different Ollama address or port:** set `[ollama] host`, run
  `nfh sync --all`. For one session only, `export NFH_OLLAMA_HOST=...` (this
  changes nfh's calls, not OpenCode's). nfh deliberately ignores the plain
  `OLLAMA_HOST` variable: it belongs to the `ollama` command, which accepts a
  different format and may point at a different Ollama.
- **A different embedding model:** set `embedding`, `embedding_dims` and the
  three format settings for that model, then `nfh ingest --reembed` in each
  project. This works the same for `ollama/...` and `opencode-go/...`. The
  index records the provider, model, dimensions and document format, and
  refuses to mix vectors made with different ones.
- **Moving the harness folder:** run `nfh sync --all` afterwards, since the
  generated files contain its path. Or set `nfh_command` to something that
  doesn't depend on the location (for example `["nfh"]`, with a small wrapper
  script on your PATH) and sync once.

### Keeping projects in step: `nfh sync`

OpenCode can't read `config.toml`, so these project files are generated from
your settings and from the harness's templates:

- `opencode.jsonc` (model, provider, Ollama address, how nfh is started, which paper databases may be searched),
- `AGENTS.md` and every file in `.opencode/agents/` and `.opencode/commands/`
  (the agents' instructions, permissions and model lines),
- `.githooks/post-commit`.

`nfh sync` brings them up to date (`--all` for every project, `--check` to only
report). nfh records what it last wrote to each file and decides file by file:

| Situation | What `nfh sync` does |
| --- | --- |
| You haven't touched the file | Updates it |
| You've edited it and nfh has nothing new for it | Leaves yours alone |
| You've edited it and nfh has something new | Leaves yours and writes the new version beside it as `<file>.nfh-new` for you to merge. An agent file whose only change is its model line just gets that line updated. |
| You deleted an agent or command file | Leaves it deleted |
| nfh no longer ships the file (a retired agent, such as the old Planner, Researcher and Scout) | Removes it if you haven't edited it; if you have, leaves it as yours and stops tracking it |

`--force` overwrites everything, including your edits. So you can customise an
agent's instructions and still receive improvements in later versions.
`nfh doctor`, `nfh selftest --project` and `nfh sync --check` tell you when a
project is out of date.

**When to run `nfh sync`.** Only once projects exist and something that feeds
those files has changed:

- You changed `[ollama]`, any `[models]` stage, `[scout]` or anything in
  `[opencode]`, in `config.toml`, `config.local.toml` or a project's `nfh.toml`.
- You moved or renamed the harness folder (the files contain its path, unless
  `nfh_command` is set).
- You replaced the harness with a newer version: sync brings in newer agent
  instructions, house rules and `opencode.jsonc` for every file you haven't
  edited.
- `nfh doctor`, `nfh selftest --project` or `nfh sync --check` says a project
  is out of date.

You don't need it for `num_ctx`, `embedding`, `embedding_dims`, the embedding
templates, `[zotero]`, `[limits]` or `[drafts]`: nfh reads those on every run.
(Changing the embedding model or `document_template` needs
`nfh ingest --reembed` instead. And `num_ctx` must still match what Ollama
allocates for OpenCode.)

### Upgrading a project made with an earlier version

Earlier versions kept notes in `notes/` and a journal you approved by hand. The
new layout (above) replaces them, so an existing project needs these steps once:

1. Update the harness (`unzip -o nfh.zip -x "nfh/uv.lock"`, then `uv sync`).
2. Replace the old `[models] chat`, `embed` and `embed_dims` settings with the
   five explicit stages: `reading`, `summarising`, `research`, `sparring`,
   `embedding` (plus `embedding_dims`). There is no `chat` fallback, and
   `nfh` will tell you if one is missing. Then run `nfh sync --force`. A plain
   `nfh sync` updates `opencode.jsonc` and the hook (earlier versions recorded
   those), but treats
   `AGENTS.md` and the agent and command files as hand-edited, because nfh has no
   record of having written them, and leaves your copies with a `.nfh-new` beside
   each. If you customised an agent's instructions, use the plain `nfh sync` and
   merge those files by hand.
3. In the project's `.gitignore`, replace the line `.nfh/` with these two lines,
   so git tracks your decisions:

   ```text
   .nfh/*
   !.nfh/ledger/
   ```

4. Run `nfh refresh`. It rebuilds the notes in their new places inside `.nfh/`.
   Cards are rebuilt once because the stage-model signature changed; unit
   readings are reused where possible. The per-source notes, maps and draft
   notes are regenerated, which takes model time.
5. Delete the old `notes/` and `research/` folders once you've kept anything you
   want from them. Old journal decisions aren't imported: tell the assistant or
   Sparring partner again what you had decided, and they'll record it.

---

## Starting a project

```bash
nfh new platform-labour-review --title "Platform labour: a review" --tag lit-review
cd projects/platform-labour-review
```

`nfh new` copies the template, renders `opencode.jsonc`, the agents' model
lines and the post-commit hook from your [settings](#configuration), and runs
`git init`. Because it reads your settings, set the Ollama address and models
first (setup step 3): a new project then needs no `nfh sync`.

Then:

1. **Choose the project's sources.** In `nfh.toml`, `[zotero]` takes a
   `collection`, `tags`, or both (both means an item must be in the collection
   *and* carry the tags). For a small essay, tags are the easy route: tag the
   handful of sources it draws on in Zotero.

   ```toml
   [zotero]
   collection = ""
   tags = ["lit-review"]
   tag_match = "any"   # any | all
   ```

2. **Set up the bibliography.** In Zotero, right-click the collection (or
   your library) > *Export*, choose *Better BibLaTeX* or *Better BibTeX*, tick
   *Keep updated*, and save it as `references.bib` in the project folder.

3. **Write `project.md`.** A rough thesis, the reader, an outline, and
   especially **Open questions**: one per line under that heading. Every
   source is related to these, so keep them current.

4. **Preview the ingest, then run it:**

   ```bash
   nfh ingest --dry-run      # lists what's in scope and which have files
   nfh refresh               # ingest, cards, corpus map, manuscript, brief
   ```

   A first refresh takes a while: each source's card means reading the whole
   source, chapter by chapter. Run large batches overnight.

5. **Check the first few cards** against sources you know well:
   `.nfh/cards/<cite_key>.md`. They're the foundation of everything the
   agents know about your library.

6. **Start working:** `nfh open` starts OpenCode in the project. The next
   section shows how to work with the agents from there.

---

## Working with the agents

You work in two places: your editor, where you write `drafts/`, and the OpenCode
chat, where you talk to the agents. That's all. You never edit the agents' notes
and never need to open `.nfh/`. Every decision you make goes to an agent in plain
language, and the agent records it.

```bash
cd projects/my-essay
nfh status        # anything flagged or stale?
nfh refresh       # after adding sources, changing open questions, or editing drafts
nfh open          # OpenCode, in this project
```

In OpenCode you'll mostly talk to the **assistant**, which starts by default. Pick
the **Sparring partner** with `/agents` (or Ctrl+X A, or Shift+Tab) when you want
your draft argued with. Each starts with a briefing on the project (your thesis,
what each draft file argues, an overview of the library, what's unresolved) and then
reads what it needs for your question.

**Start a new session when you change agent or task** (`/new`, or Ctrl+X N).
OpenCode sends the model the whole conversation with every message. While you stay
with one agent, the start of that prompt never changes, so the server only has to
read your new message. Switch agents inside one conversation and the new agent's
instructions and tools replace the old ones at the very start of the prompt, so
the server must re-read the entire conversation from scratch before it can
answer. That's the pause you'd notice. A fresh session costs far less (an empty
session is already around 10,000 tokens, mostly tool definitions, measured with
OpenCode 2.3.2), and nothing is lost: the briefing carries the state of the
project and your decisions are in the ledger. Switch inside a session only when
the new agent needs to see that conversation, and use `/compact` (Ctrl+X C) to
shorten a long one. With one general agent and one critic, you'll rarely need to.

### A typical loop

1. **Work out what to read** with the assistant. "What does my library say about
   deactivation as discipline? Which sections should I read in full, and which
   sources aren't worth my time?" You get a short answer in the chat; substantial
   findings are also saved in `reports/research/`, with citations and pointers.
   Tell it what you've read ("I've read Arendt chapter 3") and it remembers.
2. **Grow the reading list, when you ask.** "Pull the sources from Rosenblat 2018
   that bear on question 3, then search around them for recent empirical work."
   The assistant reads that text's citations, searches the scholarly databases and
   the web, reads each candidate, and lists what it found in the chat, best first,
   with ids (C1, C2...). The full assessments are in `reports/reading-list.md`.
   Answer with "shortlist C3" or "skip C5, it's about Germany".
3. **Plan and bounce ideas** with the assistant. "Where is my outline weakest
   against my open questions?" "What should I research next?" Brainstorming and
   structure are its job; a hard critique is the Sparring partner's.
4. **Read and write.** The pointed sections are for you to read in the sources
   themselves; the writing is yours, in `drafts/`.
5. **Spar** with the Sparring partner. "Read drafts/02-argument.md and give me the
   strongest objections." They arrive in the chat (below), tied to paragraph
   numbers. It may search the web and the scholarly databases itself to check a
   factual claim or find the strongest contrary view, and puts a contrary source
   worth your time on the reading list.
6. **Update.** Add the sources you want to Zotero (Connector, or *Add item by
   identifier* with the DOI), commit your drafts (the hook refreshes the draft
   notes), and run `nfh refresh`.

**The assistant searches outside your library only when you ask.** Its instructions
say so, so ask in plain words ("search for...", "find more around these") and it
will; otherwise it works from your library, your draft and its notes, and offers
when it thinks a search would help. That is an instruction, not a lock: the
databases it may use (`[scout] paper_sources`), the drafts guard and the tool lists
are enforced, but this one relies on the model following the rule. If yours
searches uninvited, change `websearch` and `papers_search_papers` to `ask` in
`.opencode/agents/assistant.md` and OpenCode will then ask you before each search.

### Where answers go

| You ask for | It arrives |
| --- | --- |
| A quick answer, objections, a claim check | In the chat |
| Substantial cited findings | A summary in the chat, and a report in `reports/research/` |
| A longer plan | In the chat, and `reports/plans/` if it's long or you ask |
| New sources | A ranked list in the chat, and `reports/reading-list.md` |
| Anything else you want to keep | Say "save this": it goes in `reports/` |

### Telling the agents what you've decided

Objections, for example, are a conversation:

> **You:** Read drafts/02-argument.md and give me the objections.
> **Sparring:** Three points. O12 (para 14): the survey is from 1958, so it can't carry the claim about platforms. O13 (para 22): the step from "rated" to "managed" is asserted, not shown. O14 (para 30): ...
> **You:** O12 is fine, I'm not worried about it, don't bring it up again. I'll fix O13 later. I've rewritten para 30.
> **Sparring:** Recorded: O12 dismissed; O13 will address; O14 answered.

Next session the Sparring partner won't raise O12 again, reminds you briefly
about O13, and re-checks O14 only if the draft has changed since. The same
works everywhere:

| You say | It records |
| --- | --- |
| "O12 is fine, don't raise it again" | the objection as dismissed |
| "I'll fix O13 later" / "I've dealt with O14" | will address / answered |
| "Reopen O12" | the objection as open again |
| "We're keeping the Weber comparison" / "drop the survey section" | a decision, in your words |
| "Withdraw D3" | the decision revoked |
| "I've read Arendt chapter 3" | Arendt as read, with your note |
| "Shortlist C3" / "skip C5, it's about Germany" / "undo C5" | the candidate shortlisted / dismissed / back to proposed |

Each agent confirms in one line what it recorded ("Recorded: O12 dismissed."). If
you don't see that line, say "record that". Run `/wrap` at the end of a conversation and the agent sweeps it for anything unrecorded.

### Seeing and undoing what's recorded

Ask in the chat: "What have I dismissed?" or "What have we decided so far?" You
can also print any of it yourself:

```bash
nfh show objections            # --file drafts/02-argument.md, --status unresolved
nfh show decisions
nfh show reading
nfh show candidates
nfh show sessions             # summaries of past conversations; --limit 20 for more
nfh show corpus_map            # also: brief, manuscript_map, chapter_note --file ..., claims
```

To undo something, say so in the chat ("reopen O12", "withdraw D3", "undo C5",
"I haven't actually read Arendt").

### What carries over to the next conversation

The model itself remembers nothing between conversations. At the start it has the
briefing (built from your notes, including the latest summary below) and reads the
records it needs:

| What carries over | How |
| --- | --- |
| Your decisions, dismissed objections, what you've read, works you shortlisted | The ledger, recorded as you said them in chat |
| What each conversation covered, what you came to understand, where you stopped | `/wrap` summaries; the latest is in the briefing |
| Your drafts, the library, the claims | The files, and the notes `nfh refresh` rebuilds |

Run `/wrap` before you close a conversation. The agent records anything you decided
that it hasn't yet, then writes a three-part summary and shows it to you:

> **Discussed:** Went through O12 to O14 on the argument chapter, then compared Rosenblat and Wood as seeds for question 3.
> **New understandings:** The 1958 survey can't carry the platform claim (yours). Rosenblat and Wood cite the same three studies, so one seed covers most of the ground (the assistant's).
> **Left off:** Rewriting para 14. Next: search around Wood's third citation, then shortlist.

Correct anything that's wrong in the chat and the agent replaces the summary. The
next conversation, with the assistant or the Sparring partner, opens by saying where
you left off and offers to pick up from there; `read_notes("sessions")` gives the
agents the fuller summaries, and `nfh show sessions` prints them for you. A
conversation you don't `/wrap` leaves behind only what the agent recorded as you went.

### Commands inside OpenCode

- `/check-claims drafts/02-argument.md` has the assistant check every new or
  changed claim in that file against your library, and report the results in the
  chat, contradicted claims first.
- `/wrap` ends a conversation properly: the agent records anything you decided that it
  hasn't yet, then summarises what was discussed, what you came to understand and where
  you left off (see [below](#what-carries-over-to-the-next-conversation)).

### After a session

- **Run `/wrap` before you close the conversation**, so the next one knows where you
  left off.
- **Read what the agents pointed you to**, in the sources themselves. That's the
  part the harness can't do for you.
- **Commit your drafts.** The hook refreshes the changed files' notes, claims and
  the briefing in the background (log: `.nfh/logs/hook.log`).
- **Before quoting from a report in your draft:** `nfh check` verifies every
  cited quotation in `reports/` and in the cards against the source text.

---

## Writing your drafts

- One file per chapter or section in `drafts/`, Markdown (`.md`) or Typst (`.typ`).
- **Cite with the keys in Zotero's Citation Key field:** Markdown
  `[@ruiz2021platform, p. 4]`, Typst `@ruiz2021platform[p. 4]`. nfh reads these
  exactly, and they render through pandoc or Typst with `references.bib`.
  `nfh status` and the briefing list any key your drafts cite that isn't in
  the index: a typo, an item outside the project's scope, or a key that
  changed in Zotero.
- Paragraphs are numbered for the agents (headings aren't counted), so
  feedback and claims point to "para 12".
- Write `TODO` anywhere in a paragraph and it shows up in that file's note.
- To typeset Markdown with Typst: `pandoc drafts/01.md --citeproc
  --bibliography references.bib -o out.pdf --pdf-engine=typst`.
- If you draft in Typst, keep prose plain and put styling in a separate
  template file, since custom functions may not survive conversion.

---

## Who changes what

| | You | The agents |
| --- | --- | --- |
| `drafts/`, `sources-local/`, `references.bib`, `nfh.toml` | write and edit | read only |
| `project.md` | write and edit | read; the assistant proposes edits and you approve each |
| `reports/` | read | write |
| `.nfh/` | never need to (`nfh show` prints it) | write, through their tools |
| `opencode.jsonc`, `AGENTS.md`, `.opencode/`, `.githooks/` | change settings, then `nfh sync`; you may edit an agent's instructions | read |

Your decisions never need a file edit: say them in the chat.

---

## Command reference

Run any command inside a project, or pass `--project PATH` first:
`nfh --project projects/x status`.

| Command | What it does |
| --- | --- |
| `nfh new NAME [--title T] [--collection C] [--tag T ...] [--path P] [--no-git]` | Create a project from the template |
| `nfh doctor` | Check Ollama, models (listing what the server has if a name is missing), Zotero, pandoc, sqlite-vec, OpenCode, keys, and that project files match the settings |
| `nfh config` | Show every setting in force and which file set it |
| `nfh show WHAT [--file F] [--status S] [--limit N]` | Print an internal note: brief, corpus_map, manuscript_map, chapter_note, claims, objections, decisions, reading, candidates, sessions |
| `nfh sync [--check] [--force] [--all]` | Bring `opencode.jsonc`, `AGENTS.md`, the agent and command files and the git hook up to date, keeping your edits |
| `nfh ingest` | Index the project's Zotero scope (adds, updates, removes) |
| `nfh ingest --tag T [--tag U] [--match any\|all]` | Only items with these tags, this run only (never removes) |
| `nfh ingest --collection C` / `--all` | Another collection, or the whole library, this run only |
| `nfh ingest --local` | Files in `sources-local/` (sidecar `<file>.yaml` for metadata) |
| `nfh ingest --dry-run` | List what would be ingested |
| `nfh ingest --reembed` | Re-embed everything after changing the embedding model |
| `nfh ingest --pdf-engine pymupdf` | Faster, simpler PDF extraction than Docling |
| `nfh inspect KEY [--limit N]` | Show a source's extracted text with locations |
| `nfh status` | Flagged sources, stale notes, claims, unresolved objections and decisions |
| `nfh cards [KEY ...] [--all] [--notes-only] [--diff KEY]` | Build stale cards (or given ones); `--diff` shows what changed |
| `nfh map [--rebuild]` | Update the corpus map |
| `nfh manuscript [FILE] [--force]` | Map changed draft files, extract claims |
| `nfh brief` | Rebuild `.nfh/brief.md`, the summary every agent reads |
| `nfh refresh [--no-ingest]` | Everything above, in order, only where stale |
| `nfh check [FILES ...]` | Verify every cited quotation in `reports/` and in the cards |
| `nfh open` | Start OpenCode in the project with `.env` loaded |
| `nfh selftest [--slow] [--agents] [--only EXPR]` | Test the real setup (see Testing your setup) |
| `nfh serve` | The MCP server; OpenCode starts it for you |

Ingest scope rules: settings in `nfh.toml` define the project; flags override
for one run. Only a run using the project's own scope removes sources that
have left it, so a one-off `--tag` run never deletes anything.

---

## Troubleshooting

- **The paper search is noisy, or finds nothing.** Which databases it can
  use is the setting `[scout] paper_sources` (see [Configuration](#configuration)).
  Edit it in `config.local.toml`, run `nfh sync --all`, and restart OpenCode.
  `nfh selftest --only paper_sources` tries each database and tells you which
  answer; some need a free key or are rate-limited (CORE and DOAJ log that they
  run without one, which limits their results). Databases cover every field, so
  expect the odd off-topic result even from the right sources; the assistant is told to
  skip them.
- **A long pause after switching agents.** The server is re-reading the whole
  conversation, because a different agent means a different start to the prompt
  (see [Working with the agents](#working-with-the-agents)). Start a new session
  with `/new` instead. The pause is roughly the conversation's length in tokens
  divided by your server's prompt-reading speed; `ollama run <model> --verbose`
  prints that speed as "prompt eval rate".
- **An agent didn't record something I told it.** It should confirm each
  recording in one line. If it didn't, say "record that", or run `/wrap`. Check
  with `nfh show objections` (or `decisions`, `reading`, `candidates`). Recording
  relies on the model calling the tool; `nfh selftest --agents` checks that
  yours does.
- **`opencode` says `Agent not found` when you start it from a script.** OpenCode
  takes its working directory from the `$PWD` environment variable, which
  changing directory in a script doesn't update. Set `PWD` to the project (`nfh open`
  does this for you).
- **`Configuration is invalid … V2 permissions are not supported by OpenCode
  V1`:** the `opencode` on your PATH is OpenCode 1. Install OpenCode 2 (setup
  step 5), removing version 1 first. `nfh doctor` reports which version it
  found, and `nfh open` refuses to start with version 1.
- **"Ollama isn't reachable":** `nfh config` shows the address nfh uses; check
  the port forward is up (`curl <that address>/api/version`), and that it is
  your server rather than a local Ollama on the same port.
- **`uv sync` can't reach `download.pytorch.org`:** that index serves the
  CPU-only PyTorch. Allow it on your network, or use `--pdf-engine pymupdf`
  and remove Docling, torch and torchvision from `pyproject.toml`.
- **Responses are slow, or `ollama ps` shows CPU:** lower the context length
  (setup step 2) and `[models] num_ctx` to match.
- **Agents skip tools or tool calls come back malformed:** check Ollama
  first: `curl` a tool-calling request to `http://127.0.0.1:11434/v1/chat/completions`.
  If Ollama is the problem, LM Studio or vLLM also serve this model, and
  OpenCode discovers both. Changing `"codemode": false` to `true` on a server
  in `opencode.jsonc` is also worth one try.
- **OpenCode hangs, or lists the wrong models:** it may be talking to a
  different Ollama than nfh. `nfh config` prints both addresses, and
  `nfh sync --check` tells you if `opencode.jsonc` lags them. `headerTimeout`
  is already off for Ollama.
- **The `quick` variant errors:** set `[opencode] quick_reasoning_effort = ""`
  (or a value your Ollama accepts) and run `nfh sync --all`.
- **`nfh doctor` says project files are out of date:** run `nfh sync`. If it
  reports a hand-edited file, merge `<file>.nfh-new` or use `--force`.
- **A permission rule doesn't seem to apply:** run `/mcps` in OpenCode to see
  the exact tool names (`<server>_<tool>`) and adjust the agent files. Test
  the drafts guard by asking an agent to fix a typo in a draft: it must be refused.
- **"Can't reach Zotero's local API":** open Zotero and check the setting in
  setup step 4.
- **A source is flagged `needs_ocr`:** it's a scan. Run
  `ocrmypdf in.pdf out.pdf`, attach the result in Zotero, and re-ingest.
- **Locations look like file pages, not printed pages:** the PDF has no page
  labels; `nfh inspect` says so. Citations then use the PDF's own page numbers.
- **"Index was built with another embedding model":** `nfh ingest --reembed`.
- **Notes reported stale:** `nfh refresh`. The briefing warns agents about
  stale notes until you do.
- **A card is wrong:** fix the cause (often extraction: check `nfh inspect`),
  then `nfh cards KEY`, and `nfh cards --diff KEY` to see what changed.

---

## Developing the harness

```bash
uv run pytest                     # fast tests; Ollama is stubbed, live tests skip
uv run pytest tests/live --live   # the live suite directly (same as nfh selftest)
uv run ruff format .              # formatting (the code passes ruff format --check)
uv run ruff check .
```

Prompts live in `src/nfh/prompts/`. If you change one materially, bump
`PROMPT_VERSION` in `src/nfh/config.py` so `nfh status` marks the cards built
with the old prompt as stale.

---

## Known limits

- **Run `nfh selftest` before relying on the harness.** It was built without
  access to your Ollama or Zotero. Against OpenCode 2.3.2, with a stand-in model
  endpoint, these were verified: the project and its three agents load; every
  permission resolves as intended (nobody edits `drafts/` or `.nfh/`); each agent is
  offered exactly its own tools; the briefing in `.nfh/brief.md` reaches the model
  even though git ignores it; and the quick variant applies where intended. Not
  verified here: that your Qwen model calls the recording tools reliably, and that
  Ollama accepts the `reasoning_effort` field (`--agents` checks both).
  Docling couldn't be installed where it was built, so its path was checked
  against Docling's source; `--pdf-engine pymupdf` is the tested fallback.
- **Your decisions rely on the model calling the recording tools.** Each agent
  confirms what it recorded; if it doesn't, say "record that" or run `/wrap`.
- **There is no migration command.** Upgrading an earlier project is a few
  manual steps, [above](#upgrading-a-project-made-with-an-earlier-version); old
  journal entries aren't imported.
- **Not everything is configurable yet.** The Ollama address, the five stage
  models, context, embedding format and provider settings are. Thresholds, token
  budgets, timeouts (nfh's Ollama calls have none), search defaults and
  embedding-dimension detection are still fixed in the code.
- **`uv.lock` predates the CPU-only PyTorch change.** It was generated
  where `download.pytorch.org` was unreachable, so your first `uv sync`
  re-resolves PyTorch and updates the lock. `uv sync --locked` will refuse
  until then. Commit the updated lock if you keep the harness in git.
- **Pinned community servers:** `zotero-mcp-server==0.17.3` and
  `paper-search-mcp==0.1.4`. paper-search-mcp needs `mcp<2`, which the
  template's `uvx --with "mcp<2"` provides. Rerun `nfh selftest --only mcp`
  after changing either pin.
- **Zotero linked files** stored relative to a base directory
  (`attachments:` paths) aren't resolved yet; stored files and absolute
  linked files are.
- **EPUB page numbers:** locations are chapter and paragraph; print page lists
  aren't read.
- **Web search** goes through OpenCode's built-in provider (TinyFish), so its
  free tier and rate limits apply.
