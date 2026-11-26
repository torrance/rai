"""Harness and project configuration.

The harness folder holds code only. Every project folder holds its own
``nfh.toml``, writing, notes and machine state (``.nfh/``). Nothing is written
outside those two places.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates" / "project"
PROMPT_DIR = PACKAGE_DIR / "prompts"
PROJECTS_DIR = HARNESS_ROOT / "projects"

PROMPT_VERSION = "1"

# The five model slots. Each is a single model reference; there is no fallback.
CHAT_STAGES = ("reading", "summarising", "research", "sparring")
MODEL_STAGES = (*CHAT_STAGES, "embedding")

# Providers nfh can call directly (for pipeline jobs and embeddings) and can
# name in the OpenCode config. Ollama is local; OpenCode Go is OpenAI-compatible.
MODEL_PROVIDERS = ("ollama", "opencode-go")
OPENCODE_GO_BASE_URL = "https://opencode.ai/inference/go"
OPENCODE_GO_CHAT_URL = f"{OPENCODE_GO_BASE_URL}/openai/v1/chat/completions"
OPENCODE_GO_EMBEDDINGS_URL = f"{OPENCODE_GO_BASE_URL}/openai/v1/embeddings"
OPENCODE_GO_MODELS_URL = f"{OPENCODE_GO_BASE_URL}/v1/models"
OPENCODE_GO_API_KEY_ENV = "OPENCODE_API_KEY"
OPENCODE_GO_API_KEY_ENVS = (OPENCODE_GO_API_KEY_ENV, "OPENCODE_GO_API_KEY")

DEFAULT_READING_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_SUMMARISING_MODEL = "opencode-go/claude-haiku-5-5"
DEFAULT_RESEARCH_MODEL = "opencode-go/gemini-4-argon"
DEFAULT_SPARRING_MODEL = "opencode-go/gemini-4-argon"
DEFAULT_EMBED_MODEL = "ollama/qwen3-embedding:8b-q4_K_M"
DEFAULT_EMBED_DIMS = 4096


class ConfigError(ValueError):
    """A problem in config.toml, config.local.toml or a project's nfh.toml."""


MODEL_CACHE = HARNESS_ROOT / ".cache" / "huggingface"


