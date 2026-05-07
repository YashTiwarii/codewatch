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
[HIGH]    HIGH_COMPLEXITY    _run    codewatch/cli.py
         metric=14  threshold=10

[HIGH]    HIGH_COMPLEXITY    _format_human    codewatch/cli.py
         metric=12  threshold=10

[HIGH]    HIGH_COMPLEXITY    _resolve_init_reexports    codewatch/graph.py
         metric=12  threshold=10
         blast radius: 3 files

[HIGH]    HIGH_COMPLEXITY    _resolve_imports    codewatch/parse.py
         metric=15  threshold=10
         blast radius: 1 file

[HIGH]    HIGH_COMPLEXITY    _build_user_prompt    codewatch/review.py
         metric=12  threshold=10
         blast radius: 1 file

[HIGH]    HIGH_COMPLEXITY    _parse_ai_response    codewatch/review.py
         metric=13  threshold=10
         blast radius: 1 file

[MEDIUM]  DEEP_NESTING       _files_from_diff    codewatch/cli.py
         metric=6  threshold=3

[MEDIUM]  DEEP_NESTING       _parse_files    codewatch/cli.py
         metric=4  threshold=3

[MEDIUM]  LONG_FUNCTION      _run    codewatch/cli.py
         metric=86  threshold=50

[MEDIUM]  LONG_FUNCTION      _format_human    codewatch/cli.py
         metric=65  threshold=50

[MEDIUM]  DEEP_NESTING       _resolve_init_reexports    codewatch/graph.py
         metric=4  threshold=3
         blast radius: 3 files

[MEDIUM]  DEEP_NESTING       _resolve_imports    codewatch/parse.py
         metric=6  threshold=3
         blast radius: 1 file

[MEDIUM]  DEEP_NESTING       _resolve_dotted    codewatch/parse.py
         metric=4  threshold=3
         blast radius: 1 file

[MEDIUM]  DEEP_NESTING       _all_imported_names    codewatch/parse.py
         metric=5  threshold=3
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      run_review    codewatch/review.py
         metric=51  threshold=50
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      _build_user_prompt    codewatch/review.py
         metric=125  threshold=50
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      _render    codewatch/review.py
         metric=80  threshold=50
         blast radius: 1 file

[MEDIUM]  LONG_FUNCTION      _check_functions    codewatch/rules.py
         metric=66  threshold=50
         blast radius: 2 files

[MEDIUM]  LONG_FUNCTION      _check_classes    codewatch/rules.py
         metric=97  threshold=50
         blast radius: 2 files

[LOW]     LONG_PARAM_LIST    review_file    codewatch/cli.py
         metric=5  threshold=4

[LOW]     LONG_PARAM_LIST    review_pr    codewatch/cli.py
         metric=5  threshold=4

[LOW]     LONG_PARAM_LIST    review_repo    codewatch/cli.py
         metric=5  threshold=4

[LOW]     LONG_PARAM_LIST    _run    codewatch/cli.py
         metric=10  threshold=4

[LOW]     LONG_PARAM_LIST    run_review    codewatch/review.py
         metric=6  threshold=4
         blast radius: 1 file

[LOW]     LONG_PARAM_LIST    _build_user_prompt    codewatch/review.py
         metric=6  threshold=4
         blast radius: 1 file

[LOW]     LONG_PARAM_LIST    run_rules    codewatch/rules.py
         metric=6  threshold=4
         blast radius: 2 files

HEALTH SCORE: 0 / 100

SUMMARY (gemini-2.5-flash-lite)
───────────────────────────────
The codebase exhibits several high-complexity functions and deep nesting,
particularly within the core logic of codewatch/cli.py, codewatch/graph.py,
and codewatch/review.py. These issues, along with long functions and parameter
lists, increase the likelihood of bugs, make modifications difficult, and raise
the effort required for future refactoring. Addressing these areas is crucial
for improving maintainability and reducing the risk of unintended consequences
when making changes.

EXPLANATIONS
────────────
HIGH · HIGH_COMPLEXITY:codewatch/graph.py:_resolve_init_reexports
  The HIGH_COMPLEXITY in `_resolve_init_reexports` (metric=12) suggests this
  function is difficult to understand and maintain. Its complexity increases
  the risk of introducing bugs. The blast radius to `codewatch/cli.py`,
  `codewatch/review.py`, and `codewatch/rules.py` means changes here could
  have widespread impact, making refactoring efforts more costly and
  time-consuming.

