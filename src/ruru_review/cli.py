"""
The command line: ``ruru``.

``check`` runs the three checks on a change, ``audit`` only the static tamper audit, ``mutants`` lists the mutants
without running them, and ``verify`` reruns one mutant. ``guide`` prints the full manual and ``man`` shows, or
installs, the man page built from it.
"""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404: runs man with fixed arguments only
import sys
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruru_review import __version__

if TYPE_CHECKING:
    from ruru_review import engine, mutants

PROG = "ruru"


def _data(name: str) -> str:
    """
    Read a file shipped in the package's data folder.

    :param name: File name inside ``data``
    :returns: Its text
    """
    return (resources.files("ruru_review") / "data" / name).read_text(encoding="utf-8")


def _options(args: argparse.Namespace) -> engine.Options:
    """
    Build engine options from parsed arguments.

    :param args: Parsed arguments
    :returns: Options
    """
    from ruru_review import engine

    quiet = getattr(args, "quiet", False) or getattr(args, "json_stdout", False)
    return engine.Options(
        repo=Path(args.repo),
        base=args.base,
        head=args.head,
        mode=getattr(args, "mode", "auto"),
        max_mutants=getattr(args, "max_mutants", None),
        time_budget=getattr(args, "time_budget", None),
        test_command=getattr(args, "test_command", None),
        python=getattr(args, "python", None),
        refactor=getattr(args, "refactor", False),
        whole=getattr(args, "all", False),
        workers=getattr(args, "workers", None),
        labels=[x for x in (getattr(args, "label", None) or []) if x],
        progress=None if quiet else (lambda msg: print(f"ruru: {msg}", file=sys.stderr, flush=True)),
    )


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def _deliver(report: dict[str, Any], args: argparse.Namespace) -> None:
    from ruru_review import github, render

    _write(args.json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.markdown, render.markdown(report))
    _write(getattr(args, "sarif", None), json.dumps(github.sarif(report), indent=2) + "\n")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(render.markdown(report))
        outputs = os.environ.get("GITHUB_OUTPUT")
        if outputs:
            s = report["summary"]
            score = s["mutants"]["score"]
            with open(outputs, "a", encoding="utf-8") as fh:
                fh.write(f"score={'' if score is None else score}\nsurvived={s['mutants']['survived']}\n")
                fh.write(f"tamper-high={s['tamper']['high']}\ngate={s['gate']['result']}\nreport={args.json or ''}\n")
        for line in github.annotations(report):
            print(line, file=sys.stderr)
    if args.json_stdout:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render.terminal(report), end="")


def cmd_check(args: argparse.Namespace) -> int:
    """
    Run the tamper audit, fail first and the mutants on the change.

    :param args: Parsed arguments
    :returns: 0 pass or advice, 1 the gate failed, 2 an environment problem
    """
    from ruru_review import engine, gitio, runner

    try:
        report = engine.review(_options(args))
    except (runner.EnvironmentProblem, gitio.GitError, ValueError, OSError) as exc:
        print(f"ruru: {exc}", file=sys.stderr)
        return 2
    _deliver(report, args)
    return engine.exit_code(report)


