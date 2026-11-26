"""The `nfh` command line. Run `nfh --help` or `nfh <command> --help`."""

from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import httpx
import typer

from .config import (
    HARNESS_ROOT,
    MODEL_STAGES,
    OPENCODE_GO_API_KEY_ENVS,
    OPENCODE_GO_MODELS_URL,
    PROJECTS_DIR,
    TEMPLATE_DIR,
    ConfigError,
    Project,
    describe,
    load_env,
    load_harness_settings,
    load_project,
    ollama_host,
    opencode_base_url,
    opencode_go_key,
    parse_ref,
)
from .notes import read_notes
from .render import render, sync_project, template_values

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="nfh: a local research and writing harness. Your prose stays yours.",
)
_state: dict[str, Path | None] = {"project": None}

ProjectOpt = Annotated[
    Path | None,
    typer.Option(
        "--project",
        "-p",
        help="Project folder. Default: the project containing the current directory.",
    ),
]


@app.callback()
def main(project: ProjectOpt = None) -> None:
    _state["project"] = project


def _project() -> Project:
    try:
        project = load_project(_state["project"])
    except (FileNotFoundError, ConfigError) as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    project.ensure_dirs()
    logging.basicConfig(
        filename=project.logs_dir / "nfh.log",
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return project


def say(message: str) -> None:
    typer.echo(message)


OPENCODE_INSTALL = (
    "OpenCode 2 is needed: the project files use its config format. Install it with\n"
    "     curl -fsSL https://opencode.ai/v2/install | bash\n"
    "     or: npm install -g @opencode/cli\n"
    "     (the opencode.ai/install script and the opencode-ai package install\n"
    "     OpenCode 1, which rejects these projects; remove it first)"
)


def _opencode_info() -> tuple[bool, int | None, str]:
    """Whether `opencode` is on PATH, its major version, and the version text."""
    binary = shutil.which("opencode")
    if binary is None:
        return False, None, ""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, None, ""
    match = re.search(r"(\d+)\.\d+\.\d+", proc.stdout + proc.stderr)
    if match is None:
        return True, None, ""
    return True, int(match.group(1)), match.group(0)


# new -------------------------------------------------------------------------
@app.command()
def new(
    name: Annotated[
        str, typer.Argument(help="Folder name, e.g. platform-labour-review")
    ],
    title: Annotated[str, typer.Option(help="Human-readable title")] = "",
    collection: Annotated[str, typer.Option(help="Zotero collection to draw on")] = "",
    tag: Annotated[
        list[str] | None, typer.Option(help="Zotero tag to draw on (repeatable)")
    ] = None,
    path: Annotated[
        Path | None, typer.Option(help="Create the project here instead of projects/")
    ] = None,
    git: Annotated[bool, typer.Option(help="Initialise a git repository")] = True,
) -> None:
    """Create a new project from the template."""
    target = (path or PROJECTS_DIR / name).resolve()
    if target.exists() and any(target.iterdir()):
        typer.secho(f"{target} already exists and isn't empty.", fg="red")
        raise typer.Exit(1)
    try:
        settings = load_harness_settings()
    except ConfigError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    values = template_values(
        settings,
        name=name,
        title=title or name.replace("-", " ").capitalize(),
        collection=collection,
        tags=tag or [],
    )
    for src in TEMPLATE_DIR.rglob("*"):
        rel = src.relative_to(TEMPLATE_DIR)
        dest = target / rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render(src.read_text(encoding="utf-8"), values), "utf-8")
        if rel.parts[0] == ".githooks":
            dest.chmod(0o755)
    project = load_project(target)
    project.ensure_dirs()
    sync_project(project)  # records the generated files, so later syncs are safe
    if git and shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=target, check=False)
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"], cwd=target, check=False
        )
    typer.secho(f"Created {target}", fg="green")
    say("Next: edit project.md and nfh.toml, then run `nfh refresh` inside it.")


# doctor ----------------------------------------------------------------------
def _ok(label: str, good: bool, fix: str = "") -> bool:
    mark = typer.style("ok  ", fg="green") if good else typer.style("FIX ", fg="red")
    typer.echo(f"{mark} {label}" + ("" if good or not fix else f"\n     {fix}"))
    return good