HIGH · HIGH_COMPLEXITY:codewatch/review.py:_build_user_prompt
  The complexity of `_build_user_prompt` (metric=12) in `codewatch/review.py`
  makes it hard to modify and debug. This increases maintenance costs. The
  direct dependency from `codewatch/cli.py` means that any issues or necessary
  changes in this function will directly affect the command-line interface,
  potentially leading to a larger blast radius for bugs and higher refactoring
  effort.

HIGH · HIGH_COMPLEXITY:codewatch/review.py:_parse_ai_response
  With a complexity metric of 13, `_parse_ai_response` in
  `codewatch/review.py` is prone to errors and difficult to refactor. Since
  `codewatch/cli.py` depends on it, bugs within this parsing logic could
  disrupt the CLI's functionality, increasing the blast radius and maintenance
  overhead.

HIGH · HIGH_COMPLEXITY:codewatch/cli.py:_run
  The `_run` function in `codewatch/cli.py` has a complexity metric of 14,
  indicating it is hard to understand and prone to defects. This high
  complexity directly impacts the core CLI execution flow, making it a
  significant refactoring challenge and increasing the blast radius for any
  introduced bugs.

HIGH · HIGH_COMPLEXITY:codewatch/cli.py:_format_human
  The complexity of `_format_human` (metric=12) in `codewatch/cli.py` suggests
  it may be difficult to maintain and extend. High complexity increases the
  chance of bugs and raises the effort needed for future modifications, directly
  impacting the user-facing output of the CLI.

HIGH · HIGH_COMPLEXITY:codewatch/parse.py:_resolve_imports
  The `_resolve_imports` function in `codewatch/parse.py` has a complexity of
  15, making it challenging to maintain and debug. Its reliance by
  `codewatch/cli.py` means that errors or necessary changes here have a broad
  impact, increasing refactoring effort and the potential blast radius for
  issues.

MEDIUM · DEEP_NESTING:codewatch/graph.py:_resolve_init_reexports
  The deep nesting (metric=4) in `_resolve_init_reexports` within
  `codewatch/graph.py` makes the logic harder to follow and increases the risk
  of errors. This directly impacts downstream modules like `codewatch/cli.py`,
  `codewatch/review.py`, and `codewatch/rules.py`, raising maintenance costs
  and refactoring effort due to the potential for cascading issues.

MEDIUM · LONG_FUNCTION:codewatch/rules.py:_check_functions
  The `_check_functions` function in `codewatch/rules.py` is excessively long
  (metric=66), which hinders readability and maintainability. Its use in
  `codewatch/cli.py` and `codewatch/review.py` means this lengthy code
  increases the blast radius for bugs and makes refactoring more complex and
  time-consuming.

MEDIUM · LONG_FUNCTION:codewatch/rules.py:_check_classes
  The `_check_classes` function in `codewatch/rules.py` has a length metric of
  97, making it very difficult to understand, test, and maintain. As it is used
  by `codewatch/cli.py` and `codewatch/review.py`, its complexity poses a
  significant risk to stability and increases future refactoring efforts.

MEDIUM · LONG_FUNCTION:codewatch/review.py:run_review
  The `run_review` function in `codewatch/review.py` exceeds the recommended
  length (metric=51), suggesting a potential for decreased clarity and increased
  bug surface. Its integration with `codewatch/cli.py` means modifications
  could be complex, raising maintenance costs and refactoring challenges.

MEDIUM · LONG_FUNCTION:codewatch/review.py:_build_user_prompt
  With a length of 125, `_build_user_prompt` in `codewatch/review.py` is
  significantly long, making it hard to read and maintain. This directly impacts
  `codewatch/cli.py`, increasing the blast radius for potential bugs and making
  any necessary refactoring a considerable undertaking.

MEDIUM · LONG_FUNCTION:codewatch/review.py:_render
  The `_render` function in `codewatch/review.py` is long (metric=80), which
  can lead to reduced code clarity and higher maintenance costs. Its dependency
  on `codewatch/cli.py` implies that changes or bug fixes require careful
  consideration due to the increased risk and refactoring effort.

