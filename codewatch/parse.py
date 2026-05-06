"""
parse.py — Convert a Python source file into a FileProfile.

Uses radon for metrics radon already computes (cyclomatic complexity, LOC,
maintainability index). Uses stdlib ast only for what radon does not expose:
LCOM-HS, nesting depth, import resolution, field detection, parameter counts.

Hard rule: if radon computes it, never recompute it.
"""

import ast
import logging
import os
from pathlib import Path

from radon.complexity import cc_visit
from radon.metrics import mi_visit
from radon.raw import analyze

from .models import ClassMetrics, FileProfile, Finding, FunctionMetrics

logger = logging.getLogger(__name__)

# Control-flow nodes that contribute one level of nesting depth.
_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.With,
    ast.Try,
    ast.ExceptHandler,
    ast.AsyncFor,
    ast.AsyncWith,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_file(path: str, repo_root: str) -> tuple[FileProfile | None, list[Finding]]:
    """Parse a single Python source file into a FileProfile.

    Returns ``(profile, [])`` on success.
    Returns ``(None, [PARSE_ERROR finding])`` if the file cannot be read or
    parsed. Never raises.
    """
    abs_path = os.path.abspath(path)
    rel_path = _relative_path(abs_path, repo_root)

    try:
        source = Path(abs_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("read error: %s — %s", abs_path, exc)
        return None, [_error_finding(rel_path)]

    try:
        tree = ast.parse(source, filename=abs_path)
    except SyntaxError as exc:
        logger.warning("parse error: %s — %s", abs_path, exc)
        return None, [_error_finding(rel_path)]

    raw = analyze(source)
    mi = mi_visit(source, multi=True)
    cc_results = cc_visit(source)
    imported_names = _all_imported_names(tree)

    profile = FileProfile(
        path=abs_path,
        relative_path=rel_path,
        lines=raw.loc,
        maintainability_index=float(mi),
        imports=_resolve_imports(tree, repo_root, abs_path),
        functions=_parse_functions(tree, cc_results),
        classes=_parse_classes(tree, imported_names),
    )
    return profile, []


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


def _relative_path(abs_path: str, repo_root: str) -> str:
    """Compute repo-relative path; fall back to basename if outside root."""
    try:
        return str(Path(abs_path).relative_to(repo_root))
    except ValueError:
        return os.path.basename(abs_path)


def _error_finding(rel_path: str) -> Finding:
    """Produce a PARSE_ERROR Finding for a file that could not be parsed."""
    return Finding(
        rule="PARSE_ERROR",
        severity="low",
        file=rel_path,
        target=rel_path,
        metric_value=0.0,
        threshold=0.0,
        affected_nodes=[],
    )


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------


def _type_checking_node_ids(tree: ast.AST) -> set[int]:
    """Return Python object ids of AST nodes inside ``if TYPE_CHECKING:`` blocks.

    Handles both the bare ``TYPE_CHECKING`` name and the
    ``typing.TYPE_CHECKING`` attribute form. These imports are type-only
    and must not become dependency graph edges.
    """
    blocked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            for child in ast.walk(node):
                blocked.add(id(child))
    return blocked


def _resolve_imports(tree: ast.Module, repo_root: str, file_path: str) -> list[str]:
    """Return repo-relative paths of local project files imported by this file.

    Excludes stdlib, third-party, and TYPE_CHECKING-gated imports.
    Unresolvable imports are logged at DEBUG level and silently skipped.
    """
    blocked = _type_checking_node_ids(tree)
    repo = Path(repo_root)
    current = Path(file_path)
    seen: dict[str, None] = {}  # insertion-order dedup

    for node in ast.walk(tree):
        if id(node) in blocked:
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_dotted(alias.name, repo, 0, current)
                if resolved:
                    seen[resolved] = None
                else:
                    logger.debug("unresolved import: %s in %s", alias.name, file_path)

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level or 0

            if not module and level > 0:
                # `from . import foo` — each alias name is itself a submodule.
                for alias in node.names:
                    resolved = _resolve_dotted(alias.name, repo, level, current)
                    if resolved:
                        seen[resolved] = None
                    else:
                        logger.debug(
                            "unresolved import: %s in %s", alias.name, file_path
                        )
            else:
                resolved = _resolve_dotted(module, repo, level, current)
                if resolved:
                    seen[resolved] = None
                elif module:
                    logger.debug("unresolved import: %s in %s", module, file_path)

    return list(seen)


def _resolve_dotted(
    module: str, repo: Path, level: int, current_file: Path
) -> str | None:
    """Resolve a dotted module string to a repo-relative path, or None.

    For relative imports (level > 0) resolution is anchored to the current
    file's package. For absolute imports it is anchored to the repo root.
    Checks ``foo.py`` before ``foo/__init__.py``.
    """
    if level > 0:
        base = current_file.parent
        for _ in range(level - 1):
            base = base.parent
        candidate_base = base / module.replace(".", "/") if module else base
    else:
        candidate_base = repo / module.replace(".", "/")

    for candidate in (
        candidate_base.with_suffix(".py"),
        candidate_base / "__init__.py",
    ):
        if candidate.is_file():
            try:
                return str(candidate.relative_to(repo))
            except ValueError:
                return None  # resolved outside repo root — treat as external

    return None


def _all_imported_names(tree: ast.Module) -> set[str]:
    """Collect every name introduced by import statements in the module.

    Includes TYPE_CHECKING-gated names because they represent compile-time
    coupling. Used downstream to compute efferent coupling per class.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    return names


# ---------------------------------------------------------------------------
# Function metrics
# ---------------------------------------------------------------------------


def _parse_functions(tree: ast.Module, cc_results: list) -> list[FunctionMetrics]:
    """Extract FunctionMetrics for every function and method in the file.

    Complexity values come from radon; all other fields come from the AST.
    cc_visit returns top-level Function blocks and Class blocks whose
    ``.methods`` attribute holds method-level Function blocks.
    """
    cc_map: dict[tuple[str, int], int] = {}
    for block in cc_results:
        if hasattr(block, "is_method"):  # radon Function / Method block
            cc_map[(block.name, block.lineno)] = block.complexity
        if hasattr(block, "methods"):  # radon Class block
            for method in block.methods:
                cc_map[(method.name, method.lineno)] = method.complexity

    metrics: list[FunctionMetrics] = []
    _collect_functions(tree, class_name=None, cc_map=cc_map, out=metrics)
    return metrics


def _collect_functions(
    node: ast.AST,
    class_name: str | None,
    cc_map: dict[tuple[str, int], int],
    out: list[FunctionMetrics],
) -> None:
    """Recursively collect FunctionMetrics, tracking the enclosing class name."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect_functions(child, child.name, cc_map, out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = child.end_lineno if child.end_lineno else child.lineno
            out.append(
                FunctionMetrics(
                    name=child.name,
                    lineno=child.lineno,
                    lines=end - child.lineno + 1,
                    complexity=cc_map.get((child.name, child.lineno), 1),
                    parameters=_count_params(child),
                    nesting_depth=_max_nesting(child),
                    is_method=class_name is not None,
                    class_name=class_name,
                )
            )
            # Recurse into the function body with no class context — nested
            # functions inside methods are not themselves methods.
            _collect_functions(child, None, cc_map, out)
        else:
            _collect_functions(child, class_name, cc_map, out)


def _count_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count parameters, excluding the implicit self / cls receiver."""
    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    count = len(positional) + len(args.kwonlyargs)
    if args.vararg:
        count += 1
    if args.kwarg:
        count += 1
    return count


def _max_nesting(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return the maximum control-flow nesting depth inside a function.

    Does not descend into nested function or class definitions — those have
    their own nesting scope independent of the enclosing function.
    """

    def _walk(node: ast.AST, depth: int) -> int:
        max_d = depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            new_depth = depth + 1 if isinstance(child, _NESTING_NODES) else depth
            max_d = max(max_d, _walk(child, new_depth))
        return max_d

    return _walk(func, 0)


# ---------------------------------------------------------------------------
# Class metrics
# ---------------------------------------------------------------------------


def _parse_classes(tree: ast.Module, imported_names: set[str]) -> list[ClassMetrics]:
    """Extract ClassMetrics for every class definition in the file."""
    metrics: list[ClassMetrics] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        fields = _instance_fields(node)
        end = node.end_lineno if node.end_lineno else node.lineno
        metrics.append(
            ClassMetrics(
                name=node.name,
                lineno=node.lineno,
                lines=end - node.lineno + 1,
                methods=len(methods),
                fields=len(fields),
                lcom=_compute_lcom(methods, fields),
                efferent_coupling=_efferent_coupling(node, imported_names),
                inheritance_depth=_inheritance_depth(node),
            )
        )
    return metrics


def _instance_fields(class_node: ast.ClassDef) -> set[str]:
    """Collect instance field names from ``self.x = …`` assignments in any method."""
    fields: set[str] = set()
    for node in ast.walk(class_node):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.target:
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                fields.add(target.attr)
    return fields


def _fields_accessed_by(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    all_fields: set[str],
) -> set[str]:
    """Return the subset of ``all_fields`` that ``method`` reads or writes."""
    accessed: set[str] = set()
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in all_fields
        ):
            accessed.add(node.attr)
    return accessed