def cmd_audit(args: argparse.Namespace) -> int:
    """
    Run only the static tamper audit; no tests run.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from ruru_review import engine, gitio

    opts = _options(args)
    opts.run_tests = False
    opts.max_mutants = 0
    try:
        report = engine.review(opts)
    except (gitio.GitError, ValueError) as exc:
        print(f"ruru: {exc}", file=sys.stderr)
        return 2
    _deliver(report, args)
    return engine.exit_code(report)


def _all_mutants(ctx: engine.Context) -> list[mutants.Mutant]:
    from ruru_review import gitio, mutants, pyast

    out: list[mutants.Mutant] = []
    for f in ctx.files:
        if f.new_path and f.added and pyast.is_source_file(f.new_path, ctx.cfg.tests):
            text = gitio.read_at(ctx.root, ctx.head, f.new_path)
            if text is not None:
                out += mutants.generate(f.new_path, text, f.added, ctx.cfg.operators, ctx.cfg.arid)
    return out


def cmd_mutants(args: argparse.Namespace) -> int:
    """
    List the mutants of the change without running them.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from ruru_review import engine, gitio

    try:
        ctx = engine.resolve(_options(args))
    except (gitio.GitError, ValueError) as exc:
        print(f"ruru: {exc}", file=sys.stderr)
        return 2
    found = _all_mutants(ctx)
    if args.json_stdout:
        print(json.dumps([m.describe() for m in found], indent=2))
    else:
        for m in found:
            print(f"{m.id}  {m.file}:{m.line}  {m.operator}  {m.before!r} -> {m.after!r}")
        print(f"{len(found)} mutant(s)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """
    Rerun one mutant against the current tests: did the new test kill it?

    :param args: Parsed arguments
    :returns: 0 when killed, 1 when it still survives, 2 on a problem
    """
    from ruru_review import engine, gitio, runner

    opts = _options(args)
    try:
        ctx = engine.resolve(opts)
        found = [m for m in _all_mutants(ctx) if m.id == args.id]
        if not found:
            print(
                f"ruru: no mutant {args.id} in this change (the line may have changed: run ruru mutants)",
                file=sys.stderr,
            )
            return 2
        m = found[0]
        project = runner.make_project(ctx.root, opts.python, opts.test_command or ctx.cfg.test_command, ctx.cfg.src)
        with gitio.Worktree(ctx.root, ctx.head_sha, ctx.head is None) as tree:
            runner.import_check(project, tree, runner.module_names([m.file], project.src_root))
            base = runner.baseline(project, tree, use_coverage=False)
            res = runner.run_mutants(project, [tree], [m], base, float("inf"))[0]
    except (runner.EnvironmentProblem, gitio.GitError, ValueError, OSError) as exc:
        print(f"ruru: {exc}", file=sys.stderr)
        return 2
    result = {"id": m.id, "file": m.file, "line": m.line, "operator": m.operator, "outcome": res.outcome}
    print(json.dumps(result) if args.json_stdout else f"{m.id} {m.file}:{m.line} {m.operator}: {res.outcome}")
    return 0 if res.outcome in (runner.KILLED, runner.TIMEOUT) else 1


def cmd_guide(args: argparse.Namespace) -> int:
    """
    Print the full manual as Markdown, written for people and agents alike.

    :param args: Parsed arguments
    :returns: Exit code
    """
    print(_data("MANUAL.md"), end="")
    return 0


def man_folder() -> Path:
    """
    Find the per user man folder that ``man`` searches without any setup.

    :returns: ``$XDG_DATA_HOME/man/man1``, or ``~/.local/share/man/man1``
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "man" / "man1"


def cmd_man(args: argparse.Namespace) -> int:
    """
    Show the man page, print it as roff, or install it so ``man ruru-review`` works.

    :param args: Parsed arguments
    :returns: Exit code
    """
    page = _data(f"{PROG}.1")
    if args.install:
        target = man_folder() / f"{PROG}.1"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        print(f"installed {target}; open it with: man {PROG}")
        return 0
    man = shutil.which("man")
    if man and sys.stdout.isatty() and not args.raw:
        with resources.as_file(resources.files("ruru_review") / "data" / f"{PROG}.1") as path:
            return subprocess.call([man, "-l", str(path)])  # nosec B603: man, fixed arguments
    print(page, end="")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    """
    Post a report to its pull request: the summary comment and one review. Never checks out pull request code.

    :param args: Parsed arguments
    :returns: 0 posted, 2 on any mismatch or refusal
    :raises PostError: Caught here, for a missing token or event
    """
    from ruru_review import github

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
    try:
        if not token:
            raise github.PostError("set GITHUB_TOKEN")
        if not event_path:
            raise github.PostError("no event: run in GitHub Actions or pass --event")
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        done = github.post(Path(args.report), github.GitHub(token, repo), event, args.pr, args.comment_cap)
    except (github.PostError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ruru: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(done))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """
    Serve the MCP tools over stdio.

    :param args: Parsed arguments
    :returns: Exit code
    """
    from ruru_review import mcp

    return mcp.serve()


def build_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser with every command.

    :returns: The parser
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A pull request reviewer for code written by AI agents: fail first, mutants, tamper audit.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(
        name: str, func: Callable[[argparse.Namespace], int], about: str, change: bool = True
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=about, description=about)
        if change:
            p.add_argument("--repo", default=".", help="the repository (default: the current folder)")
            p.add_argument("--base", help="target branch (default: [tool.ruru] base, origin's HEAD, main or master)")
            p.add_argument("--head", help="head commit (default: the working tree, uncommitted changes included)")
            p.add_argument("--json-stdout", action="store_true", help="print the JSON report instead of the score card")
        p.set_defaults(func=func)
        return p

    def outputs(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", metavar="PATH", help="write the JSON report (schema ruru.report/1)")
        p.add_argument("--markdown", metavar="PATH", help="write the Markdown summary")
        p.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 for code scanning")
        p.add_argument(
            "--mode",
            choices=["advise", "gate", "auto"],
            default="auto",
            help="gate fails the run, advise never does, auto gates agent changes only (default)",
        )
        p.add_argument("--label", action="append", help="a pull request label, for agent detection (repeatable)")
        p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")

    def testing(p: argparse.ArgumentParser) -> None:
        p.add_argument("--test-command", help="the test command (default: PYTHON -m pytest)")
        p.add_argument("--python", help="the project's interpreter (default: .venv, the active virtualenv, python3)")

    check = add("check", cmd_check, "run the tamper audit, fail first and the mutants on the change")
    outputs(check)
    testing(check)
    check.add_argument("--max-mutants", type=int, metavar="N", help="most mutants to run (default 60)")
    check.add_argument("--time-budget", type=float, metavar="S", help="seconds for mutants (default 600)")
    check.add_argument("--workers", type=int, metavar="N", help="parallel worktrees (default 2)")
    check.add_argument("--refactor", action="store_true", help="a refactor: fail first gives advice only")
    check.add_argument("--all", action="store_true", help="score the whole repository's tests, not a change")
    audit = add("audit", cmd_audit, "run only the static tamper audit")
    outputs(audit)
    mut = add("mutants", cmd_mutants, "list the mutants of the change without running them")
    mut.add_argument("--all", action="store_true", help="every mutant in the repository, not only the change")
    verify = add("verify", cmd_verify, "rerun one mutant against the current tests")
    verify.add_argument("id", help="the mutant id from a report")
    testing(verify)
    post = add("post", cmd_post, "post a report to its pull request (summary comment and one review)", change=False)
    post.add_argument("--report", required=True, help="the JSON report from the read only run")
    post.add_argument("--pr", type=int, help="pull request number from the artifact, for workflow_run (checked)")
    post.add_argument("--comment-cap", type=int, default=5, metavar="N", help="most inline comments (default 5)")
    post.add_argument("--repository", help="owner/name (default: GITHUB_REPOSITORY)")
    post.add_argument("--event", help="event payload file (default: GITHUB_EVENT_PATH)")
    add("mcp", cmd_mcp, "serve the MCP tools over stdio, for coding agents", change=False)
    add("guide", cmd_guide, "print the full manual, for people and agents", change=False)
    man = add("man", cmd_man, "show the man page", change=False)
    man.add_argument("--install", action="store_true", help=f"install it so `man {PROG}` works")
    man.add_argument("--raw", action="store_true", help="print the roff source")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the command line.

    :param argv: Arguments, without the program name; ``sys.argv`` when omitted
    :returns: Exit code
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
