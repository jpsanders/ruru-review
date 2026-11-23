# ruru

A pull request reviewer for code written by AI agents. ruru asks three questions of a change and answers each with
evidence, not opinion:

1. **Tamper audit.** Did the change weaken or bypass the tests?
2. **Fail first.** Do the new and changed tests fail without the change?
3. **Mutants on the diff.** Would the tests catch a bug in the lines that changed?

Every check is deterministic. ruru calls no model, needs no API key, and your code never leaves the machine or the CI
runner. Your own coding agent reads what ruru finds (`--json`), writes the missing tests, and ruru verifies them.

## Synopsis

```text
ruru check   [--base REF | --all] [--head REF] [--mode advise|gate|auto] [--json PATH] [--markdown PATH]
             [--max-mutants N] [--time-budget S] [--workers N] [--test-command CMD] [--python PATH]
             [--refactor] [--label NAME] [--quiet] [--json-stdout] [--repo PATH]
ruru audit   [--base REF] [--head REF] [--mode MODE] [--json PATH] [--markdown PATH]
ruru mutants [--base REF | --all] [--head REF] [--json-stdout]
ruru verify  ID [--base REF] [--head REF] [--test-command CMD] [--python PATH]
ruru post    --report PATH [--pr N] [--comment-cap N] [--repository OWNER/NAME] [--event PATH]
ruru mcp
ruru guide
ruru man [--install] [--raw]
ruru --version
```

## The change

The base is the merge base of the target branch and the head. The target branch is `--base`, then `[tool.ruru] base`,
then origin's HEAD, `main` or `master`. The head is `--head` (in CI, the pull request's head commit), or the working
tree when `--head` is left out, uncommitted and untracked files included, so an agent can check its work before it
commits.

## Commands

### check

Runs all three checks and prints a score card, then the findings, most serious first. Progress lines go to stderr.

- `--json PATH` writes the report an agent works from (see Report).
- `--markdown PATH` writes the summary as Markdown.
- `--sarif PATH` writes SARIF 2.1.0 for GitHub code scanning.
- Inside GitHub Actions (`GITHUB_ACTIONS=true`) ruru also appends the summary to the job summary, writes the outputs
  `score`, `survived`, `tamper-high`, `gate` and `report`, and marks up to 10 findings on the diff with `::warning`
  annotations.
- `--mode` picks what a failed gate does: `gate` fails the run, `advise` never fails it, `auto` (the default) gates
  changes written by an agent and advises on the rest.
- `--all` reviews the whole repository instead of a change: every source line is in scope, and the mutant cap
  samples from all of them. The tamper audit and fail first need a base, so they are skipped. Use it to score a test
  suite, or on a repository's first commit.
- `--refactor` says the change is a refactor: tests should stay green on both sides, so fail first gives advice only.
- `--max-mutants N` (default 60) and `--time-budget S` (default 600) cap the mutant run. Over the cap, ruru keeps the
  most valuable operators first and breaks ties with a shuffle seeded by the head SHA, so reruns on the same commit
  choose the same mutants.
- `--workers N` (default 2) runs mutants in that many git worktrees at once.
- `--python PATH` is the project's interpreter. Without it ruru uses `.venv/bin/python`, `venv/bin/python`, the active
  virtualenv, then `python3`. The project can use any Python from 3.9 on; ruru itself needs 3.14.
- `--test-command CMD` replaces `PYTHON -m pytest`. It must accept pytest's arguments.
- `--label NAME` passes a pull request label for agent detection; repeat it for several.

### audit

Only the tamper audit. Static and fast: nothing is installed and no test runs.

### mutants

Lists the mutants of the change, with their ids, without running anything.

### verify ID

Reruns one mutant, by the id a report gave it, against the tests as they are now. Exit 0 when the tests kill it, 1
when it still survives, 2 when the id is not in the change (the line moved or changed; run `ruru mutants`).

### post