@app.command()
def doctor() -> None:
    """Check every dependency and say what to fix."""
    load_env()
    try:
        project: Project | None = load_project(_state["project"])
        settings = project
    except FileNotFoundError:
        project = None
        settings = None
    except ConfigError as exc:
        typer.secho(f"Settings problem: {exc}", fg="red")
        raise typer.Exit(1) from exc
    try:
        settings = settings or load_harness_settings()
    except ConfigError as exc:
        typer.secho(f"Settings problem: {exc}", fg="red")
        raise typer.Exit(1) from exc
    host = ollama_host(settings)
    refs = {
        stage: parse_ref(getattr(settings.models, stage), field=f"[models] {stage}")
        for stage in MODEL_STAGES
    }
    local = [
        (stage, ref) for stage, ref in refs.items() if ref.provider != "opencode-go"
    ]
    remote = [
        (stage, ref) for stage, ref in refs.items() if ref.provider == "opencode-go"
    ]
    source = (
        "this project's settings"
        if project
        else "the harness settings (config.toml, config.local.toml)"
    )
    typer.echo(f"Using {source}.")
    if local:
        typer.echo(f"Ollama at {host}")
    if os.environ.get("OLLAMA_HOST"):
        typer.secho(
            "Note: OLLAMA_HOST is set in your shell. nfh ignores it (it is for the "
            "`ollama` command); use [ollama] host in config.toml or NFH_OLLAMA_HOST.",
            fg="yellow",
        )
    all_ok = True
    if local:
        try:
            tags = httpx.get(f"{host}/api/tags", timeout=5).json()
            names = {m["name"] for m in tags.get("models", [])}
            all_ok &= _ok(f"Ollama at {host}", True)
            for stage, ref in local:
                fix = f"ollama pull {ref.model}"
                if names and ref.model not in names:
                    fix += "\n     the server has: " + ", ".join(sorted(names))
                    close = difflib.get_close_matches(ref.model, sorted(names), n=1)
                    if close:
                        fix += f"\n     closest: {close[0]}"
                    fix += f"\n     or fix [models] {stage} in config.toml"
                all_ok &= _ok(f"{stage} model {ref}", ref.model in names, fix)
        except (httpx.HTTPError, ValueError):
            all_ok &= _ok(
                f"Ollama at {host}",
                False,
                "Start it, or check your port forward and [ollama] host in config.toml",
            )
    if remote:
        key = opencode_go_key()
        all_ok &= _ok(
            "OpenCode Go key set",
            bool(key),
            "Add "
            + " or ".join(OPENCODE_GO_API_KEY_ENVS)
            + f" to {HARNESS_ROOT / '.env'}",
        )
        if key:
            try:
                resp = httpx.get(
                    OPENCODE_GO_MODELS_URL,
                    headers={"Authorization": f"Bearer {key}", "User-Agent": "nfh/0.1"},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data if isinstance(data, list) else (
                    data.get("data") or data.get("models") or []
                )
                available = {
                    m.get("id") for m in items if isinstance(m, dict)
                }
                for stage, ref in remote:
                    all_ok &= _ok(
                        f"{stage} model {ref}",
                        ref.model in available,
                        "check the model ID; `opencode models` lists the "
                        "account's models",
                    )
            except (httpx.HTTPError, ValueError) as exc:
                all_ok &= _ok(
                    "OpenCode Go model list",
                    False,
                    f"couldn't read {OPENCODE_GO_MODELS_URL}: {exc}",
                )
    from .zotero import Zotero

    if project:
        drift = sync_project(project, check=True).drift
        all_ok &= _ok(
            "opencode.jsonc, agent models and git hook match the settings",
            not drift,
            f"out of date: {', '.join(drift)}; run `nfh sync`",
        )
        all_ok &= _ok(
            "Zotero local API",
            Zotero(project.zotero).ping(),
            "Open Zotero; Settings > Advanced > allow other applications",
        )
        storage = project.zotero.storage_dir
        all_ok &= _ok(
            f"Zotero storage at {storage}",
            storage.exists(),
            "Set [zotero] data_dir in nfh.toml",
        )
    all_ok &= _ok("pandoc", bool(shutil.which("pandoc")), "sudo apt install pandoc")
    import sqlite3

    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        vec_ok = True
    except Exception:  # noqa: BLE001
        vec_ok = False
    all_ok &= _ok(
        "sqlite-vec loads",
        vec_ok,
        "Use a uv-managed Python: uv python install 3.12 && uv sync",
    )
    from importlib.metadata import distributions

    cuda_pkgs = sorted(
        d.metadata["Name"]
        for d in distributions()
        if (d.metadata["Name"] or "").lower().startswith(("nvidia-", "onnxruntime-gpu"))
    )
    all_ok &= _ok(
        "no CUDA packages installed locally",
        not cuda_pkgs,
        f"found {', '.join(cuda_pkgs[:4])}...; run `uv sync` to remove them",
    )
    found, major, version = _opencode_info()
    if not found:
        all_ok &= _ok("opencode", False, OPENCODE_INSTALL)
    elif major == 2:
        all_ok &= _ok(f"opencode {version} (OpenCode 2)", True)
    else:
        all_ok &= _ok(
            f"opencode {version or '?'} is not OpenCode 2", False, OPENCODE_INSTALL
        )
    for tool, fix in (
        ("uvx", "uvx ships with uv"),
        ("git", "sudo apt install git"),
    ):
        all_ok &= _ok(tool, bool(shutil.which(tool)), fix)
    for key in ("TINYFISH_API_KEY", "OPENALEX_API_KEY", "UNPAYWALL_EMAIL"):
        all_ok &= _ok(
            f"{key} set",
            bool(os.environ.get(key)),
            f"Add {key} to {HARNESS_ROOT / '.env'}",
        )
    if not project:
        say("(Run inside a project, or pass --project, to check Zotero too.)")
    raise typer.Exit(0 if all_ok else 1)


# sync / config ---------------------------------------------------------------
@app.command()
def sync(
    check: Annotated[
        bool, typer.Option(help="Only report what is out of date")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="Overwrite generated files you've edited by hand")
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all", help="Every project in projects/")
    ] = False,
) -> None:
    """Re-render opencode.jsonc, agent models and the git hook from the settings."""
    if all_projects:
        roots = sorted(p.parent for p in PROJECTS_DIR.glob("*/nfh.toml"))
        if not roots:
            say("No projects in projects/.")
            return
    else:
        roots = [_project().root]
    failed = False
    for root in roots:
        try:
            project = load_project(root)
        except (FileNotFoundError, ConfigError) as exc:
            typer.secho(f"{root.name}: {exc}", fg="red")
            failed = True
            continue
        result = sync_project(project, force=force, check=check)
        if check:
            if result.drift:
                typer.secho(
                    f"{project.name}: out of date: {', '.join(result.drift)}",
                    fg="yellow",
                )
                failed = True
            else:
                say(f"{project.name}: up to date")
            continue
        for rel in result.written:
            say(f"{project.name}: updated {rel}")
        for rel in result.retired:
            say(f"{project.name}: removed {rel} (nfh no longer ships it)")
        for rel in result.conflicts:
            typer.secho(
                f"{project.name}: {rel} was edited by hand, so it was left alone; "
                f"the new version is in {rel}.nfh-new (or use --force)",
                fg="yellow",
            )
            failed = True
        if result.kept:
            say(f"{project.name}: kept your edits to {', '.join(result.kept)}")
        if not (result.written or result.conflicts or result.retired):
            say(f"{project.name}: already up to date")
    raise typer.Exit(1 if failed else 0)


