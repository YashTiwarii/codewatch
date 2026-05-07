"""
cli.py — Click CLI for codewatch.

Provides three review modes: file, pr, repo.
stdout receives all output (human-readable or --json).
stderr receives all logs and warnings.

Exit codes:
  0  — clean, no findings at or above --fail-on severity
  1  — findings found at or above --fail-on severity
  2  — tool error (bad path, config error, unreadable diff)
       NOT used for AI errors — AI failure degrades gracefully.
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import click
import networkx as nx
from dotenv import load_dotenv

from .graph import build_graph, export_graph_html
from .models import FileProfile, Finding, Review
from .parse import parse_file
from .review import make_provider, run_review
from .rules import compute_health_score, run_rules
from .semantic import detect_duplicates

load_dotenv()

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="codewatch: %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
_CACHE_DIR = ".codewatch_cache"


# ---------------------------------------------------------------------------
# CLI structure
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """codewatch — structural code quality guardrail."""


@cli.group()
def review() -> None:
    """Run a structural review."""


@review.command("file")
@click.argument("path", type=click.Path(exists=True))
@click.option("--json", "output_json", is_flag=True, help="Output JSON instead of human-readable text.")
@click.option("--fail-on", type=click.Choice(["high", "medium", "low"]), default=None, help="Exit 1 if any finding at or above this severity.")
@click.option("--depth", type=int, default=None, help="Blast radius depth (overrides config).")
@click.option("--skip-semantic", is_flag=True, help="Skip duplicate detection.")
@click.option("--graph", "graph_flag", is_flag=True, default=False, help="Write dependency graph to codewatch_graph.html")
def review_file(path: str, output_json: bool, fail_on: str | None, depth: int | None, skip_semantic: bool, graph_flag: bool) -> None:
    """Review a single Python file."""
    config = _load_config()
    abs_path = os.path.abspath(path)
    # When --graph is requested, scan the whole repo so the dependency graph
    # has full context for blast radius. Use the inferred project root so we
    # scan FastAPI (or whatever project owns the file) rather than CWD.
    if graph_flag:
        repo_root = _infer_project_root(abs_path)
        py_paths = _collect_py_files(repo_root)
    else:
        repo_root = os.getcwd()
        py_paths = [abs_path]
    _run(
        mode="file",
        py_paths=py_paths,
        repo_root=repo_root,
        config=config,
        output_json=output_json,
        fail_on=fail_on,
        skip_semantic=skip_semantic,
        depth=depth,
        use_cache=False,
        changed_files=None,
        graph_flag=graph_flag,
        target_file=abs_path if graph_flag else None,
    )


@review.command("pr")
@click.argument("diff", type=click.Path(exists=True))
@click.option("--json", "output_json", is_flag=True)
@click.option("--fail-on", type=click.Choice(["high", "medium", "low"]), default=None)
@click.option("--depth", type=int, default=None)
@click.option("--skip-semantic", is_flag=True)
@click.option("--graph", "graph_flag", is_flag=True, default=False, help="Write dependency graph to codewatch_graph.html")
def review_pr(diff: str, output_json: bool, fail_on: str | None, depth: int | None, skip_semantic: bool, graph_flag: bool) -> None:
    """Review files changed in a unified diff.

    Parses the whole repo for a complete dependency graph so that blast
    radius is accurate, then filters findings to changed files only.
    """
    repo_root = os.getcwd()
    changed_files = _files_from_diff(diff, repo_root)
    if not changed_files:
        click.echo("No changed Python files found in diff.", err=True)
        sys.exit(0)

    config = _load_config()
    py_paths = _collect_py_files(repo_root)
    _run(
        mode="pr",
        py_paths=py_paths,
        repo_root=repo_root,
        config=config,
        output_json=output_json,
        fail_on=fail_on,
        skip_semantic=skip_semantic,
        depth=depth,
        use_cache=True,
        changed_files=set(changed_files),
        graph_flag=graph_flag,
    )


@review.command("repo")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--json", "output_json", is_flag=True)
@click.option("--fail-on", type=click.Choice(["high", "medium", "low"]), default=None)
@click.option("--depth", type=int, default=None)
@click.option("--skip-semantic", is_flag=True)
@click.option("--skip-tests", is_flag=True, help="Exclude test files and test directories from analysis.")
@click.option("--graph", "graph_flag", is_flag=True, default=False, help="Write dependency graph to codewatch_graph.html")
def review_repo(path: str, output_json: bool, fail_on: str | None, depth: int | None, skip_semantic: bool, skip_tests: bool, graph_flag: bool) -> None:
    """Review an entire repository."""
    repo_root = os.path.abspath(path)
    config = _load_config()
    py_paths = _collect_py_files(repo_root, skip_tests=skip_tests)
    if not py_paths:
        click.echo(f"No Python files found in {repo_root}", err=True)
        sys.exit(2)
    _run(
        mode="repo",
        py_paths=py_paths,
        repo_root=repo_root,
        config=config,
        output_json=output_json,
        fail_on=fail_on,
        skip_semantic=skip_semantic,
        depth=depth,
        use_cache=False,
        changed_files=None,
        graph_flag=graph_flag,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _run(
    mode: str,
    py_paths: list[str],
    repo_root: str,
    config: dict,
    output_json: bool,
    fail_on: str | None,
    skip_semantic: bool,
    depth: int | None,
    use_cache: bool,
    changed_files: set[str] | None,
    graph_flag: bool = False,
    target_file: str | None = None,
) -> None:
    """Execute the full analysis pipeline and print results."""
    if depth is not None:
        config.setdefault("graph", {})["blast_radius_depth"] = depth

    blast_depth = config.get("graph", {}).get("blast_radius_depth", 3)
    thresholds = config.get("thresholds", {})
    custom_rules = config.get("custom_rules", [])
    dead_code_enabled = config.get("dead_code", {}).get("enabled", False)

    # Parse — with cache for PR mode.
    cache = _load_cache(repo_root) if use_cache else {}
    profiles, parse_findings, cache = _parse_files(py_paths, repo_root, cache)
    if use_cache:
        _save_cache(repo_root, cache)

    if not profiles and not parse_findings:
        click.echo("No files could be parsed.", err=True)
        sys.exit(2)

    # Build dependency graph from all profiles (needed for accurate blast radius).
    graph = build_graph(profiles)

    # In file mode with --graph, findings run only on the target file while the
    # full graph is kept for blast radius and visualization.
    if target_file is not None:
        target_rel = os.path.relpath(target_file, repo_root)
        analysis_profiles = [p for p in profiles if p.relative_path == target_rel]
    else:
        analysis_profiles = profiles

    # Detect structural violations.
    all_findings: list[Finding] = list(parse_findings)
    all_findings += run_rules(
        profiles=analysis_profiles,
        graph=graph,
        thresholds=thresholds,
        custom_rules=custom_rules,
        blast_depth=blast_depth,
        dead_code_enabled=dead_code_enabled,
    )

    # Semantic duplicate detection (optional).
    skipped_semantic = skip_semantic
    if not skip_semantic:
        sem_cfg = config.get("semantic", {})
        sem_findings = detect_duplicates(
            profiles=profiles,
            threshold=thresholds.get("duplicate_similarity", 0.85),
            model_name=sem_cfg.get("embedding_model", "all-MiniLM-L6-v2"),
        )
        if sem_findings is None:
            skipped_semantic = True
        else:
            all_findings += sem_findings

    # In PR mode, filter findings to changed files only.
    if changed_files is not None:
        all_findings = [
            f for f in all_findings
            if _finding_involves_changed(f, changed_files)
        ]

    # AI explanation — always runs, degrades gracefully on failure.
    rev = run_review(
        mode=mode,
        profiles=profiles,
        findings=all_findings,
        graph=graph,
        config=config,
        skipped_semantic=skipped_semantic,
    )

    # Output.
    if output_json:
        click.echo(rev.model_dump_json(indent=2))
    else:
        click.echo(_format_human(rev, config, len(profiles)))

    # Graph export.
    if graph_flag:
        try:
            if mode == "repo":
                export_graph_html(graph, rev.findings)
            elif mode == "file" and profiles:
                if target_file is not None:
                    source_file = os.path.relpath(target_file, repo_root)
                else:
                    source_file = profiles[0].relative_path
                subgraph_nodes = [source_file] + [e.node for e in rev.blast_radius]
                export_graph_html(graph, rev.findings, subgraph_nodes=subgraph_nodes)
            elif mode == "pr" and changed_files:
                changed_rel = [os.path.relpath(f, repo_root) for f in changed_files]
                blast_nodes = [e.node for e in rev.blast_radius]
                subgraph_nodes = list(set(changed_rel) | set(blast_nodes))
                export_graph_html(graph, rev.findings, subgraph_nodes=subgraph_nodes)
        except Exception as e:
            click.echo(f"warning: graph generation failed: {e}", err=True)

    # Exit code.
    if fail_on and _has_violation(rev.findings, fail_on):
        sys.exit(1)
    sys.exit(0)


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------


_PROJECT_MARKERS = {"setup.py", "pyproject.toml", "setup.cfg", ".git", "Pipfile"}


def _infer_project_root(file_path: str) -> str:
    """Walk up from file_path's directory to find the project root.

    Stops at the first directory containing a known project marker.
    Falls back to the file's parent directory if none is found.
    """
    directory = os.path.dirname(file_path)
    current = directory
    while True:
        if any(os.path.exists(os.path.join(current, m)) for m in _PROJECT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return directory
        current = parent


_TEST_DIRS = {"tests", "test", "testing", "e2e", "integration", "functional"}
_TEST_FILE_PREFIXES = ("test_",)
_TEST_FILE_SUFFIXES = ("_test.py",)


def _collect_py_files(root: str, skip_tests: bool = False) -> list[str]:
    """Recursively collect all .py files under root, skipping hidden dirs."""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d not in ("__pycache__", "node_modules", ".venv", "venv")
            and not (skip_tests and d in _TEST_DIRS)
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            if skip_tests and (
                any(name.startswith(p) for p in _TEST_FILE_PREFIXES)
                or name.endswith(_TEST_FILE_SUFFIXES)
            ):
                continue
            paths.append(os.path.join(dirpath, name))
    return paths


def _files_from_diff(diff_path: str, repo_root: str) -> list[str]:
    """Extract relative paths of changed Python files from a unified diff.

    Reads '+++ b/<path>' lines. Returns absolute paths within repo_root.
    """
    changed: list[str] = []
    try:
        with open(diff_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("+++ b/") and line.rstrip().endswith(".py"):
                    rel = line[6:].strip()
                    if rel != "/dev/null":
                        abs_path = os.path.join(repo_root, rel)
                        if os.path.isfile(abs_path):
                            changed.append(abs_path)
    except OSError as exc:
        click.echo(f"error reading diff: {exc}", err=True)
        sys.exit(2)
    return changed


def _finding_involves_changed(finding: Finding, changed: set[str]) -> bool:
    """True if the finding's file or any affected node is in the changed set."""
    if finding.file in changed:
        return True
    # Include graph-level findings (e.g. CIRCULAR_DEP) where a changed file
    # is part of the cycle even if it's not the 'file' field.
    return bool(set(finding.affected_nodes) & changed)