def _compute_lcom(
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    fields: set[str],
) -> float:
    """Compute normalised LCOM-HS in [0.0, 1.0].

    Formula: sum(mf for f in fields) / (a * M * F)
    where mf = methods NOT accessing field f,
          M  = total methods,
          a  = 1 - 1/M  (Henderson-Sellers normalisation factor),
          F  = total instance fields.

    Dividing by F ensures the fully-disjoint case (each method accesses
    exactly one unique field) yields 1.0. The raw HS formula omits this
    division, giving a range of [0, 2] that exceeds 1.0 for small M; we
    normalise and clamp to keep the contract [0.0, 1.0].

    Returns 0.0 when M <= 1 or F == 0 — insufficient evidence of incoherence.
    """
    M = len(methods)
    F = len(fields)

    if M <= 1 or F == 0:
        return 0.0

    a = 1.0 - 1.0 / M
    method_field_sets = [_fields_accessed_by(m, fields) for m in methods]

    not_accessed = sum(
        sum(1 for mfs in method_field_sets if field not in mfs)
        for field in fields
    )

    raw = not_accessed / (a * M * F)
    return max(0.0, min(1.0, raw))


def _efferent_coupling(class_node: ast.ClassDef, imported_names: set[str]) -> int:
    """Count distinct imported names referenced within the class body.

    Proxy for efferent coupling: how many external types or modules this
    class concretely depends on. Detects usage via Name nodes and the root
    of Attribute chains (``module.Foo`` → counts ``module``).
    """
    used: set[str] = set()
    for node in ast.walk(class_node):
        if isinstance(node, ast.Name) and node.id in imported_names:
            used.add(node.id)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in imported_names
        ):
            used.add(node.value.id)
    return len(used)


def _inheritance_depth(class_node: ast.ClassDef) -> int:
    """Return number of explicit base classes as a proxy for inheritance depth.

    True multi-level depth requires cross-file resolution (v2 scope).
    Excludes ``object`` — it is the implicit base for all Python 3 classes.
    """
    return sum(
        1
        for base in class_node.bases
        if not (isinstance(base, ast.Name) and base.id == "object")
    )