@app.command(name="config")
def show_config() -> None:
    """Show every setting in force and which file set it."""
    try:
        project = load_project(_state["project"])
        scope = f"project {project.name}"
    except FileNotFoundError:
        project = load_harness_settings()
        scope = "the harness (no project here)"
    except ConfigError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    say(f"Settings for {scope}\n")
    for section, key, value, source in describe(project):
        say(f"[{section}] {key} = {value!r}   <- {source}")
    say(f"\nnfh reaches Ollama at {ollama_host(project)}")
    say(f"OpenCode reaches it at {opencode_base_url(project)}")


# ingest ----------------------------------------------------------------------
@app.command()
def ingest(
    tag: Annotated[
        list[str] | None,
        typer.Option(
            help="Only items with this Zotero tag (repeatable). This run only."
        ),
    ] = None,
    match: Annotated[
        str | None, typer.Option(help="With several --tag: any (default) or all")
    ] = None,
    collection: Annotated[
        str | None, typer.Option(help="Use this Zotero collection for this run")
    ] = None,
    all_items: Annotated[
        bool, typer.Option("--all", help="Whole Zotero library, this run only")
    ] = False,
    local: Annotated[bool, typer.Option(help="Ingest files in sources-local/")] = False,
    dry_run: Annotated[bool, typer.Option(help="List what would be ingested")] = False,
    reembed: Annotated[
        bool, typer.Option(help="Re-embed every chunk with the configured model")
    ] = False,
    pdf_engine: Annotated[str, typer.Option(help="docling or pymupdf")] = "docling",
) -> None:
    """Index sources from Zotero (configured scope, or --tag/--collection)."""
    from .ingest import ingest_local, ingest_zotero
    from .ingest import reembed as do_reembed
    from .zotero import ZoteroError

    project = _project()
    if reembed:
        say(f"Re-embedded {do_reembed(project, say=say)} chunks.")
        return
    if match and match not in {"any", "all"}:
        typer.secho("--match must be 'any' or 'all'", fg="red")
        raise typer.Exit(1)
    try:
        if local:
            report = ingest_local(
                project, dry_run=dry_run, pdf_engine=pdf_engine, say=say
            )
        else:
            report = ingest_zotero(
                project,
                collection=collection,
                tags=tag,
                tag_match=match,
                all_items=all_items,
                dry_run=dry_run,
                pdf_engine=pdf_engine,
                say=say,
            )
    except (ValueError, ZoteroError, RuntimeError) as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    _print_report(report, dry_run)


