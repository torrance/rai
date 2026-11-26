"""Turn settings into the files OpenCode and git read, and keep them in step.

Settings live in config.toml (and config.local.toml, and a project's rai.toml).
OpenCode can't read those, so a few project files are generated from them:
``opencode.jsonc`` and the git hook are rendered whole, and each agent file's
``model:`` line is patched. ``rai sync`` re-renders them after a settings change.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    HARNESS_ROOT,
    TEMPLATE_DIR,
    ModelRef,
    Project,
    opencode_base_url,
    parse_ref,
)

PAPER_SEARCH_PIN = "paper-search-mcp==0.1.4"
MODEL_LINE = re.compile(r"^model:.*$", re.MULTILINE)


def _json_inner(value: str) -> str:
    """A string escaped for the inside of a JSON or YAML double-quoted string."""
    return json.dumps(value)[1:-1]


def rai_command(settings: Project) -> list[str]:
    configured = list(settings.opencode.rai_command)
    return configured or ["uv", "run", "--project", str(HARNESS_ROOT), "rai"]


def papers_command(settings: Project) -> list[str]:
    """Start paper-search-mcp with its source list cut down to the configured one.

    The server checks both "all" and any named source against its ALL_SOURCES
    list, so shortening that list at start-up limits what any agent can search.
    """
    code = (
        "import paper_search_mcp.server as s; "
        f"s.ALL_SOURCES[:] = {list(settings.scout.paper_sources)!r}; s.main()"
    )
    return ["uvx", "--from", PAPER_SEARCH_PIN, "--with", "mcp<2", "python", "-c", code]


def _quick_variants(settings: Project) -> list[dict[str, object]]:
    oc = settings.opencode
    if not oc.quick_variant:
        return []
    effort = oc.quick_reasoning_effort.strip()
    body = {"reasoning_effort": effort} if effort else {}
    return [{"id": oc.quick_variant, "body": body}]


def opencode_providers(settings: Project) -> dict[str, object]:
    """Provider blocks for stage models OpenCode must reach locally.

    opencode-go models use OpenCode's built-in provider and credential, so they
    need no block here. Local stage models share the configured provider block.
    """
    oc = settings.opencode
    models = (
        settings.models.reading,
        settings.models.research,
        settings.models.sparring,
    )
    by_provider: dict[str, dict[str, object]] = {}
    variants = _quick_variants(settings)
    for value in models:
        ref: ModelRef = parse_ref(value, field="[models] agent")
        if ref.provider == "opencode-go":
            continue
        entry: dict[str, object] = {"modelID": ref.model}
        if variants:
            entry["variants"] = variants
        by_provider.setdefault(ref.provider, {})[ref.model] = entry
    return {
        provider: {
            "package": oc.provider_package,
            "settings": {
                "baseURL": opencode_base_url(settings),
                "headerTimeout": False,
            },
            "models": provider_models,
        }
        for provider, provider_models in by_provider.items()
    }


def template_values(
    settings: Project,
    name: str = "",
    title: str = "",
    collection: str = "",
    tags: list[str] | None = None,
) -> dict[str, str]:
    command = rai_command(settings)
    m = settings.models
    reading = parse_ref(m.reading, field="[models] reading")
    research = parse_ref(m.research, field="[models] research")
    sparring = parse_ref(m.sparring, field="[models] sparring")
    return {
        "{{HARNESS_ROOT}}": str(HARNESS_ROOT),
        "{{PROJECT_NAME}}": name,
        "{{PROJECT_TITLE}}": title,
        "{{COLLECTION}}": collection,
        "{{TAGS}}": "[" + ", ".join(f'"{t}"' for t in (tags or [])) + "]",
        "{{OC_MODEL_RESEARCH}}": _json_inner(str(research)),
        "{{OC_MODEL_RESEARCH_BASE}}": _json_inner(research.base),
        "{{OC_MODEL_SPARRING}}": _json_inner(str(sparring)),
        "{{OC_MODEL_READER}}": _json_inner(str(reading)),
        "{{OC_PROVIDERS_JSON}}": json.dumps(opencode_providers(settings), indent=2),
        "{{PAPERS_COMMAND_JSON}}": json.dumps(papers_command(settings)),
        "{{RAI_COMMAND_JSON}}": json.dumps([*command, "--project", ".", "serve"]),
        "{{RAI_COMMAND_SH}}": shlex.join(command).replace("'", "'\\''"),
    }


def render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(key, value)
    return text


# Sync ------------------------------------------------------------------------
@dataclass
class SyncResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # your edits; nothing new from rai
    conflicts: list[str] = field(default_factory=list)  # both changed
    drift: list[str] = field(default_factory=list)  # with check=True: what would change
    retired: list[str] = field(default_factory=list)  # files rai no longer ships


def managed_paths() -> list[str]:
    paths = ["opencode.jsonc", ".githooks/post-commit", "AGENTS.md"]
    for folder in (".opencode/agents", ".opencode/commands"):
        found = (TEMPLATE_DIR / folder).glob("*.md")
        paths += sorted(f"{folder}/{p.name}" for p in found)
    return paths


def _is_agent(rel: str) -> bool:
    return rel.startswith(".opencode/agents/")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body_sha(rel: str, text: str) -> str:
    """Hash without the model line (agents), so a settings-only change to the
    model can be told apart from a change to the instructions."""
    return _sha(MODEL_LINE.sub("", text, count=1) if _is_agent(rel) else text)


def _records_path(project: Project) -> Path:
    return project.state_dir / "sync.json"


def _load_records(project: Project) -> dict[str, dict[str, str]]:
    path = _records_path(project)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8")).get("files", {})
    return {
        k: v if isinstance(v, dict) else {"sha": v, "body": ""} for k, v in raw.items()
    }


def _save_records(project: Project, records: dict[str, dict[str, str]]) -> None:
    project.state_dir.mkdir(parents=True, exist_ok=True)
    _records_path(project).write_text(
        json.dumps({"files": records}, indent=2), encoding="utf-8"
    )


def _with_model_line(text: str, rendered: str) -> str:
    wanted, found = MODEL_LINE.search(rendered), MODEL_LINE.search(text)
    if not wanted or not found:
        return text
    return text[: found.start()] + wanted.group(0) + text[found.end() :]


def sync_project(
    project: Project, force: bool = False, check: bool = False
) -> SyncResult:
    """Bring the generated project files up to date with the settings and templates.

    rai records what it last wrote to each file. A file you haven't touched since
    is simply updated. A file you've edited is left alone unless rai has something
    new for it; then the new version is written beside it as ``<name>.rai-new``
    (an agent file whose only change is its model line just gets that line
    updated). ``force`` overwrites regardless; ``check`` writes nothing.
    """
    values = template_values(project, project.name, project.title)
    records = _load_records(project)
    result = SyncResult()

    for rel in managed_paths():
        rendered = render((TEMPLATE_DIR / rel).read_text(encoding="utf-8"), values)
        new = {"sha": _sha(rendered), "body": _body_sha(rel, rendered)}
        path = project.root / rel
        alt = path.with_name(path.name + ".rai-new")
        current = path.read_text(encoding="utf-8") if path.exists() else None
        last = records.get(rel, {})

        if current == rendered:
            result.unchanged.append(rel)
            records[rel] = new
            if not check:
                alt.unlink(missing_ok=True)
            continue
        if current is None and last and rel.startswith(".opencode/") and not force:
            result.kept.append(rel)  # you deleted it on purpose
            continue

        untouched = current is None or last.get("sha") == _sha(current)
        moved = last.get("sha") != new["sha"]
        if untouched or force:
            action = "write"
        elif not moved:
            result.kept.append(rel)
            continue
        elif _is_agent(rel) and last.get("body") == new["body"]:
            action = "patch"  # only the model line changed
        else:
            action = "conflict"

        if check:
            result.drift.append(rel)
        elif action == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            if rel.startswith(".githooks"):
                path.chmod(0o755)
            alt.unlink(missing_ok=True)
            records[rel] = new
            result.written.append(rel)
        elif action == "patch":
            patched = _with_model_line(current or "", rendered)
            path.write_text(patched, encoding="utf-8")
            records[rel] = new
            result.written.append(rel)
        else:
            if _is_agent(rel):
                patched = _with_model_line(current or "", rendered)
                path.write_text(patched, encoding="utf-8")
            alt.write_text(rendered, encoding="utf-8")
            result.conflicts.append(rel)

    for rel in sorted(set(records) - set(managed_paths())):  # rai dropped these
        path = project.root / rel
        if not path.exists():
            if not check:
                records.pop(rel, None)
        elif _sha(path.read_text(encoding="utf-8")) == records[rel].get("sha"):
            if check:
                result.drift.append(rel)  # untouched, so it would be removed
            else:
                path.unlink()
                path.with_name(path.name + ".rai-new").unlink(missing_ok=True)
                records.pop(rel, None)
                result.retired.append(rel)
        else:
            result.kept.append(rel)  # you edited it, so it's yours now
            if not check:
                records.pop(rel, None)

    if not check:
        _save_records(project, records)
    return result
