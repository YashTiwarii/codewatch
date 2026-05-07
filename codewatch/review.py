"""
review.py — AI explanation layer.

Calls the configured AI provider exactly once per review. AI only explains
pre-computed findings — it never detects them. Detection was done by rules.py
and semantic.py; this stage adds human-readable architectural context.

AI failure degrades gracefully: findings and health score are always present
in the returned Review regardless of AI availability. The exit code reflects
finding severity, never AI status. This preserves the invariant that detection
is independent of the AI layer.
"""

import json
import logging
import os
import re
import sys
from typing import Protocol

import networkx as nx

from .graph import blast_radius as _blast_radius
from .models import BlastRadiusEntry, FileProfile, Finding, Review
from .rules import compute_health_score

logger = logging.getLogger(__name__)

# Verbatim from spec — do not paraphrase.
_SYSTEM_PROMPT = (
    "You are a code architecture reviewer. You receive pre-computed structural "
    "findings from a deterministic analysis tool. Your job is to explain WHY "
    "each finding matters in terms of future maintenance cost, blast radius "
    "risk, and refactoring effort. Never claim to have detected these issues "
    "yourself — detection was done by a separate deterministic system. Never "
    "invent findings not in the input. Never suggest the health score should "
    "be different. Each explanation must be under 150 words. Be specific to "
    "the file names, class names, and metrics provided.\n\n"
    "Respond with raw JSON only — no markdown, no code fences, no commentary. "
    "Output must start with { and end with }. Exactly match this schema:\n"
    '{"summary": "<3-5 sentence architectural assessment>", '
    '"explanations": {"RULE:target": "<explanation under 150 words>"}, '
    '"confidence": 0.9}'
)

# Leave headroom for the response in each provider's context window.
_TOKEN_BUDGETS: dict[str, int] = {
    "anthropic": 180_000,
    "openai": 110_000,
    "ollama": 6_000,
    "gemini": 900_000,
}

# Conservative chars-per-token approximation used for pre-call budget checks.
# Avoids network round-trips for token counting; sufficient for truncation.
_CHARS_PER_TOKEN = 3.5


# ---------------------------------------------------------------------------
# Provider protocol and implementations
# ---------------------------------------------------------------------------


class AIProvider(Protocol):
    """Contract every provider must satisfy."""

    def complete(self, system: str, user: str) -> str: ...

    @property
    def token_budget(self) -> int: ...


class AnthropicProvider:
    """Wraps the anthropic SDK for Claude models."""

    def __init__(self, model: str, api_key_env: str) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError("anthropic package not installed") from exc
        api_key = os.getenv(api_key_env)
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        """Send one request and return the text of the first content block."""
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text

    @property
    def token_budget(self) -> int:
        return _TOKEN_BUDGETS["anthropic"]


class OpenAIProvider:
    """Wraps the openai SDK for GPT / o-series models."""

    def __init__(self, model: str, api_key_env: str) -> None:
        try:
            import openai  # type: ignore
        except ImportError as exc:
            raise RuntimeError("openai package not installed") from exc
        api_key = os.getenv(api_key_env)
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=2048,
        )
        return resp.choices[0].message.content or ""

    @property
    def token_budget(self) -> int:
        return _TOKEN_BUDGETS["openai"]