def _print_report(report, dry_run: bool) -> None:  # noqa: ANN001
    if dry_run:
        say(f"{len(report.planned)} item(s) in scope:")
        for line in report.planned:
            say(f"  {line}")
        return
    for label, items in (
        ("added", report.added),
        ("updated", report.updated),
        ("removed", report.removed),
    ):
        if items:
            say(f"{label}: {', '.join(items)}")
    say(f"unchanged: {len(report.unchanged)}")
    for old, new in report.renamed:
        say(f"citation key changed in Zotero: {old} -> {new} (card and note moved)")
    if report.generated_keys:
        typer.secho(
            f"{len(report.generated_keys)} item(s) have no citation key in Zotero, so "
            f"nfh made temporary ones: {', '.join(report.generated_keys)}. In Zotero, "
            "select them and choose Better BibTeX > Refresh BibTeX key, then rerun; "
            "nfh will follow the new keys.",
            fg="yellow",
        )
    if report.placeholder_keys:
        typer.secho(
            f"{len(report.placeholder_keys)} item(s) use placeholder citation keys: "
            f"{', '.join(report.placeholder_keys)}. In Zotero, select them and choose "
            "Better BibTeX > Refresh BibTeX key, then rerun; nfh will follow the new "
            "keys.",
            fg="yellow",
        )
    for key, why in report.skipped:
        typer.secho(f"skipped {key}: {why}", fg="yellow")
    for key, status, detail in report.flagged:
        typer.secho(f"flagged {key} [{status}]: {detail}", fg="yellow")


# inspect / status / check ----------------------------------------------------
@app.command()
def inspect(
    source: Annotated[str, typer.Argument(help="Cite key or source id")],
    limit: Annotated[int, typer.Option(help="Blocks to show (0 = all)")] = 60,
) -> None:
    """Print a source's extracted blocks with their locations."""
    from .store import Store, loc_label

    project = _project()
    store = Store(project)
    row = store.source(source)
    if row is None:
        typer.secho(f"No source {source!r}.", fg="red")
        raise typer.Exit(1)
    say(f"{row['cite_key']}: {row['title']} ({row['year']})  [{row['status']}]")
    if row["status_detail"]:
        say(f"  note: {row['status_detail']}")
    blocks = store.blocks(row["id"])
    say(f"  {len(blocks)} blocks\n")
    for b in blocks[: limit or None]:
        where = loc_label([b]) or f"seq {b['seq']}"
        text = b["text"] if len(b["text"]) < 300 else b["text"][:300] + " ..."
        say(f"[{where} | {b['kind']} | {b['heading_path'] or '-'}]\n  {text}")
    store.close()


@app.command()
def status() -> None:
    """Flagged sources, stale notes and the state of the claims ledger."""
    from .render import sync_project
    from .status import compute, embedding_problem

    project = _project()
    s = compute(project)
    say(f"{s.sources} sources indexed")
    for key, st, detail in s.flagged:
        typer.secho(f"  flagged {key} [{st}]: {detail}", fg="yellow")
    if s.stale_notes:
        typer.secho("Stale notes: " + ", ".join(s.stale_notes), fg="yellow")
        say("  run `nfh refresh`")
    else:
        say("All notes current.")
    c = s.claims
    say(f"Claims: {c.get('total', 0)} " + str(c.get("by_status", {})))
    say(f"Unresolved objections: {s.open_objections}; decisions: {s.decisions}")
    if s.unknown_citations:
        typer.secho(
            "Drafts cite keys that aren't in the index: "
            + ", ".join(s.unknown_citations)
            + " (a typo, an item outside the project's scope, or a key changed "
            "in Zotero)",
            fg="yellow",
        )
    drift = sync_project(project, check=True).drift
    if drift:
        typer.secho(
            "Generated files out of sync with the settings: "
            + ", ".join(drift)
            + "; run `nfh sync`",
            fg="yellow",
        )
    problem = embedding_problem(project)
    if problem:
        typer.secho(problem, fg="yellow")