MEDIUM · LONG_FUNCTION:codewatch/cli.py:_run
  The `_run` function in `codewatch/cli.py` has a length metric of 86,
  indicating it is overly long and likely difficult to maintain. This complexity
  directly impacts the core CLI operation, posing a significant refactoring
  challenge and increasing the blast radius for any introduced defects.

MEDIUM · DEEP_NESTING:codewatch/cli.py:_files_from_diff
  The deep nesting (metric=6) within `_files_from_diff` in `codewatch/cli.py`
  makes the code harder to debug and refactor. This increases the likelihood of
  errors and raises maintenance costs for the file collection logic.

MEDIUM · DEEP_NESTING:codewatch/cli.py:_parse_files
  Deeply nested logic (metric=4) in `_parse_files` within `codewatch/cli.py`
  can obscure control flow, making maintenance and refactoring more difficult
  and error-prone. This directly impacts the core file processing of the CLI.

MEDIUM · LONG_FUNCTION:codewatch/cli.py:_format_human
  The `_format_human` function in `codewatch/cli.py` is long (metric=65),
  which can make it harder to read and maintain. This increases the risk of
  bugs and complicates future modifications, directly affecting the CLI's
  output formatting.

MEDIUM · DEEP_NESTING:codewatch/parse.py:_resolve_imports
  The deep nesting (metric=6) in `_resolve_imports` within `codewatch/parse.py`
  complicates understanding and debugging. Its impact on `codewatch/cli.py`
  means that this complexity could lead to a wider blast radius for issues and
  increased refactoring effort.

MEDIUM · DEEP_NESTING:codewatch/parse.py:_resolve_dotted
  The nesting level of 4 in `_resolve_dotted` from `codewatch/parse.py` makes
  the code harder to follow, increasing maintenance costs. Since
  `codewatch/cli.py` relies on this parsing logic, refactoring efforts and the
  blast radius of bugs are heightened.

MEDIUM · DEEP_NESTING:codewatch/parse.py:_all_imported_names
  With a nesting depth of 5, `_all_imported_names` in `codewatch/parse.py` is
  complex and difficult to refactor. The dependency from `codewatch/cli.py`
  means that issues here could propagate, increasing the blast radius and
  maintenance overhead.

LOW · LONG_PARAM_LIST:codewatch/rules.py:run_rules
  The `run_rules` function in `codewatch/rules.py` has a long parameter list
  (metric=6). While a low-severity issue, it can still make the function harder
  to call correctly and understand, slightly increasing maintenance effort.

LOW · LONG_PARAM_LIST:codewatch/review.py:run_review
  The `run_review` function in `codewatch/review.py` having 6 parameters makes
  it less readable and potentially harder to use. This slightly increases the
  maintenance burden and the chance of incorrect usage.

LOW · LONG_PARAM_LIST:codewatch/review.py:_build_user_prompt
  A parameter list of 6 for `_build_user_prompt` in `codewatch/review.py` can
  make the function signature cumbersome and harder to manage. This minor
  increase in complexity can lead to slightly higher maintenance costs.

LOW · LONG_PARAM_LIST:codewatch/cli.py:review_file
  The `review_file` function in `codewatch/cli.py` has 5 parameters, which
  marginally impacts readability and maintainability. Overly long parameter
  lists can contribute to increased refactoring effort.

LOW · LONG_PARAM_LIST:codewatch/cli.py:review_pr
  With 5 parameters, the `review_pr` function in `codewatch/cli.py` exhibits a
  slightly long parameter list. This can make the function signature less
  intuitive, marginally increasing maintenance effort.

LOW · LONG_PARAM_LIST:codewatch/cli.py:review_repo
  The `review_repo` function in `codewatch/cli.py` has 5 parameters. Long
  parameter lists can slightly degrade code clarity and increase the potential
  for errors during modification, contributing to maintenance overhead.

LOW · LONG_PARAM_LIST:codewatch/cli.py:_run
  The `_run` function in `codewatch/cli.py` has 10 parameters. Such a long
  list can make the function harder to understand and use correctly, slightly
  increasing maintenance costs and refactoring difficulty.

(DUPLICATE_LOGIC detection skipped — install sentence-transformers or remove --skip-semantic)
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