def opencode_go_key() -> str:
    """The OpenCode Go key, from either documented environment name."""
    for name in OPENCODE_GO_API_KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the harness .env without overriding the shell.

    Also keeps Docling's model downloads inside the harness folder.
    """
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE))
    path = path or HARNESS_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (about four characters per token for English)."""
    return max(1, len(text) // 4)


@dataclass
class ZoteroSettings:
    api: str = "http://127.0.0.1:23119/api"
    data_dir: str = "~/Zotero"
    collection: str = ""
    tags: list[str] = field(default_factory=list)
    tag_match: str = "any"

    @property
    def storage_dir(self) -> Path:
        return Path(self.data_dir).expanduser() / "storage"


@dataclass
class OllamaSettings:
    host: str = "http://127.0.0.1:11434"
    opencode_base_url: str = ""


@dataclass(frozen=True)
class ModelRef:
    """``provider/model[#variant]``; a bare name is an Ollama model."""

    provider: str
    model: str
    variant: str = ""

    @property
    def base(self) -> str:
        return f"{self.provider}/{self.model}"

    def __str__(self) -> str:
        return self.base + (f"#{self.variant}" if self.variant else "")


def parse_ref(value: str, *, field: str = "model") -> ModelRef:
    """Parse a model reference, rejecting providers nfh can't talk to."""
    value = (value or "").strip()
    if not value:
        raise ConfigError(f"{field} can't be empty")
    base, hash_sep, variant = value.partition("#")
    variant = variant.strip()
    if hash_sep and not variant:
        raise ConfigError(f"{field} has an empty #variant: {value!r}")
    provider, sep, model = base.partition("/")
    provider = provider.strip()
    if not sep:
        provider, model = "ollama", base.strip()
    model = model.strip()
    if not model:
        raise ConfigError(f"{field} needs a model after the provider: {value!r}")
    if provider not in MODEL_PROVIDERS:
        raise ConfigError(
            f"{field}: unknown provider {provider!r} in {value!r}; "
            f"known providers: {', '.join(MODEL_PROVIDERS)}"
        )
    return ModelRef(provider=provider, model=model, variant=variant)


def model_identity(value: str) -> str:
    """Canonical provider/model form, ignoring a pipeline-ignored variant."""
    return parse_ref(value).base


def same_model(left: str | None, right: str | None) -> bool:
    """Whether two stored/configured names mean the same underlying model.

    This also lets an old database that stored a bare Ollama name match the
    new explicit ``ollama/<name>`` form without a needless rebuild.
    """
    if not left or not right:
        return False
    try:
        return model_identity(left) == model_identity(right)
    except ConfigError:
        return left == right


@dataclass
class ModelSettings:
    # One model per stage. These are all required; nfh never falls back from
    # one stage to another.
    reading: str = DEFAULT_READING_MODEL
    summarising: str = DEFAULT_SUMMARISING_MODEL
    research: str = DEFAULT_RESEARCH_MODEL
    sparring: str = DEFAULT_SPARRING_MODEL
    embedding: str = DEFAULT_EMBED_MODEL
    embedding_dims: int = DEFAULT_EMBED_DIMS
    num_ctx: int = 262144
    query_instruction: str = (
        "Given a question about a body of texts, retrieve passages that answer it"
    )
    query_template: str = "Instruct: {instruction}\nQuery: {query}"
    document_template: str = "{text}"


@dataclass
class OpenCodeSettings:
    # The local provider nfh configures for OpenCode. Stage refs using this
    # provider get a generated provider block; opencode-go refs use OpenCode's
    # built-in provider and its own credential.
    provider: str = "ollama"
    provider_package: str = "@opencode/ai/providers/openai-compatible"
    quick_variant: str = "quick"
    quick_reasoning_effort: str = "low"
    nfh_command: list[str] = field(default_factory=list)


# The sources paper-search-mcp can search (its own list, plus two that need paid keys).
KNOWN_PAPER_SOURCES = (
    "arxiv", "pubmed", "biorxiv", "medrxiv", "google_scholar", "iacr", "semantic",
    "crossref", "openalex", "pmc", "core", "europepmc", "dblp", "openaire",
    "citeseerx", "doaj", "base", "zenodo", "hal", "ssrn", "unpaywall", "ieee", "acm",
)  # fmt: skip


@dataclass
class ScoutSettings:
    paper_sources: list[str] = field(
        default_factory=lambda: ["openalex", "crossref", "doaj", "core", "ssrn"]
    )


@dataclass
class Limits:
    chunk_min_tokens: int = 300
    chunk_max_tokens: int = 800
    unit_min_tokens: int = 5000
    unit_max_tokens: int = 40000
    single_unit_tokens: int = 60000
    candidate_max_tokens: int = 60000
    search_results: int = 8
    corpus_single_pass_cards: int = 40


@dataclass
class Project:
    root: Path
    name: str = ""
    title: str = ""
    drafts_dir_name: str = "drafts"
    zotero: ZoteroSettings = field(default_factory=ZoteroSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    limits: Limits = field(default_factory=Limits)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    opencode: OpenCodeSettings = field(default_factory=OpenCodeSettings)
    scout: ScoutSettings = field(default_factory=ScoutSettings)
    layers: list[tuple[str, dict]] = field(default_factory=list, repr=False)

    # Paths -----------------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.root / ".nfh"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "library.db"

    @property
    def cards_dir(self) -> Path:
        return self.state_dir / "cards"

    @property
    def candidates_dir(self) -> Path:
        return self.state_dir / "candidates"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def ledger_dir(self) -> Path:
        return self.state_dir / "ledger"

    @property
    def sources_notes_dir(self) -> Path:
        return self.state_dir / "sources"

    @property
    def corpus_dir(self) -> Path:
        return self.state_dir / "corpus"

    @property
    def manuscript_dir(self) -> Path:
        return self.state_dir / "manuscript"

    @property
    def brief_path(self) -> Path:
        return self.state_dir / "brief.md"

    @property
    def claims_path(self) -> Path:
        return self.state_dir / "claims.md"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def drafts_dir(self) -> Path:
        return self.root / self.drafts_dir_name

    @property
    def sources_local_dir(self) -> Path:
        return self.root / "sources-local"

    @property
    def project_md(self) -> Path:
        return self.root / "project.md"

    @property
    def reading_list(self) -> Path:
        return self.reports_dir / "reading-list.md"

    def ensure_dirs(self) -> None:
        for d in (
            self.state_dir,
            self.cards_dir,
            self.candidates_dir,
            self.logs_dir,
            self.ledger_dir,
            self.sources_notes_dir,
            self.corpus_dir,
            self.manuscript_dir,
            self.drafts_dir,
            self.sources_local_dir,
            self.reports_dir / "plans",
            self.reports_dir / "research",
            self.reports_dir / "feedback",
        ):
            d.mkdir(parents=True, exist_ok=True)

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)


CONFIG_FILE = HARNESS_ROOT / "config.toml"
LOCAL_CONFIG_FILE = HARNESS_ROOT / "config.local.toml"
BUILTIN = "built-in default"


def _sections(project: Project) -> dict[str, object]:
    return {
        "zotero": project.zotero,
        "models": project.models,
        "limits": project.limits,
        "ollama": project.ollama,
        "opencode": project.opencode,
        "scout": project.scout,
    }


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _same_type(current: object, value: object) -> bool:
    if isinstance(current, bool):
        return isinstance(value, bool)
    if isinstance(current, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(current, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, type(current))


REPLACED_MODEL_KEYS = {
    "chat": "reading, summarising, research and sparring",
    "embed": "embedding",
    "embed_dims": "embedding_dims",
}


def _apply(obj: object, values: dict, section: str, label: str) -> None:
    valid = {f.name for f in fields(obj)}
    for key, value in values.items():
        if key not in valid:
            if section == "models" and key in REPLACED_MODEL_KEYS:
                raise ConfigError(
                    f"{label}: [models] {key} was replaced by "
                    f"[models] {REPLACED_MODEL_KEYS[key]}; set each stage explicitly."
                )
            raise ConfigError(
                f"{label}: unknown setting [{section}] {key}. "
                f"Valid settings: {', '.join(sorted(valid))}."
            )
        current = getattr(obj, key)
        if not _same_type(current, value):
            raise ConfigError(
                f"{label}: [{section}] {key} should be a "
                f"{type(current).__name__}, not {value!r}."
            )
        setattr(obj, key, value)


def harness_layers() -> list[tuple[str, dict]]:
    layers = []
    for label, path in (
        ("config.toml", CONFIG_FILE),
        ("config.local.toml", LOCAL_CONFIG_FILE),
    ):
        if path.exists():
            layers.append((label, _read_toml(path)))
    return layers


def _build(root: Path, layers: list[tuple[str, dict]]) -> Project:
    project = Project(root=root, layers=layers)
    sections = _sections(project)
    for label, data in layers:
        for section, values in data.items():
            if section == "project":
                for key in values:
                    if key not in {"name", "title"}:
                        raise ConfigError(f"{label}: unknown setting [project] {key}.")
                project.name = values.get("name", project.name)
                project.title = values.get("title", project.title)
            elif section == "drafts":
                for key in values:
                    if key != "dir":
                        raise ConfigError(f"{label}: unknown setting [drafts] {key}.")
                project.drafts_dir_name = values.get("dir", project.drafts_dir_name)
            elif section in sections:
                _apply(sections[section], values, section, label)
            else:
                raise ConfigError(
                    f"{label}: unknown section [{section}]. Valid sections: "
                    f"project, drafts, {', '.join(sections)}."
                )
    return project


def _validate(project: Project) -> None:
    m = project.models
    problems = []
    if "{query}" not in m.query_template:
        problems.append("[models] query_template must contain {query}")
    if "{text}" not in m.document_template:
        problems.append("[models] document_template must contain {text}")
    if m.num_ctx < 0:
        problems.append("[models] num_ctx must be 0 (server default) or positive")
    if m.embedding_dims <= 0:
        problems.append("[models] embedding_dims must be positive")
    if project.opencode.provider not in MODEL_PROVIDERS:
        problems.append(
            f"[opencode] provider must be one of {', '.join(MODEL_PROVIDERS)}"
        )
    for stage in MODEL_STAGES:
        try:
            ref = parse_ref(getattr(m, stage), field=f"[models] {stage}")
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        if ref.provider not in {project.opencode.provider, "opencode-go"}:
            problems.append(
                f"[models] {stage} uses provider {ref.provider!r}, but "
                f"[opencode] provider is {project.opencode.provider!r}; "
                "use that provider or 'opencode-go'."
            )
        if stage in {"summarising", "embedding"} and ref.variant:
            problems.append(
                f"[models] {stage} can't have a #variant; variants apply to "
                "OpenCode agent turns"
            )
    quick = project.opencode.quick_variant
    if quick and quick.startswith("#"):
        problems.append("[opencode] quick_variant must not start with '#'")
    if not project.ollama.host.strip():
        problems.append("[ollama] host can't be empty")
    sources = project.scout.paper_sources
    unknown = [x for x in sources if x not in KNOWN_PAPER_SOURCES]
    if not sources:
        problems.append("[scout] paper_sources can't be empty")
    if unknown:
        problems.append(
            f"[scout] paper_sources has unknown sources {unknown}; "
            f"known: {', '.join(KNOWN_PAPER_SOURCES)}"
        )
    if project.zotero.tag_match not in {"any", "all"}:
        problems.append("[zotero] tag_match must be 'any' or 'all'")
    if problems:
        raise ConfigError(
            "; ".join(problems) + ". `nfh config` shows which file sets each value."
        )


def load_harness_settings() -> Project:
    """The harness-wide settings (config.toml, then config.local.toml)."""
    load_env()
    project = _build(HARNESS_ROOT, harness_layers())
    _validate(project)
    return project


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: cwd) to the folder holding nfh.toml."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "nfh.toml").exists():
            return candidate
    raise FileNotFoundError(
        f"No nfh.toml found in {here} or its parents. "
        "Run this inside a project, pass --project, or create one with `nfh new`."
    )


def load_project(path: Path | None = None) -> Project:
    load_env()
    root = find_project_root(path)
    layers = harness_layers() + [("nfh.toml", _read_toml(root / "nfh.toml"))]
    project = _build(root, layers)
    project.name = project.name or root.name
    _validate(project)
    return project


def normalise_host(value: str) -> str:
    """Accept '127.0.0.1:11435', 'http://host:11435/' or '.../v1'."""
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    if value.endswith("/v1"):
        value = value[:-3]
    return value.rstrip("/")


def ollama_host(project: Project | None = None, use_env: bool = True) -> str:
    """Where nfh reaches Ollama. NFH_OLLAMA_HOST overrides the configured host."""
    if use_env and os.environ.get("NFH_OLLAMA_HOST"):
        return normalise_host(os.environ["NFH_OLLAMA_HOST"])
    settings = project or load_harness_settings()
    return normalise_host(settings.ollama.host)


def opencode_base_url(project: Project | None = None) -> str:
    """Where OpenCode reaches Ollama (always the /v1 address)."""
    settings = project or load_harness_settings()
    explicit = settings.ollama.opencode_base_url.strip().rstrip("/")
    return explicit or f"{ollama_host(settings, use_env=False)}/v1"


def describe(project: Project) -> list[tuple[str, str, object, str]]:
    """Every setting in force, with the layer that set it."""
    rows: list[tuple[str, str, object, str]] = []
    for section, obj in _sections(project).items():
        for f in fields(obj):
            source = BUILTIN
            for label, data in project.layers:
                if f.name in data.get(section, {}):
                    source = label
            value = getattr(obj, f.name)
            if section == "ollama" and f.name == "host":
                if os.environ.get("NFH_OLLAMA_HOST"):
                    source = "env NFH_OLLAMA_HOST"
                value = ollama_host(project)
            rows.append((section, f.name, value, source))
    source = BUILTIN
    for label, data in project.layers:
        if "dir" in data.get("drafts", {}):
            source = label
    rows.append(("drafts", "dir", project.drafts_dir_name, source))
    return rows