Posts a report to its pull request: one summary comment, updated in place on every run (it carries the hidden marker
`<!-- ruru -->`), and one review with at most `--comment-cap` (default 5) inline comments, most valuable first: high
tamper findings, then survivors in the most changed files, then fail first findings. A finding already commented on in
an earlier run is not posted again. Comments describe the gap and hint at a test; they never propose code.

`post` is built to run with a write token in a job that never checks out pull request code. It reads the token from
`GITHUB_TOKEN` and the trusted event from `GITHUB_EVENT_PATH`, and trusts nothing in the report: it checks the
schema, drops malformed items and paths that climb out of the repository, checks that the pull request's head is the
commit the event names and that the report was made for that commit, and escapes and truncates every string
(no HTML, no mentions, no table breaks). Under `workflow_run`, a pull request from a fork is not listed in the
event, so the number comes from the artifact through `--pr`, and is only accepted when that pull request's head is
the event's commit. Exit 2 on any mismatch.

### mcp

Serves five tools over stdio for coding agents (Model Context Protocol, JSON-RPC 2.0, one message per line):

| Tool | What it does |
|---|---|
| `ruru_check` | all three checks; returns the gate, the counts and a to do list |
| `ruru_survivors` | survived and not covered mutants from the last check |
| `ruru_explain` | one item with the source lines around it and the tests that ran |
| `ruru_verify` | rerun one mutant against the current tests |
| `ruru_audit` | the static tamper audit only |

