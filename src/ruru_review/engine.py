"""One review: read the diff, audit the tests, run fail first and the mutants, apply the gate, build the report."""

import hashlib
import os
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ruru_review import __version__, config, failfirst, gitio, mutants, pyast, runner, tamper

SCHEMA = "ruru.report/1"


@dataclass
class Options:
    """What the command line asked for."""

    repo: Path = field(default_factory=Path.cwd)
    base: str | None = None
    head: str | None = None
    mode: str = "auto"
    max_mutants: int | None = None
    time_budget: float | None = None
    test_command: str | None = None
    python: str | None = None
    refactor: bool = False
    workers: int | None = None
    run_tests: bool = True
    run_mutants: bool = True
    whole: bool = False
    labels: list[str] = field(default_factory=list)
    author: str | None = None
    branch: str | None = None
    progress: Any = None


@dataclass
class Context:
    """The resolved change."""

    root: Path
    cfg: config.Config
    base_ref: str
    merge_base: str
    head: str | None
    head_sha: str
    files: list[gitio.FileChange]


def resolve(opts: Options) -> Context:
    """
    Resolve the repository, settings, base and head.

    :param opts: Options
    :returns: The change
    """
    root = gitio.toplevel(opts.repo)
    cfg = config.load(root)
    head = gitio.resolve(root, opts.head) if opts.head else None
    head_sha = head or gitio.resolve(root, "HEAD")
    if opts.whole:
        return Context(
            root,
            cfg,
            "(whole repository)",
            gitio.EMPTY_TREE,
            head,
            head_sha,
            gitio.changed_files(root, gitio.EMPTY_TREE, head),
        )
    base_ref = opts.base or cfg.base or gitio.default_base(root)
    mb = gitio.merge_base(root, base_ref, head)
    return Context(root, cfg, base_ref, mb, head, head_sha, gitio.changed_files(root, mb, head))


def finding_id(*parts: object) -> str:
    """
    Make a stable id for a finding, used to deduplicate comments across reruns.

    :param parts: What identifies it
    :returns: 12 hex characters
    """
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12]


def detect_agent(ctx: Context, opts: Options) -> dict[str, Any]:
    """
    Decide whether an AI agent wrote the change, and say why.

    :param ctx: The change
    :param opts: Options (labels, author and branch from CI)
    :returns: ``{"agent": bool, "reasons": [...]}``
    """
    a = ctx.cfg.agents
    reasons = []
    # GitHub's pull request variables describe the checkout CI made, not any other repository ruru is pointed at
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    in_ci_checkout = bool(workspace) and os.path.realpath(workspace) == os.path.realpath(ctx.root)
    ci: Mapping[str, str] = os.environ if in_ci_checkout else {}
    author = opts.author or ci.get("RURU_PR_AUTHOR", "")
    if author and author in a.bots:
        reasons.append(f"author {author}")
    branch = (
        opts.branch
        or ci.get("GITHUB_HEAD_REF")
        or gitio.git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    )
    if any(branch.startswith(p) for p in a.branches):
        reasons.append(f"branch {branch}")
    labels = set(opts.labels) | {x for x in ci.get("RURU_PR_LABELS", "").split(",") if x}
    for label in sorted(labels & set(a.labels)):
        reasons.append(f"label {label}")
    span = ctx.head_sha if ctx.merge_base == gitio.EMPTY_TREE else f"{ctx.merge_base}..{ctx.head_sha}"
    log = gitio.git(ctx.root, "log", "--format=%B%x00", span, check=False).lower()
    for t in a.trailers:
        if "co-authored-by:" in log and any(
            t.lower() in line for line in log.splitlines() if line.startswith("co-authored-by:")
        ):
            reasons.append(f"co author trailer {t}")
            break
    return {"agent": bool(reasons), "reasons": reasons}


def _mutant_items(results: list[runner.Result], skipped: list[mutants.Mutant]) -> list[dict[str, Any]]:
    items = []
    for r in results:
        m = r.mutant
        items.append(
            {
                "id": m.id,
                "check": "mutant",
                "file": m.file,
                "line": m.line,
                "end_line": m.end_line,
                "rule": m.operator,
                "before": m.before,
                "after": m.after,
                "tests": r.tests,
                "outcome": r.outcome,
                "severity": "high" if r.outcome in (runner.SURVIVED, runner.NOT_COVERED) else "info",
                "side": "RIGHT",
                "hint": (
                    m.hint
                    if r.outcome == runner.SURVIVED
                    else ("no test executes this line: add one that does" if r.outcome == runner.NOT_COVERED else "")
                ),
            }
        )
    for m in skipped:
        d = m.describe()
        items.append(
            {
                "id": m.id,
                "check": "mutant",
                "file": m.file,
                "line": m.line,
                "end_line": m.end_line,
                "rule": m.operator,
                "before": d["before"],
                "after": d["after"],
                "tests": [],
                "outcome": runner.SKIPPED,
                "severity": "info",
                "side": "RIGHT",
                "hint": "",
            }
        )
    return items