# ---------------------------------------------------------------------------
# Parsing with PR-mode cache
# ---------------------------------------------------------------------------


def _parse_files(
    paths: list[str],
    repo_root: str,
    cache: dict,
) -> tuple[list[FileProfile], list[Finding], dict]:
    """Parse Python files, using cache for unchanged files.

    Cache keys are absolute paths. Cache entries are invalidated when the
    file's content hash changes — not on timestamp, to survive checkouts.
    """
    profiles: list[FileProfile] = []
    findings: list[Finding] = []

    for path in paths:
        h = _content_hash(path)
        cached = cache.get(path)

        if cached and cached.get("hash") == h:
            try:
                profiles.append(FileProfile.model_validate(cached["profile"]))
                continue
            except Exception:
                pass  # stale or corrupt cache entry — fall through to re-parse

        profile, parse_findings = parse_file(path, repo_root)
        findings.extend(parse_findings)
        if profile:
            profiles.append(profile)
            cache[path] = {"hash": h, "profile": profile.model_dump()}

    return profiles, findings, cache


def _content_hash(path: str) -> str:
    """Return a short SHA-256 hex digest of a file's contents."""
    try:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest()[:16]
    except OSError:
        return ""


def _load_cache(repo_root: str) -> dict:
    cache_path = Path(repo_root) / _CACHE_DIR / "profiles.json"
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(repo_root: str, cache: dict) -> None:
    cache_dir = Path(repo_root) / _CACHE_DIR
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "profiles.json").write_text(
        json.dumps(cache, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_config(path: str = "config.yaml") -> dict:
    """Load config.yaml, falling back to built-in defaults on missing file."""
    try:
        import yaml  # type: ignore

        with open(path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        # Merge with defaults so missing keys never cause KeyError downstream.
        defaults = _default_config()
        return _deep_merge(defaults, loaded)
    except FileNotFoundError:
        return _default_config()
    except Exception as exc:
        click.echo(f"config error: {exc}", err=True)
        sys.exit(2)


def _default_config() -> dict:
    return {
        "thresholds": {
            "cyclomatic_complexity": 10,
            "function_lines": 50,
            "class_lines": 300,
            "class_methods": 20,
            "parameters": 4,
            "nesting_depth": 3,
            "efferent_coupling": 10,
            "lcom": 0.8,
            "duplicate_similarity": 0.85,
        },
        "graph": {"blast_radius_depth": 3},
        "ai": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "semantic": {
            "embedding_model": "all-MiniLM-L6-v2",
            "embedding_model_sha": "",
        },
        "dead_code": {"enabled": False},
        "custom_rules": [],
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def _has_violation(findings: list[Finding], fail_on: str) -> bool:
    """Return True if any finding is at or above the fail_on severity."""
    threshold = _SEVERITY_RANK.get(fail_on, 0)
    return any(_SEVERITY_RANK.get(f.severity, 0) >= threshold for f in findings)


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _format_human(review: Review, config: dict, file_count: int) -> str:
    """Render the Review as human-readable terminal output."""
    model = config.get("ai", {}).get("model", "unknown")
    sep = "─" * 60

    lines: list[str] = [
        f"codewatch · {review.mode} mode · {file_count} files analysed",
        "",
    ]

    if not review.findings:
        lines += [f"No findings.  Health score: 100 / 100"]
        return "\n".join(lines)

    sev_order = {"high": 0, "medium": 1, "low": 2}
    sorted_findings = sorted(
        review.findings,
        key=lambda f: (sev_order.get(f.severity, 3), f.file, f.rule),
    )

    lines += [f"FINDINGS ({len(review.findings)})", sep]
    for f in sorted_findings:
        sev_tag = f"[{f.severity.upper()}]"
        lines.append(f"{sev_tag:<9} {f.rule:<18} {f.target}    {f.file}")
        lines.append(
            f"         metric={f.metric_value:.3g}  threshold={f.threshold:.3g}"
        )
        if f.affected_nodes:
            n = len(f.affected_nodes)
            lines.append(f"         blast radius: {n} file{'s' if n != 1 else ''}")
        lines.append("")

    lines += [
        f"HEALTH SCORE: {review.health_score:.0f} / 100",
        "",
        f"SUMMARY ({model})",
        "─" * (len(model) + 10),
        review.summary,
        "",
    ]

    if review.explanations:
        lines += ["EXPLANATIONS", "─" * 12]
        for key, text in review.explanations.items():
            # Key is "RULE:target" — split for display as "RULE · target"
            rule_part, _, target_part = key.partition(":")
            header = f"{rule_part} · {target_part}" if target_part else key
            lines.append(header)
            for expl_line in text.splitlines():
                lines.append(f"  {expl_line}")
            lines.append("")

    if review.skipped_semantic:
        lines.append(
            "(DUPLICATE_LOGIC detection skipped — "
            "install sentence-transformers or remove --skip-semantic)"
        )

    if review.confidence < 0.5 and review.confidence > 0.0:
        lines.append(
            f"(AI confidence: {review.confidence:.2f} — "
            "prompt was truncated or response was malformed)"
        )

    return "\n".join(lines)