@app.command()
def show(
    what: Annotated[
        str,
        typer.Argument(
            help="brief, corpus_map, manuscript_map, chapter_note, claims, "
            "objections, decisions, reading, sources, candidates or sessions"
        ),
    ],
    file: Annotated[str, typer.Option(help="A draft file (for chapter_note)")] = "",
    status: Annotated[
        str, typer.Option(help="For objections: open, will_address, answered, ...")
    ] = "",
    limit: Annotated[
        int, typer.Option(help="For sessions: how many, newest first (default 5)")
    ] = 0,
) -> None:
    """Print one of the project's internal notes. You never need to; you can."""
    say(read_notes(_project(), what, file, status, limit))


@app.command()
def check(
    files: Annotated[
        list[Path] | None, typer.Argument(help="Note files to check")
    ] = None,
) -> None:
    """Verify every cited quotation in reports/ and in source cards."""
    from .check import check_quotes

    project = _project()
    results = check_quotes(project, [f.resolve() for f in files] if files else None)
    bad = 0
    for r in results:
        colour = {"exact": "green", "near": "yellow", "elsewhere": "yellow"}.get(
            r.status, "red"
        )
        bad += r.status not in {"exact"}
        typer.secho(f"{r.status:9} {r.file}  [@{r.cite_key}, {r.location}]", fg=colour)
        typer.echo(f'          "{r.quote[:90]}"')
        if r.detail:
            typer.echo(f"          {r.detail}")
    say(f"{len(results)} quotations checked, {bad} need attention.")
    raise typer.Exit(1 if any(r.status == "not_found" for r in results) else 0)


# notes -----------------------------------------------------------------------
@app.command()
def cards(
    cite_keys: Annotated[
        list[str] | None, typer.Argument(help="Specific sources")
    ] = None,
    all_sources: Annotated[
        bool, typer.Option("--all", help="Rebuild every card")
    ] = False,
    diff: Annotated[str | None, typer.Option(help="Show changes to one card")] = None,
    notes_only: Annotated[
        bool,
        typer.Option(help="Only refresh project notes (e.g. after editing questions)"),
    ] = False,
) -> None:
    """Build or refresh source cards (default: only stale ones)."""
    from .cards import (
        build_card,
        build_project_note,
        card_diff,
        stale_project_notes,
        stale_sources,
    )
    from .store import Store

    project = _project()
    if diff:
        say(card_diff(project, diff))
        return
    store = Store(project)
    if notes_only:
        targets = stale_project_notes(project, store)
        for source in targets:
            say(f"{source['cite_key']}: project note")
            build_project_note(project, store, source, say=say)
        say(f"{len(targets)} project note(s) refreshed.")
        store.close()
        return
    if cite_keys:
        targets = [s for k in cite_keys if (s := store.source(k)) is not None]
    elif all_sources:
        targets = store.sources()
    else:
        targets = stale_sources(project, store)
    for n, source in enumerate(targets, start=1):
        say(f"[{n}/{len(targets)}] {source['cite_key']}")
        build_card(project, store, source, say=say)
    for source in stale_project_notes(project, store):
        build_project_note(project, store, source, say=say)
    say(f"{len(targets)} card(s) built.")
    store.close()


@app.command(name="map")
def corpus_map(
    rebuild: Annotated[bool, typer.Option(help="Rewrite every theme")] = False,
) -> None:
    """Update the corpus map from the cards."""
    from .corpus_map import build_map

    project = _project()
    try:
        build_map(project, say=say, rebuild=rebuild)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    say("Wrote .nfh/corpus/map.md")


@app.command()
def manuscript(
    file: Annotated[Path | None, typer.Argument(help="One draft file")] = None,
    force: Annotated[bool, typer.Option(help="Re-map even if unchanged")] = False,
) -> None:
    """Map changed draft files, extract claims, update the manuscript map."""
    from .manuscript import build_manuscript

    project = _project()
    done = build_manuscript(
        project, only=file.resolve() if file else None, force=force, say=say
    )
    say(f"{len(done)} draft file(s) mapped.")


