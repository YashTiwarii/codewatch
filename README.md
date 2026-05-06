# codewatch

Structural code quality guardrail for Python. Combines AST parsing, dependency
graph analysis, and AI explanations to detect code quality violations, blocking
your CI pipeline when it finds them.

The core philosophy: guardrails only work when they are **immediate, automatic,
and in the critical path**. codewatch is not a reporting tool; it is a
pipeline-blocking guardrail that makes the cost of bad structure visible at the
moment it is introduced, not months later when the blast radius has compounded.

## Why codewatch

Most code quality tools fracture the feedback loop. The person who writes bad
structure is not the person who pays the price, and not at the time they wrote
it. codewatch tightens that loop so detection is immediate, local, free, and in
the commit path.

**Structural detection is always deterministic.** Same input always produces
identical findings regardless of API availability or model version. AI only
explains; it never detects.

## Architecture

```
cli.py
  -> parse.py      deterministic · radon + stdlib ast
  -> graph.py      deterministic · networkx dependency graph
  -> rules.py      deterministic · zero AI
  -> semantic.py   reproducible  · local embeddings (optional)
  -> review.py     AI called exactly once · explanations only
```

Each stage is independently runnable and testable without the AI layer.
If the AI provider is unavailable, findings and health score are always
returned. Exit code reflects finding severity, never AI availability.

## Detected rules

| Rule | Severity | Description |
|---|---|---|
| `GOD_CLASS` | high | Class with high methods, lines, coupling, and low cohesion |
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

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
# Add your Gemini API key to .env
```

### For duplicate detection (optional)

```bash
pip install sentence-transformers sqlite-vec
```

## Quick start

```bash
# Review a single file
codewatch review file src/mymodule.py

# Review all files changed in a PR
codewatch review pr changes.diff

# Review an entire repository
codewatch review repo .
```

### Output against this repository

```
codewatch · repo mode · 9 files analysed

FINDINGS (26)
────────────────────────────────────────────────────────────
[HIGH]    HIGH_COMPLEXITY    _run                    codewatch/cli.py
         metric=14  threshold=10

[HIGH]    HIGH_COMPLEXITY    _resolve_imports        codewatch/parse.py
         metric=15  threshold=10
         blast radius: 1 file

[HIGH]    HIGH_COMPLEXITY    _build_user_prompt      codewatch/review.py
         metric=12  threshold=10
         blast radius: 1 file

[HIGH]    HIGH_COMPLEXITY    _parse_ai_response      codewatch/review.py
         metric=13  threshold=10
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      _build_user_prompt      codewatch/review.py
         metric=125  threshold=50
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      _check_classes          codewatch/rules.py
         metric=97  threshold=50
         blast radius: 2 files

[LOW]     LONG_PARAM_LIST    _run                    codewatch/cli.py
         metric=10  threshold=4

HEALTH SCORE: 0 / 100

SUMMARY (gemini-2.5-flash-lite)
───────────────────────────────
The codebase exhibits several high-complexity functions and deep nesting,
particularly within codewatch/cli.py, codewatch/graph.py, and
codewatch/review.py. These issues, along with long functions and parameter
lists, increase the likelihood of bugs, make modifications difficult, and raise
the effort required for future refactoring.

EXPLANATIONS
────────────
HIGH · HIGH_COMPLEXITY:codewatch/parse.py:_resolve_imports
  The function has a complexity of 15, making it challenging to maintain and
  debug. Its reliance by codewatch/cli.py means errors or changes here have a
  broad impact, increasing refactoring effort and the potential blast radius.

HIGH · HIGH_COMPLEXITY:codewatch/cli.py:_run
  Complexity metric of 14 indicates this function is hard to understand and
  prone to defects. High complexity directly impacts the core CLI execution
  flow, making it a significant refactoring challenge.
```

## CI/CD integration

```yaml
# .github/workflows/quality.yml
- name: codewatch
  run: codewatch review repo . --fail-on high
```

Exit codes:

* `0` clean, no findings at or above `--fail-on` severity
* `1` findings found at or above `--fail-on` severity
* `2` tool error (bad path, config error), never raised for AI failures

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
  provider: "gemini"
  model: "gemini-2.5-flash-lite"
  api_key_env: "GEMINI_API_KEY"

custom_rules:
  - "controllers/* must not import from repositories/*"
  - "no import from api/* to db/*"
```

### Supported AI providers

| Provider | Config value | Key env var |
|---|---|---|
| Google Gemini | `gemini` | `GEMINI_API_KEY` |
| Anthropic (Claude) | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI (GPT) | `openai` | `OPENAI_API_KEY` |
| Ollama (local) | `ollama` | none required |

## Flags

```
--fail-on high|medium|low   Exit 1 if any finding at or above this severity
--json                      Output full JSON (Review model) instead of text
--skip-semantic             Skip duplicate detection (faster CI runs)
--depth <int>               Blast radius BFS depth (default: 3)
```

## Why AI is called once, not per finding

Per-finding calls cost O(N) API calls and produce redundant explanations for
related violations. One call with all findings plus the full dependency graph
produces better architectural synthesis, costs O(1), and enables cross-finding
pattern recognition. Individual explanations are slightly less granular; this
is the correct tradeoff.

## Known limitations

* Python only; multi-language support is planned for v2
* Dynamic imports via `importlib.import_module()` and plugin systems are
  invisible to AST analysis; document this in team onboarding
* DUPLICATE_LOGIC similarity scores near the threshold (+-0.02) may flip
  across environments due to float nondeterminism in matmul. Pin
  `embedding_model_sha` in `config.yaml` for CI
* DEAD_CODE is disabled by default due to false positives on framework
  registered code such as Flask routes, Celery tasks, and Click commands.
  Enable with `dead_code: enabled: true`
* Custom rules use glob matching only; a proper DSL is planned for v2
* Inheritance depth counts direct base classes only; cross-file depth
  requires symbol resolution not yet implemented

## Project structure

```
codewatch/
├── codewatch/
│   ├── models.py     Pydantic schemas, zero logic
│   ├── parse.py      file to FileProfile (radon + ast)
│   ├── graph.py      FileProfile list to nx.DiGraph, blast radius
│   ├── rules.py      deterministic rule evaluation, zero AI
│   ├── semantic.py   optional duplicate detection (local embeddings)
│   ├── review.py     AI provider abstraction, one call per review
│   └── cli.py        Click CLI, file and pr and repo modes
├── config.yaml
├── requirements.txt
└── .env.example
```
