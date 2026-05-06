"""
rules.py — Deterministic code quality rule evaluation.

Consumes FileProfile objects and a dependency graph; emits Finding objects.
Zero AI involvement — every finding here is reproducible given the same input.
DUPLICATE_LOGIC detection lives in semantic.py, not here.

TYPE_CHECKING-gated imports are already excluded from graph edges by
parse.py, so all cycles detected via networkx are runtime circular deps.
"""

import fnmatch
import logging
import re

import networkx as nx

from .graph import blast_radius as _blast_radius
from .models import FileProfile, Finding

logger = logging.getLogger(__name__)

# File basenames that are entrypoints by convention and have no internal callers.
# Flagging these as dead code produces immediate false positives on every project.
_ENTRYPOINT_BASENAMES = frozenset({
    "__init__.py",
    "__main__.py",
    "main.py",
    "manage.py",
    "app.py",
    "application.py",
    "wsgi.py",
    "asgi.py",
    "cli.py",
    "server.py",
    "setup.py",
    "conftest.py",
})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_rules(
    profiles: list[FileProfile],
    graph: nx.DiGraph,
    thresholds: dict,
    custom_rules: list[str],
    blast_depth: int,
    dead_code_enabled: bool = False,
) -> list[Finding]:
    """Run all deterministic rules and return the combined list of findings.

    Blast radius is computed once per file and reused across all rules that
    apply to that file. Custom arch rules are parsed once before the loop.
    """
    findings: list[Finding] = []

    parsed_arch_rules = _parse_arch_rules(custom_rules)

    # Pre-compute blast radius for every file to avoid redundant BFS calls.
    blast_map: dict[str, list[str]] = {
        p.relative_path: [
            e.node for e in _blast_radius(graph, p.relative_path, blast_depth)
        ]
        for p in profiles
    }

    for profile in profiles:
        br = blast_map[profile.relative_path]
        findings.extend(_check_functions(profile, thresholds, br))
        findings.extend(_check_classes(profile, thresholds, br))
        findings.extend(_check_arch_violations(profile, parsed_arch_rules, br))

    findings.extend(_check_circular_deps(graph))

    if dead_code_enabled:
        findings.extend(_check_dead_code(profiles, graph))

    return findings


# ---------------------------------------------------------------------------
# Function-level rules
# ---------------------------------------------------------------------------