def _gate(summary: dict[str, Any], cfg: config.Config, enforce: bool, refactor: bool) -> dict[str, Any]:
    g = cfg.gate
    reasons = []
    if g.tamper_high and summary["tamper"]["high"]:
        reasons.append(f"{summary['tamper']['high']} high tamper finding(s)")
    ff = summary["fail_first"]
    if g.fail_first and not refactor and ff["new_passes_without_change"]:
        reasons.append(f"{ff['new_passes_without_change']} new test(s) pass without the change")
    m = summary["mutants"]
    if m["score"] is not None and m["run"] >= g.min_mutants and m["score"] < g.min_score:
        reasons.append(f"diff mutation score {m['score']:.0%} is under {g.min_score:.0%}")
    if not reasons:
        result = "pass"
    else:
        result = "fail" if enforce else "advise"
    return {"result": result, "enforced": enforce, "reasons": reasons}


def review(opts: Options, ctx: Context | None = None) -> dict[str, Any]:
    """
    Run every check and build the report.

    :param opts: Options
    :param ctx: An already resolved change
    :returns: The report, schema ``ruru.report/1``
    :raises runner.EnvironmentProblem: When the import check or the baseline fails
    """
    ctx = ctx or resolve(opts)
    cfg = ctx.cfg
    say = opts.progress or (lambda _msg: None)
    tdirs = cfg.tests
    notes: list[str] = []
    items: list[dict[str, Any]] = []

    whole = ctx.merge_base == gitio.EMPTY_TREE
    findings = [] if whole else tamper.audit(ctx.root, ctx.merge_base, ctx.head, ctx.files, tdirs)
    if whole:
        notes.append("whole repository: every source line is in scope; the tamper audit and fail first need a base")
    for f in findings:
        items.append(
            {
                "id": finding_id("tamper", f.rule, f.file, f.line, f.message),
                "check": "tamper",
                "file": f.file,
                "line": f.line,
                "end_line": f.line,
                "rule": f.rule,
                "before": f.before,
                "after": f.after,
                "tests": [],
                "outcome": "finding",
                "severity": f.severity,
                "side": f.side,
                "hint": f.message,
            }
        )

    src_changes = [f for f in ctx.files if f.new_path and pyast.is_source_file(f.new_path, tdirs) and f.added]
    all_mutants: list[mutants.Mutant] = []
    for change in src_changes:
        text = gitio.read_at(ctx.root, ctx.head, change.new_path or "")
        if text is not None:
            all_mutants += mutants.generate(change.path, text, change.added, cfg.operators, cfg.arid)
    limit = opts.max_mutants if opts.max_mutants is not None else cfg.max_mutants
    chosen, over = mutants.select(all_mutants, limit, ctx.head_sha)

    tests = [] if whole else failfirst.changed_tests(ctx.root, ctx.merge_base, ctx.head, ctx.files, tdirs)
    test_files = sorted(
        {t[1] for t in tests} | {f.new_path for f in ctx.files if f.new_path and pyast.is_test_file(f.new_path, tdirs)}
    )
    verdicts: list[failfirst.TestVerdict] = []
    results: list[runner.Result] = []
    project = runner.make_project(ctx.root, opts.python or None, opts.test_command or cfg.test_command, cfg.src)
    budget = opts.time_budget if opts.time_budget is not None else cfg.time_budget

    if opts.run_tests and (chosen or tests):
        workers = max(1, opts.workers or cfg.workers)
        with ExitStack() as stack:
            trees = [
                stack.enter_context(gitio.Worktree(ctx.root, ctx.head_sha, ctx.head is None))
                for _ in range(workers if chosen and opts.run_mutants else 1)
            ]
            say(f"worktree ready at {ctx.head_sha[:10]}; checking imports")
            runner.import_check(project, trees[0], runner.module_names([f.path for f in src_changes], project.src_root))
            say("baseline test run")
            base = runner.baseline(project, trees[0], use_coverage=bool(chosen))
            if base.note:
                notes.append(base.note)
            deadline = time.monotonic() + budget
            if tests:
                say(f"fail first: {len(tests)} new or changed test(s)")
                packages = runner.module_names([f.path for f in src_changes], project.src_root)
                timeout = max(60.0, 5 * base.seconds)
                verdicts = failfirst.check(project, trees[0], ctx.merge_base, tests, test_files, timeout, packages)
            if chosen and opts.run_mutants:
                say(f"mutants: {len(chosen)} of {len(all_mutants)}")
                results = runner.run_mutants(
                    project,
                    trees,
                    chosen,
                    base,
                    deadline,
                    lambda r: say(
                        f"  {r.outcome:<11} {r.mutant.file}:{r.mutant.line} {r.mutant.operator} "
                        f"{r.mutant.before!r} -> {r.mutant.after!r}"
                    ),
                )
    else:
        results = [runner.Result(m, runner.SKIPPED) for m in chosen]
        if chosen or tests:
            notes.append("tests were not run")

    # a refactor keeps tests green on both sides, and a change with no source can only add tests for existing code
    ff_advice = opts.refactor or not src_changes
    for v in verdicts:
        # a new test that passes on the base proves nothing; an edited old one may be strengthening old behaviour
        if v.status != failfirst.PASSES_WITHOUT or ff_advice:
            sev = "info"
        else:
            sev = "high" if v.new else "medium"
        hint = {
            failfirst.PASSES_WITHOUT: (
                "this test passes on the base too, so it does not prove the change; assert what the change adds"
            ),
            failfirst.HEAD_FAILS: "this test does not pass at the head; fix it before ruru can score it",
            failfirst.PROVES_IMPORT: "fails on the base because it imports the new code",
            failfirst.PROVES: "fails on the base and passes at the head",
            failfirst.NOT_RUN: "the test did not run (skipped or not collected)",
        }[v.status]
        items.append(
            {
                "id": finding_id("fail_first", v.node_id),
                "check": "fail_first",
                "file": v.file,
                "line": v.line,
                "end_line": v.line,
                "rule": v.status,
                "before": v.base,
                "after": v.head,
                "tests": [v.node_id],
                "outcome": v.status,
                "severity": sev,
                "side": "RIGHT",
                "hint": hint,
            }
        )
    items += _mutant_items(results, over)

    count = {
        k: sum(1 for r in results if r.outcome == k)
        for k in (runner.KILLED, runner.SURVIVED, runner.TIMEOUT, runner.NOT_COVERED, runner.SKIPPED)
    }
    count[runner.SKIPPED] += len(over)
    ran = count[runner.KILLED] + count[runner.SURVIVED] + count[runner.TIMEOUT]
    score = round((count[runner.KILLED] + count[runner.TIMEOUT]) / ran, 4) if ran else None
    ff_count = {
        s: sum(1 for v in verdicts if v.status == s)
        for s in (
            failfirst.PROVES,
            failfirst.PROVES_IMPORT,
            failfirst.PASSES_WITHOUT,
            failfirst.HEAD_FAILS,
            failfirst.NOT_RUN,
        )
    }
    test_only = bool(ctx.files) and not src_changes
    summary: dict[str, Any] = {
        "mutants": {"total": len(all_mutants), "run": ran, "score": score, **count},
        "fail_first": {
            "tests": len(verdicts),
            **ff_count,
            "new_passes_without_change": sum(1 for v in verdicts if v.status == failfirst.PASSES_WITHOUT and v.new),
            "advice_only": ff_advice,
        },
        "tamper": {
            "high": sum(1 for f in findings if f.severity == tamper.HIGH),
            "medium": sum(1 for f in findings if f.severity == tamper.MEDIUM),
        },
        "tests_only_weakening": test_only and any(f.severity == tamper.HIGH for f in findings),
    }
    agent = detect_agent(ctx, opts)
    enforce = opts.mode == "gate" or (opts.mode == "auto" and agent["agent"])
    summary["gate"] = _gate(summary, cfg, enforce, ff_advice)
    rank = {"tamper": 0, "mutant": 1, "fail_first": 2}
    items.sort(key=lambda i: (i["severity"] != "high", rank[i["check"]], i["file"], i["line"], i["id"]))
    return {
        "schema": SCHEMA,
        "tool": {"name": "ruru", "version": __version__},
        "base": {"ref": ctx.base_ref, "merge_base": ctx.merge_base},
        "head": {"sha": ctx.head_sha, "working_tree": ctx.head is None},
        "mode": opts.mode,
        "agent": agent,
        "python": runner.python_version(project.python),
        "config": {"max_mutants": limit, "operators": cfg.operators or mutants.OPERATORS, "gate": asdict(cfg.gate)},
        "summary": summary,
        "items": items,
        "notes": notes,
    }


def exit_code(report: dict[str, Any]) -> int:
    """
    Give the process exit code for a report: 1 when the gate failed, else 0.

    :param report: A report
    :returns: Exit code
    """
    return 1 if report["summary"]["gate"]["result"] == "fail" else 0