class OllamaProvider:
    """Wraps a locally running Ollama server via httpx."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434") -> None:
        try:
            import httpx  # type: ignore
            self._httpx = httpx
        except ImportError as exc:
            raise RuntimeError("httpx package not installed") from exc
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._budget: int | None = None

    def complete(self, system: str, user: str) -> str:
        resp = self._httpx.post(
            f"{self._base_url}/api/chat",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    @property
    def token_budget(self) -> int:
        if self._budget is not None:
            return self._budget
        try:
            resp = self._httpx.post(
                f"{self._base_url}/api/show",
                json={"name": self._model},
                timeout=10.0,
            )
            self._budget = resp.json().get("context_length", _TOKEN_BUDGETS["ollama"])
        except Exception:
            self._budget = _TOKEN_BUDGETS["ollama"]
        return self._budget


class GeminiProvider:
    """Calls the Gemini REST API directly using httpx.

    Uses the native generateContent endpoint rather than the OpenAI-compat
    surface — AI Studio keys (AIzaSy…) work reliably here without needing
    billing enabled on the Google Cloud project.
    """

    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str, api_key_env: str) -> None:
        try:
            import httpx  # type: ignore
            self._httpx = httpx
        except ImportError as exc:
            raise RuntimeError("httpx package not installed") from exc
        self._api_key = os.getenv(api_key_env) or ""
        self._model = model

    def complete(self, system: str, user: str) -> str:
        url = f"{self._BASE}/{self._model}:generateContent?key={self._api_key}"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "maxOutputTokens": 8192,
                # Enforce JSON output at the API level — prevents the model from
                # wrapping the response in markdown code fences regardless of
                # how the system prompt is worded.
                "responseMimeType": "application/json",
            },
        }
        resp = self._httpx.post(url, json=body, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return next(p["text"] for p in parts if "text" in p)

    @property
    def token_budget(self) -> int:
        return 900_000


def make_provider(config: dict) -> AIProvider:
    """Instantiate the AI provider specified in config['ai']."""
    ai = config.get("ai", {})
    name = ai.get("provider", "anthropic")
    model = ai.get("model", "claude-sonnet-4-6")
    key_env = ai.get("api_key_env", "ANTHROPIC_API_KEY")

    if name == "anthropic":
        return AnthropicProvider(model=model, api_key_env=key_env)
    if name == "openai":
        return OpenAIProvider(model=model, api_key_env=key_env)
    if name == "ollama":
        return OllamaProvider(model=model)
    if name == "gemini":
        return GeminiProvider(model=model, api_key_env=key_env)
    raise ValueError(f"unknown AI provider: {name!r}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_review(
    mode: str,
    profiles: list[FileProfile],
    findings: list[Finding],
    graph: nx.DiGraph,
    config: dict,
    skipped_semantic: bool,
) -> Review:
    """Assemble the Review: compute health score, call AI once, return result.

    health_score is computed before the AI call and cannot be changed by it.
    AI failure produces a valid Review with empty explanations and confidence=0.0;
    the caller's exit code is based on finding severity, not AI availability.
    """
    health_score = compute_health_score(findings, len(profiles))
    depth = config.get("graph", {}).get("blast_radius_depth", 3)
    blast_entries = _aggregate_blast(findings, graph, depth)
    profile_map = {p.relative_path: p for p in profiles}

    try:
        provider = make_provider(config)
        user_prompt, high_truncated = _build_user_prompt(
            mode=mode,
            health_score=health_score,
            findings=findings,
            graph=graph,
            profile_map=profile_map,
        )
        raw = provider.complete(_SYSTEM_PROMPT, user_prompt)
        summary, explanations, confidence = _parse_ai_response(raw)
        if high_truncated:
            # HIGH findings were cut — AI missed the worst issues.
            confidence = min(confidence, 0.6)
    except Exception as exc:
        logger.error("AI call failed: %s", exc)
        print(f"AI unavailable: {exc}", file=sys.stderr)
        summary = f"AI unavailable: {exc}"
        explanations = {}
        confidence = 0.0

    return Review(
        mode=mode,
        findings=findings,
        health_score=health_score,
        blast_radius=blast_entries,
        summary=summary,
        explanations=explanations,
        confidence=confidence,
        skipped_semantic=skipped_semantic,
    )


# ---------------------------------------------------------------------------
# Prompt assembly with tiered truncation
# ---------------------------------------------------------------------------


_AI_FINDING_CAP = 30


def _build_user_prompt(
    mode: str,
    health_score: float,
    findings: list[Finding],
    graph: nx.DiGraph,
    profile_map: dict[str, FileProfile],
) -> tuple[str, bool]:
    """Build the AI user prompt, capped at the top 30 findings by priority.

    Priority order:
      1. HIGH findings, sorted by blast radius size descending.
      2. MEDIUM findings, sorted by blast radius size descending.
      3. LOW findings are never sent — their rule name is self-explanatory.

    Returns (prompt, high_truncated) where high_truncated is True when not
    all HIGH findings fit within the cap (worst issues unseen by the AI).
    """
    def _blast_size(f: Finding) -> int:
        return len(f.affected_nodes)

    high = sorted(
        (f for f in findings if f.severity == "high"),
        key=_blast_size,
        reverse=True,
    )
    medium = sorted(
        (f for f in findings if f.severity == "medium"),
        key=_blast_size,
        reverse=True,
    )
    low = [f for f in findings if f.severity == "low"]

    eligible = high + medium
    ai_findings = eligible[:_AI_FINDING_CAP]

    total = len(findings)
    shown = len(ai_findings)
    high_truncated = len(ai_findings) < len(high)
    omitted = total - shown

    if high_truncated:
        logger.warning(
            "prompt: only %d of %d HIGH findings sent to AI — worst issues may be under-reported",
            sum(1 for f in ai_findings if f.severity == "high"),
            len(high),
        )

    lines: list[str] = [
        f"MODE: {mode}",
        f"HEALTH SCORE: {health_score:.1f}/100",
        "",
    ]
    if omitted > 0:
        lines.append(
            f"Note: showing top {shown} of {total} findings by severity"
            + (" (LOW findings omitted — self-explanatory)" if low and shown == len(eligible) else "")
        )
    lines.append(f"FINDINGS ({shown} shown):")

    for f in ai_findings:
        lines.append(
            f"[{f.severity.upper()}] {f.rule}"
            f" | target: {f.target}"
            f" | file: {f.file}"
            f" | metric={f.metric_value:.3g}"
            f" | threshold={f.threshold:.3g}"
        )
        if f.affected_nodes:
            preview = f.affected_nodes[:5]
            extra = len(f.affected_nodes) - 5
            lines.append(
                "  blast radius: "
                + ", ".join(preview)
                + (f" +{extra} more" if extra > 0 else "")
            )

    lines += [
        "",
        "DEPENDENCY GRAPH:",
        f"  nodes={graph.number_of_nodes()} edges={graph.number_of_edges()}",
    ]
    top5 = sorted(graph.nodes, key=lambda n: graph.in_degree(n), reverse=True)[:5]
    if top5:
        lines.append(
            "  most depended-on: "
            + ", ".join(f"{n} ({graph.in_degree(n)} in)" for n in top5)
        )

    involved = sorted({f.file for f in ai_findings})
    if involved:
        lines += ["", "FILE CONTEXT:"]
        for rel in involved:
            p = profile_map.get(rel)
            if not p:
                continue
            cls_names = [c.name for c in p.classes]
            fn_names = [fn.name for fn in p.functions[:8]]
            overflow = len(p.functions) - 8
            lines.append(
                f"  {rel}: MI={p.maintainability_index:.1f}"
                + (f" classes=[{', '.join(cls_names)}]" if cls_names else "")
                + (
                    f" functions=[{', '.join(fn_names)}"
                    + ("..." if overflow > 0 else "")
                    + "]"
                    if fn_names
                    else ""
                )
            )

    lines += [
        "",
        "Return one explanations entry per finding.",
        'Key format: "RULE:target" using the exact rule and target strings above.',
    ]
    return "\n".join(lines), high_truncated


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_ai_response(text: str) -> tuple[str, dict[str, str], float]:
    """Parse the AI response JSON. Falls back to text extraction on failure.

    Degraded output is signalled by confidence=0.1 so callers can distinguish
    it from a clean parse.
    """
    # Try direct parse first.
    data = _try_json(text)

    # Try extracting from a markdown code fence if direct parse failed.
    # Use [\s\S]+? instead of \{.*?\} — the nested-braces form stops at the
    # first inner } and misparses any JSON with nested objects.
    if data is None:
        match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if match:
            data = _try_json(match.group(1).strip())

    if data is not None:
        summary = str(data.get("summary") or "AI response incomplete")
        raw_expl = data.get("explanations")
        explanations = (
            {str(k): str(v) for k, v in raw_expl.items()}
            if isinstance(raw_expl, dict)
            else {}
        )
        # Missing fields degrade confidence per spec gap-resolution protocol.
        degraded = "summary" not in data or "explanations" not in data
        confidence = 0.1 if degraded else float(data.get("confidence") or 0.0)
        return summary, explanations, confidence

    # Final fallback: first non-empty paragraph as summary.
    logger.warning("AI response was not valid JSON; using fallback parser")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    summary = paragraphs[0] if paragraphs else "AI response incomplete"
    return summary, {}, 0.1


def _try_json(text: str) -> dict | None:
    """Return parsed dict if text is valid JSON, else None."""
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Aggregate blast radius
# ---------------------------------------------------------------------------


def _aggregate_blast(
    findings: list[Finding],
    graph: nx.DiGraph,
    depth: int,
) -> list[BlastRadiusEntry]:
    """Union the blast radius of all finding source files.

    When a node appears at different distances from different violations,
    the minimum distance is kept — it is the closest any violation is to it.
    """
    node_map: dict[str, BlastRadiusEntry] = {}
    for f in findings:
        for entry in _blast_radius(graph, f.file, depth):
            existing = node_map.get(entry.node)
            if existing is None or entry.distance < existing.distance:
                node_map[entry.node] = entry
    return sorted(node_map.values(), key=lambda e: (e.distance, e.node))