@app.command()
def brief() -> None:
    """Regenerate the briefing every agent loads."""
    from .brief import build_brief

    build_brief(_project())
    say("Wrote .nfh/brief.md")


@app.command()
def refresh(
    ingest_sources: Annotated[
        bool, typer.Option("--ingest/--no-ingest", help="Ingest first")
    ] = True,
) -> None:
    """Ingest, then bring every stale note up to date, then rebuild the briefing."""
    from .brief import build_brief
    from .cards import (
        build_card,
        build_project_note,
        stale_project_notes,
        stale_sources,
    )
    from .corpus_map import build_map
    from .ingest import ingest_local, ingest_zotero
    from .manuscript import build_manuscript
    from .status import compute
    from .store import Store
    from .zotero import ZoteroError

    project = _project()
    if ingest_sources:
        if project.zotero.collection or project.zotero.tags:
            try:
                _print_report(ingest_zotero(project, say=say), False)
            except (ZoteroError, RuntimeError) as exc:
                typer.secho(f"Zotero ingest skipped: {exc}", fg="yellow")
        else:
            typer.secho("No Zotero scope in nfh.toml; skipping Zotero.", fg="yellow")
        if any(project.sources_local_dir.iterdir()):
            _print_report(ingest_local(project, say=say), False)
    # Refresh citations.json first so project notes see the latest draft uses.
    build_manuscript(project, say=say)
    store = Store(project)
    stale = stale_sources(project, store)
    for n, source in enumerate(stale, start=1):
        say(f"card [{n}/{len(stale)}] {source['cite_key']}")
        build_card(project, store, source, say=say)
    for source in stale_project_notes(project, store):
        say(f"project note {source['cite_key']}")
        build_project_note(project, store, source, say=say)
    store.close()
    if compute(project).corpus_map_stale:
        build_map(project, say=say)
    build_brief(project)
    say("Refresh complete.")


@app.command()
def selftest(
    slow: Annotated[
        bool, typer.Option(help="Include the slow tests (real pipeline, long context)")
    ] = False,
    agents: Annotated[
        bool, typer.Option(help="Include OpenCode agent runs (drafts guard, etc.)")
    ] = False,
    only: Annotated[
        str | None,
        typer.Option(help="Only tests matching this pytest -k expression, e.g. ollama"),
    ] = None,
) -> None:
    """Test your real setup: Ollama, Zotero, Docling, APIs, MCP servers, OpenCode.

    Pass --project (before the command) to also check that project's Zotero
    scope, references.bib and a real PDF.
    """
    import importlib.util
    import sys

    if importlib.util.find_spec("pytest") is None:
        typer.secho("pytest isn't installed; run `uv sync` (with dev deps).", fg="red")
        raise typer.Exit(1)
    args = [
        sys.executable,
        "-m",
        "pytest",
        str(HARNESS_ROOT / "tests" / "live"),
        "--live",
        "-ra",
        "-p",
        "no:cacheprovider",
    ]
    if slow:
        args.append("--slow")
    if agents:
        args.append("--agents")
    if only:
        args += ["-k", only]
    try:
        project = load_project(_state["project"])
        args += ["--live-project", str(project.root)]
        say(f"Checking project {project.root}")
    except FileNotFoundError:
        say("No project given: checking the setup with a scratch project only.")
    raise typer.Exit(subprocess.call(args, cwd=HARNESS_ROOT))


@app.command(name="open")
def open_session() -> None:
    """Start OpenCode in the project with the harness's .env loaded."""
    project = _project()
    load_env()
    found, major, version = _opencode_info()
    if not found or major != 2:
        what = "isn't on PATH" if not found else f"is version {version or '?'}"
        typer.secho(f"`opencode` {what}.", fg="red")
        typer.secho(OPENCODE_INSTALL.replace("\n     ", "\n  "), fg="red")
        raise typer.Exit(1)
    binary = shutil.which("opencode")
    if not project.brief_path.exists():
        from .brief import build_brief

        build_brief(project)
    os.chdir(project.root)
    # OpenCode takes its working directory from $PWD, which chdir doesn't update;
    # without this it would open in whatever folder you ran `nfh open` from.
    os.environ["PWD"] = str(project.root)
    os.execv(binary, [binary])


@app.command()
def serve() -> None:
    """Run the MCP server (OpenCode starts this; you don't need to)."""
    from .server import serve as run

    try:
        run(_state["project"])
    except (FileNotFoundError, ConfigError) as exc:
        typer.secho(str(exc), fg="red", err=True)  # OpenCode shows stderr
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