def _check_functions(
    profile: FileProfile,
    thresholds: dict,
    blast_nodes: list[str],
) -> list[Finding]:
    """Evaluate HIGH_COMPLEXITY, LONG_FUNCTION, LONG_PARAM_LIST, DEEP_NESTING."""
    findings: list[Finding] = []
    cc_thresh = thresholds.get("cyclomatic_complexity", 10)
    fn_thresh = thresholds.get("function_lines", 50)
    param_thresh = thresholds.get("parameters", 4)
    nest_thresh = thresholds.get("nesting_depth", 3)

    for func in profile.functions:
        if func.complexity > cc_thresh:
            findings.append(
                Finding(
                    rule="HIGH_COMPLEXITY",
                    severity="high",
                    file=profile.relative_path,
                    target=func.name,
                    metric_value=float(func.complexity),
                    threshold=float(cc_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        if func.lines > fn_thresh:
            findings.append(
                Finding(
                    rule="LONG_FUNCTION",
                    severity="medium",
                    file=profile.relative_path,
                    target=func.name,
                    metric_value=float(func.lines),
                    threshold=float(fn_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        if func.parameters > param_thresh:
            findings.append(
                Finding(
                    rule="LONG_PARAM_LIST",
                    severity="low",
                    file=profile.relative_path,
                    target=func.name,
                    metric_value=float(func.parameters),
                    threshold=float(param_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        if func.nesting_depth > nest_thresh:
            findings.append(
                Finding(
                    rule="DEEP_NESTING",
                    severity="medium",
                    file=profile.relative_path,
                    target=func.name,
                    metric_value=float(func.nesting_depth),
                    threshold=float(nest_thresh),
                    affected_nodes=blast_nodes,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Class-level rules
# ---------------------------------------------------------------------------


def _check_classes(
    profile: FileProfile,
    thresholds: dict,
    blast_nodes: list[str],
) -> list[Finding]:
    """Evaluate GOD_CLASS, HIGH_COUPLING, LOW_COHESION, DEEP_INHERITANCE, LONG_CLASS."""
    findings: list[Finding] = []
    class_line_thresh = thresholds.get("class_lines", 300)
    method_thresh = thresholds.get("class_methods", 20)
    coupling_thresh = thresholds.get("efferent_coupling", 10)
    lcom_thresh = thresholds.get("lcom", 0.8)
    inherit_thresh = thresholds.get("inheritance_depth", 3)

    # GOD_CLASS thresholds are fixed per spec — not configurable individually.
    GOD_METHOD_MIN = 20
    GOD_LINE_MIN = 300
    GOD_COUPLING_MIN = 5
    GOD_LCOM_MIN = 0.8

    for cls in profile.classes:
        if cls.lines > class_line_thresh:
            findings.append(
                Finding(
                    rule="LONG_CLASS",
                    severity="medium",
                    file=profile.relative_path,
                    target=cls.name,
                    metric_value=float(cls.lines),
                    threshold=float(class_line_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        # GOD_CLASS requires all four conditions simultaneously.
        # Requiring all four reduces false positives — a large-but-cohesive
        # domain service is not a god class; a class with many methods but
        # low coupling may be a legitimate orchestrator.
        if (
            cls.methods > GOD_METHOD_MIN
            and cls.lines > GOD_LINE_MIN
            and cls.efferent_coupling > GOD_COUPLING_MIN
            and cls.lcom > GOD_LCOM_MIN
        ):
            findings.append(
                Finding(
                    rule="GOD_CLASS",
                    severity="high",
                    file=profile.relative_path,
                    target=cls.name,
                    # Report LCOM as the primary metric — it is the most
                    # diagnostic of the four conditions.
                    metric_value=cls.lcom,
                    threshold=GOD_LCOM_MIN,
                    affected_nodes=blast_nodes,
                )
            )

        if cls.efferent_coupling > coupling_thresh:
            findings.append(
                Finding(
                    rule="HIGH_COUPLING",
                    severity="high",
                    file=profile.relative_path,
                    target=cls.name,
                    metric_value=float(cls.efferent_coupling),
                    threshold=float(coupling_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        if cls.lcom > lcom_thresh:
            findings.append(
                Finding(
                    rule="LOW_COHESION",
                    severity="medium",
                    file=profile.relative_path,
                    target=cls.name,
                    metric_value=cls.lcom,
                    threshold=float(lcom_thresh),
                    affected_nodes=blast_nodes,
                )
            )

        if cls.inheritance_depth > inherit_thresh:
            findings.append(
                Finding(
                    rule="DEEP_INHERITANCE",
                    severity="medium",
                    file=profile.relative_path,
                    target=cls.name,
                    metric_value=float(cls.inheritance_depth),
                    threshold=float(inherit_thresh),
                    affected_nodes=blast_nodes,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Graph-level rules
# ---------------------------------------------------------------------------


def _check_circular_deps(graph: nx.DiGraph) -> list[Finding]:
    """Detect runtime circular imports using networkx simple_cycles.

    simple_cycles finds all elementary cycles, not just one. Each unique
    cycle produces one Finding. TYPE_CHECKING imports are already absent
    from graph edges (stripped by parse.py), so every cycle found here
    represents a real runtime circular dependency.

    severity=medium per spec: TYPE_CHECKING + annotations make many
    cycle patterns harmless at runtime. Flagging them as HIGH would
    erode trust on well-structured codebases.
    """
    findings: list[Finding] = []
    seen_cycles: set[frozenset[str]] = set()

    for cycle in nx.simple_cycles(graph):
        key = frozenset(cycle)
        if key in seen_cycles:
            continue
        seen_cycles.add(key)

        cycle_nodes = sorted(cycle)
        source = cycle_nodes[0]
        # Cycle path: A → B → C → A
        path = " → ".join(cycle_nodes + [cycle_nodes[0]])

        findings.append(
            Finding(
                rule="CIRCULAR_DEP",
                severity="medium",
                file=source,
                target=path,
                metric_value=float(len(cycle)),
                threshold=1.0,
                affected_nodes=cycle_nodes,
            )
        )

    return findings


def _check_dead_code(
    profiles: list[FileProfile],
    graph: nx.DiGraph,
) -> list[Finding]:
    """Flag functions in files that have no incoming imports and are not entrypoints.

    Files with zero in-degree are invisible to static import analysis — no
    other file imports them. Functions inside such files that are not
    themselves exported are strong dead code candidates.

    Note: the 'never referenced in same file' check from the spec requires
    a call-graph pass not stored in FileProfile. This implementation flags
    all functions in zero-in-degree non-entrypoint files, which is
    conservative (may include functions called within the same file).
    False positives are expected; this rule is opt-in for that reason.
    """
    findings: list[Finding] = []

    for profile in profiles:
        if _is_entrypoint(profile.relative_path):
            continue
        if graph.in_degree(profile.relative_path) > 0:
            continue

        for func in profile.functions:
            findings.append(
                Finding(
                    rule="DEAD_CODE",
                    severity="low",
                    file=profile.relative_path,
                    target=func.name,
                    metric_value=0.0,
                    threshold=0.0,
                    affected_nodes=[],
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Architectural violation rules
# ---------------------------------------------------------------------------


def _parse_arch_rules(custom_rules: list[str]) -> list[tuple[str, str]]:
    """Parse custom rule strings into (source_glob, forbidden_glob) pairs.

    Accepted formats:
      "X must not import from Y"
      "no import from X to Y"
    Unrecognised formats are logged and skipped — never crash.
    """
    parsed: list[tuple[str, str]] = []
    for rule in custom_rules:
        result = _parse_one_arch_rule(rule)
        if result:
            parsed.append(result)
        else:
            logger.warning("invalid custom rule: %s — skipped", rule)
    return parsed


def _parse_one_arch_rule(rule: str) -> tuple[str, str] | None:
    """Return (source_glob, forbidden_glob) or None for unrecognised format."""
    rule = rule.strip()

    m = re.match(
        r"^(.+?)\s+must not import from\s+(.+)$", rule, re.IGNORECASE
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.match(
        r"^no import from\s+(.+?)\s+to\s+(.+)$", rule, re.IGNORECASE
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return None


def _check_arch_violations(
    profile: FileProfile,
    parsed_rules: list[tuple[str, str]],
    blast_nodes: list[str],
) -> list[Finding]:
    """Check one file against all parsed architectural rules."""
    findings: list[Finding] = []

    for source_glob, forbidden_glob in parsed_rules:
        if not fnmatch.fnmatch(profile.relative_path, source_glob):
            continue
        for imported in profile.imports:
            if fnmatch.fnmatch(imported, forbidden_glob):
                findings.append(
                    Finding(
                        rule="ARCH_VIOLATION",
                        severity="high",
                        file=profile.relative_path,
                        target=f"{profile.relative_path} → {imported}",
                        metric_value=1.0,
                        threshold=0.0,
                        affected_nodes=blast_nodes,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _is_entrypoint(relative_path: str) -> bool:
    """Return True if the file is a known entry point that has no internal callers."""
    import os
    return os.path.basename(relative_path) in _ENTRYPOINT_BASENAMES


def compute_health_score(findings: list[Finding]) -> float:
    """Compute a 0–100 health score from a list of findings.

    Weights per severity: high=15, medium=5, low=2.
    Computed here so review.py can call it before the AI call, keeping
    the score deterministic and AI-independent.
    """
    penalty = sum(
        15 if f.severity == "high" else 5 if f.severity == "medium" else 2
        for f in findings
    )
    return max(0.0, 100.0 - penalty)
