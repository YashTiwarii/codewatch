"""
Pydantic v2 data models for codewatch.

This module is the single source of truth for all data structures passed
between pipeline stages. It contains zero logic — only schemas. Every field
decision that is non-obvious is documented inline.
"""

from typing import Literal

from pydantic import BaseModel


class FunctionMetrics(BaseModel):
    """Metrics for a single function or method extracted during parsing."""

    name: str
    lineno: int
    lines: int
    complexity: int
    parameters: int
    nesting_depth: int
    is_method: bool
    class_name: str | None  # None when the function is module-level


class ClassMetrics(BaseModel):
    """Metrics for a single class extracted during parsing."""

    name: str
    lineno: int
    lines: int
    methods: int
    fields: int

    # LCOM-HS (Henderson-Sellers 1996).
    # Formula: (1 / a) * sum((mf / M) for each field f)
    # where mf = number of methods NOT accessing field f,
    #       M  = total method count,
    #       a  = 1 - (1 / M)  [normalisation factor].
    # Range [0.0, 1.0]: 0.0 = perfect cohesion, 1.0 = no cohesion.
    # Set to 0.0 when M == 0 or fields == 0 (no evidence of incoherence).
    lcom: float

    efferent_coupling: int  # count of external classes this class imports
    inheritance_depth: int


class FileProfile(BaseModel):
    """Complete structural profile of a single source file."""

    path: str           # absolute path on disk
    relative_path: str  # relative to repo root; used as the graph node key

    lines: int
    maintainability_index: float  # radon output, range 0–100

    # Local project imports only — stdlib and third-party are excluded.
    # Including them would couple every file that imports `os` to every other,
    # making the dependency graph meaningless as an architectural tool.
    imports: list[str]  # relative_paths of local project files imported

    functions: list[FunctionMetrics]
    classes: list[ClassMetrics]


class Finding(BaseModel):
    """A single detected code quality violation."""

    rule: str       # e.g. "GOD_CLASS", "CIRCULAR_DEP", "DUPLICATE_LOGIC"
    severity: Literal["high", "medium", "low"]
    file: str       # relative_path of the violating file
    target: str     # class name, function name, or file name
    metric_value: float  # the actual measured value
    threshold: float     # the configured threshold it violated
    affected_nodes: list[str]  # relative_paths within blast radius


class BlastRadiusEntry(BaseModel):
    """One node in the downstream blast radius of a violation."""

    node: str            # relative_path of the affected file
    distance: int        # hops from the violation source
    incoming_edges: int  # how many other files depend on this node


class Review(BaseModel):
    """
    Complete output of one codewatch review run.

    health_score is computed deterministically before the AI call and cannot
    be changed by it. This makes it reproducible and trustworthy as a CI gate.
    """

    mode: Literal["file", "pr", "repo"]
    findings: list[Finding]

    # Weighted function of finding count and severity, computed before AI.
    # AI receives this value but is explicitly instructed not to suggest changes.
    health_score: float  # 0–100

    blast_radius: list[BlastRadiusEntry]

    # AI-generated fields — populated by review.py in a single call.
    summary: str                    # 3–5 sentence architectural assessment
    explanations: dict[str, str]    # "{rule}:{target}" → explanation text
    confidence: float               # AI-reported confidence; drops on truncation or fallback

    skipped_semantic: bool  # True when the semantic.py stage was not run
