# codewatch

Structural code quality guardrail for Python. Combines AST parsing, dependency
graph analysis, and AI explanations to detect code quality violations — and
blocks your CI pipeline when it finds them.

The core philosophy: guardrails only work when they are **immediate, automatic,
and in the critical path**. codewatch is not a reporting tool — it is a
pipeline-blocking guardrail that makes the cost of bad structure visible at the
moment it is introduced, not months later when the blast radius has compounded.

---

## Why codewatch

Most code quality tools fracture the feedback loop. The person who writes bad
structure is not the person who pays the price, and not at the time they wrote
it. codewatch tightens that loop — detection is immediate, local, free, and in
the commit path.

**Structural detection is always deterministic.** Same input always produces
identical findings regardless of API availability or model version. AI only
explains — it never detects.

---

## Architecture

```
cli.py
  → parse.py      deterministic · radon + stdlib ast
  → graph.py      deterministic · networkx dependency graph
  → rules.py      deterministic · zero AI
  → semantic.py   reproducible  · local embeddings (optional)
  → review.py     AI called exactly once · explanations only
```

Each stage is independently runnable and testable without the AI layer.
If the AI provider is unavailable, findings and health score are always
returned — exit code reflects finding severity, never AI availability.

---

## Detected rules

| Rule | Severity | Description |
|---|---|---|
| `GOD_CLASS` | high | Class with high methods + lines + coupling + low cohesion |
| `HIGH_COMPLEXITY` | high | Cyclomatic complexity above threshold |
| `HIGH_COUPLING` | high | Class with too many external dependencies |
| `ARCH_VIOLATION` | high | Import violates a custom architectural rule |
| `CIRCULAR_DEP` | medium | Runtime circular import between modules |
| `LONG_FUNCTION` | medium | Function exceeds line threshold |
| `LONG_CLASS` | medium | Class exceeds line threshold |
| `LOW_COHESION` | medium | Class with low LCOM-HS score |
| `DEEP_INHERITANCE` | medium | Deep class hierarchy |
| `DEEP_NESTING` | medium | Control-flow nesting too deep |
| `DUPLICATE_LOGIC` | medium | Semantically similar functions (optional) |
| `LONG_PARAM_LIST` | low | Too many function parameters |
| `DEAD_CODE` | low | Internal function with no callers (opt-in) |

---

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
# Add your AI provider key to .env
```

### For duplicate detection (optional)

```bash
pip install sentence-transformers sqlite-vec
```

---

## Quick start

```bash
# Review a single file
codewatch review file src/mymodule.py

# Review all files changed in a PR
codewatch review pr changes.diff

# Review an entire repository
codewatch review repo .
```

### Example output

```
codewatch · repo mode · 9 files analysed

FINDINGS (6)
────────────────────────────────────────────────────────────
[HIGH]    HIGH_COMPLEXITY    _resolve_imports    codewatch/parse.py
         metric=15  threshold=10
         blast radius: 1 file

[HIGH]    GOD_CLASS          UserService         services/user.py
         metric=0.91  threshold=0.80
         blast radius: 8 files

[MEDIUM]  LONG_FUNCTION      _check_classes      codewatch/rules.py
         metric=97  threshold=50
         blast radius: 2 files

HEALTH SCORE: 42 / 100

SUMMARY (gemini-2.5-flash-lite)
────────────────────────────────
The codebase shows concentrated structural risk in the services layer...

EXPLANATIONS
────────────
HIGH_COMPLEXITY · _resolve_imports
  The function in codewatch/parse.py has a complexity of 15, significantly
  above the threshold of 10. High complexity here makes it prone to errors
  and difficult to maintain. Its impact on codewatch/cli.py indicates that
  changes could affect how the CLI handles file parsing and analysis.
```

---

## CI/CD integration

```yaml
# .github/workflows/quality.yml
- name: codewatch
  run: codewatch review repo . --fail-on high
```

Exit codes:
- `0` — clean, no findings at or above `--fail-on` severity
- `1` — findings found at or above `--fail-on` severity  
- `2` — tool error (bad path, config error) — **never** used for AI failures

---

## Configuration

`config.yaml` in the project root:

```yaml
thresholds:
  cyclomatic_complexity: 10
  function_lines: 50
  class_lines: 300
  lcom: 0.8
  efferent_coupling: 10

graph:
  blast_radius_depth: 3

ai:
  provider: "anthropic"       # anthropic | openai | ollama | gemini
  model: "claude-sonnet-4-6"
  api_key_env: "ANTHROPIC_API_KEY"

custom_rules:
  - "controllers/* must not import from repositories/*"
  - "no import from api/* to db/*"
```

### Supported AI providers

| Provider | Config value | Key env var |
|---|---|---|
| Anthropic (Claude) | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI (GPT / o-series) | `openai` | `OPENAI_API_KEY` |
| Google Gemini | `gemini` | `GEMINI_API_KEY` |
| Ollama (local) | `ollama` | — |

---

## Flags

```
--fail-on high|medium|low   Exit 1 if any finding at or above this severity
--json                      Output full JSON (Review model) instead of text
--skip-semantic             Skip duplicate detection (faster CI runs)
--depth <int>               Blast radius BFS depth (default: 3)
```

---

## Why AI is called once, not per finding

Per-finding calls cost O(N) API calls and produce redundant explanations for
related violations. One call with all findings plus the full dependency graph
produces better architectural synthesis, costs O(1), and enables cross-finding
pattern recognition. Individual explanations are slightly less granular — this
is the correct tradeoff.

---

## Known limitations

- **Python only** — multi-language support is v2 scope
- **Dynamic imports** — `importlib.import_module()`, `__import__()`, and plugin
  systems are invisible to AST analysis; document this in team onboarding
- **DUPLICATE_LOGIC reproducibility** — similarity scores near the threshold
  (±0.02) may flip across environments due to float nondeterminism in matmul.
  Pin `embedding_model_sha` in `config.yaml` for CI
- **DEAD_CODE** — disabled by default; too many false positives on
  framework-registered code (Flask routes, Celery tasks, Click commands).
  Enable with `dead_code: enabled: true`
- **Custom rules** — glob matching only; a proper DSL is v2 scope
- **Inheritance depth** — cross-file depth requires symbol resolution not
  yet implemented; current metric counts direct base classes only

---

## Project structure

```
codewatch/
├── codewatch/
│   ├── models.py     Pydantic schemas — zero logic
│   ├── parse.py      file → FileProfile (radon + ast)
│   ├── graph.py      FileProfile[] → nx.DiGraph, blast radius
│   ├── rules.py      deterministic rule evaluation, zero AI
│   ├── semantic.py   optional duplicate detection (local embeddings)
│   ├── review.py     AI provider abstraction, one call per review
│   └── cli.py        Click CLI — file | pr | repo modes
├── config.yaml
├── requirements.txt
└── .env.example
```