Each takes `repo` (default: the server's working folder); the check tools also take `base`, `head`, `python` and
`test_command`.

### guide, man

`guide` prints this manual. `man` shows it as a man page; `man --install` puts it where `man ruru` finds it.

## The three checks

### Tamper audit

ruru compares the base and head versions of every changed test module, `conftest.py`, test and coverage
configuration, CI workflow and source file. Each finding is high severity unless it says otherwise. A test that
was renamed (most of its body kept, or a close name and half its body) is compared with its old self, so a rename is
not a removal. Coverage `omit` and `exclude` lines count only when they change coverage configuration that already
existed.

| Rule | What it means |
|---|---|
| `test-removed` | a test function that existed on the base is gone |
| `assertion-removed` | a test still exists but asserts less |
| `assertion-loosened` | exact assertions became weaker: `==` to `assert x`, `is not None` or `in`; |
| | `assertEqual` to `assertTrue`; `raises(KeyError)` to `raises(Exception)` |
| `assert-true` | a test now asserts `True` |
| `approx-widened` | a `pytest.approx` tolerance grew |
| `cases-removed` | a `parametrize` list lost cases |
| `skip-added` | a new `skip`, `xfail`, `unittest.skip` or `pytestmark`; medium when conditional: `skipif`, |
| | `importorskip`, or a `pytest.skip()` inside an `if` |
| `empty-test` | a new test asserts nothing and calls nothing |
| `assertion-in-try` | assertions were wrapped in `try`/`except` |
| `except-pass` | a test swallows an exception with `pass` |
| `harness-hook` | a `conftest.py` gained a hook that can change what runs, such as `pytest_collection_modifyitems` |
| `addopts-deselect` | test options gained `-k`, `--deselect`, `--ignore` or `-p no:` |
| `coverage-omit`, `coverage-lowered` | coverage leaves code out, or its floor went down |
| `startup-hook` | a new `sitecustomize.py` or `usercustomize.py` |
| `ci-ignores-failure` | a CI line gained `\|\| true` or `continue-on-error: true` |
| `no-cover-added` (medium) | source gained `# pragma: no cover` |

When a change touches only tests and weakens them, the summary says so (`tests_only_weakening`).

### Fail first

ruru finds the test functions the change adds or edits, comparing each function's syntax tree by its qualified name,
so a moved function is not a changed one. It runs them at the head, then in a worktree at the base with the head's test
files laid over it. Imports of names that do not exist on the base become placeholders that fail when used, so one new
name does not hide every other test in the file.

| Outcome | Meaning |
|---|---|
| `proves_change` | fails on the base, passes at the head |
| `needs_new_code` | fails on the base because it uses code the change adds |
| `passes_without_change` | passes on both sides: it does not prove the change. High for a new test, medium for |
| | an edited one (it may strengthen old behaviour); advice only when the change touches no source |
| `fails_on_head` | does not pass at the head; reported, not scored |
| `not_run` | skipped or not collected |

### Mutants on the diff

ruru parses each changed source file and mutates only syntax that touches an added or modified line. Each mutant is
one small bug:

| Code | Mutation |
|---|---|
| `ROR` | comparison: `<` and `<=`, `>` and `>=`, `==` and `!=`, `is` and `is not`, `in` and `not in` |
| `NEG` | condition: `if c` to `if not (c)`, `while c` likewise, an existing `not` removed |
| `RET` | `return x` to `return None` |
| `AOR` | arithmetic: `+` and `-`, `*` and `/`, `//` to `/`, `%` to `*`, `**` to `*` |
| `LCR` | boolean: `and` and `or` |
| `CRP` | constants: `n` to `n + 1`, `0` and `1` swapped, `True` and `False`, a non empty string to `""` |
| `AUG` | `+=` and `-=`, `*=` and `/=` |
| `BRK` | `break` and `continue` |
| `SDL` | a call statement replaced by `pass` |

**Never mutated** (arid code): logging and `print` calls, `warnings.warn`, docstrings, annotations and default
values, `__repr__` and `__str__`, `if __name__ == "__main__":` blocks, `assert` and imports in source,
`raise NotImplementedError`, the text of f strings, `__all__` and `__version__`. A line ending in `# ruru: ignore`
is skipped; `# ruru: ignore[ROR,CRP]` skips only those operators. `[tool.ruru] arid` adds call name patterns such as
`metrics.*`.

**How mutants run.** ruru makes one git worktree at the head per worker. Before any mutant it runs an **import check**:
the project's interpreter, with the worktree's `src` (or root) first on `PYTHONPATH`, must import each changed
package from the worktree. If an installed copy wins, every mutant would survive, so ruru stops with exit 2 and says
why. Then a **baseline** run of the whole suite must pass. With pytest-cov installed in the project, the baseline also
records which tests execute which lines, and each mutant runs only those tests; without it, each mutant runs the full
suite. Each mutant runs with `-x -q -p no:cacheprovider` and a timeout of 5 times the baseline (at least 10 seconds);
on timeout the whole process group is killed.

| Outcome | Meaning |
|---|---|
| `killed` | a test failed |
| `timeout` | the tests hung; counts as killed |
| `survived` | every selected test passed: a gap in the tests (a finding) |
| `not_covered` | no test executes the line (a finding, not scored) |
| `skipped` | over the mutant cap or the time budget |

The **score** is killed and timed out mutants over all that ran.

## Agents and the gate

A change counts as an agent's when its pull request author is a listed bot (`RURU_PR_AUTHOR`), when a commit carries
a listed co author trailer, when the branch starts with `claude/`, `copilot/`, `codex/`, `cursor/` or `devin/`
(`GITHUB_HEAD_REF` in Actions, else the current branch), or when it has an `agent` label (`--label`, or
`RURU_PR_LABELS`, comma separated). Every list lives in `[tool.ruru.agents]`.

The gate fails on any high tamper finding, on any new test that passes without the change (unless `--refactor`,
or the change touches no source), or on a mutation score under 70% with at least 5 mutants run. In `auto` mode it
is enforced for agent changes only; for people it gives the same findings as advice.

## Report

`--json` writes schema `ruru.report/1`. `summary` holds the counts, the score and the gate's result and reasons.
`items` holds one entry per finding or mutant, most serious first:

```json
{
  "id": "ROR-822e815b28",
  "check": "mutant",
  "file": "src/calc/__init__.py",
  "line": 15,
  "end_line": 15,
  "rule": "ROR",
  "before": "pct > 100",
  "after": "(pct >= 100)",
  "tests": ["tests/test_calc.py::test_discount_runs"],
  "outcome": "survived",
  "severity": "high",
  "side": "RIGHT",
  "hint": "add a test at the boundary of this comparison, where < and <= (or == and !=) give different answers"
}
```

`check` is `tamper`, `fail_first` or `mutant`. Two runs on the same commit write the same report.

## The agent loop

Before an agent says a change is done:

1. `ruru check --json ruru-report.json`
2. For each `survived` item, write a test that fails with the mutant and passes without it; for each
   `passes_without_change`, assert what the change adds; for each tamper finding, put the test back.
3. `ruru verify <id>` for each survivor until it is killed.
4. Never weaken, skip or delete a test to make ruru pass.

## GitHub Action

`action.yml` at the root of `jpsanders/ruru-review` is a composite action, "ruru PR reviewer". It installs ruru on
its own Python 3.14 without touching the project's `PATH`, then runs `ruru check` with the project's interpreter.

| Input | Default | Meaning |
|---|---|---|
| `run` | `check` | `check` runs the tests (read only); `post` comments from a report |
| `base` | `origin/<base branch>` | target branch |
| `mode` | `auto` | `advise`, `gate` or `auto` |
| `max-mutants`, `time-budget` | 60, 600 | the mutant budget |
| `test-command`, `python` | pytest, `python` on `PATH` | how to run the project's tests |
| `post` | `false` | also comment from the same job; same repository pull requests only |
| `report` | `ruru-report.json` | report path |
| `pr` | | `post` only: the number from the artifact |
| `comment-cap` | 5 | most inline comments |

Outputs: `score`, `survived`, `tamper-high`, `gate`, `report`. The job fails when the gate fails and on exit 2.

**Never run pull request code with a write token.** Two workflows, both in `examples/workflows/`:

- `ruru.yml` (Template A) runs on `pull_request` with `contents: read`: checkout with `fetch-depth: 0`, install the
  project and pytest, run the action, upload the report as an artifact. Safe for forks.
- `ruru-post.yml` (Template B) runs on `workflow_run` when A completes, with `pull-requests: write`. It downloads the
  artifact and runs the action with `run: post`. It never checks out the pull request.

A repository that only takes pull requests from its own branches can use Template A alone with `post: true` and
`pull-requests: write`.

## Claude Code plugin

The repository is a Claude Code plugin and marketplace. It adds the `ruru` skill, which tells the agent to run
`ruru_check` before it says a change is done, write a test for each survivor, verify it, and never weaken a test,
and it starts the MCP server through `uvx`. `docs/hooks/stop-hook.md` shows an opt in Stop hook that keeps the agent
working while the gate fails. Agents other than Claude Code get the same loop from `AGENTS.md` and
`ruru check --json`.

## Configuration

In the checked project's `pyproject.toml`:

```toml
[tool.ruru]
base = "main"
test-command = "python -m pytest -p no:randomly"
src = "src"
tests = ["tests"]
max-mutants = 60
time-budget = 600
workers = 2
comment-cap = 5
operators = ["ROR", "NEG", "RET", "AOR", "LCR", "CRP", "AUG", "BRK", "SDL"]
arid = ["metrics.*", "tracer.*"]

[tool.ruru.gate]
tamper-high = true
fail-first = true
min-score = 0.70
min-mutants = 5

[tool.ruru.agents]
bots = ["copilot-swe-agent[bot]", "claude[bot]", "devin-ai-integration[bot]", "cursor[bot]"]
branches = ["claude/", "copilot/", "codex/", "cursor/", "devin/"]
labels = ["agent"]
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed, or advice only |
| 1 | the gate failed |
| 2 | a usage or environment problem: not a repository, no base, the import check or the baseline failed |
