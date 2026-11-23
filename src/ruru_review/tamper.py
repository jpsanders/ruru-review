"""
Tamper audit: did the change weaken or bypass the tests? Static and fast; nothing runs.

It compares the base and head versions of test files, ``conftest.py`` files, test and coverage configuration, CI
workflows and source pragmas.
"""

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ruru_review import gitio, pyast

HIGH, MEDIUM = "high", "medium"
STRONG_METHODS = {
    "assertEqual",
    "assertEquals",
    "assertListEqual",
    "assertDictEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertSequenceEqual",
    "assertMultiLineEqual",
    "assertAlmostEqual",
    "assertCountEqual",
    "assertRaises",
    "assertRaisesRegex",
    "assertIs",
    "assertIsNone",
    "assertGreater",
    "assertLess",
    "assertGreaterEqual",
    "assertLessEqual",
    "assertRegex",
    "assertNotEqual",
}
WEAK_METHODS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertIn", "assertIsInstance"}
SKIP_NAMES = {"skip", "skipif", "xfail", "importorskip", "skipIf", "skipUnless", "expectedFailure"}
HOOKS = {
    "pytest_collection_modifyitems",
    "pytest_ignore_collect",
    "pytest_collect_file",
    "pytest_collection",
    "pytest_deselected",
    "pytest_runtest_setup",
    "pytest_runtest_call",
    "pytest_runtest_makereport",
    "pytest_pycollect_makeitem",
    "pytest_configure",
    "pytest_sessionfinish",
    "pytest_report_teststatus",
}
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", ".coveragerc"}
_ADDOPTS = re.compile(r"(^|\s)(-k\b|--deselect\b|--ignore\b|--ignore-glob\b|-p\s*no:|--co\b|--lf\b)")
_FAIL_UNDER = re.compile(r"fail[_-]under\s*[=:]\s*([0-9.]+)")


@dataclass
class Finding:
    """One way the change weakened the tests."""

    rule: str
    severity: str
    file: str
    line: int
    side: str
    message: str
    before: str = ""
    after: str = ""


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _strength(node: ast.AST) -> tuple[int, int, list[str]]:
    """
    Count strong and weak assertions in a test.

    :param node: A test function
    :returns: (strong, weak, weak assertion texts)
    """
    strong = weak = 0
    weak_text: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            t = n.test
            if isinstance(t, ast.Constant):
                weak += 1
                weak_text.append(ast.unparse(n))
            elif (
                isinstance(t, ast.Compare)
                and any(isinstance(o, (ast.Eq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.NotEq, ast.Is)) for o in t.ops)
                and not (
                    isinstance(t.ops[0], ast.IsNot)
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value is None
                )
            ):
                strong += 1
            else:
                weak += 1
                weak_text.append(ast.unparse(n))
        elif isinstance(n, ast.Call):
            name = pyast.dotted(n.func).rsplit(".", 1)[-1]
            if name in STRONG_METHODS:
                strong += 1
            elif name in WEAK_METHODS:
                weak += 1
                weak_text.append(ast.unparse(n))
            elif name == "raises" and n.args:
                if pyast.dotted(n.args[0]) in {"Exception", "BaseException"}:
                    weak += 1
                    weak_text.append(ast.unparse(n))
                else:
                    strong += 1
    return strong, weak, weak_text


def _assert_count(node: ast.AST) -> int:
    s, w, _ = _strength(node)
    return s + w


CONDITIONAL_SKIPS = {"skipif", "skipIf", "skipUnless", "importorskip"}


def _skip_counts(node: ast.AST, in_if: bool = False) -> tuple[int, int]:
    """
    Count skip and xfail markers or calls, split into unconditional and conditional ones.

    ``skipif``, ``importorskip`` and a ``pytest.skip()`` inside an ``if`` are conditional: they usually guard a missing
    tool or platform. A bare ``skip`` or ``xfail`` turns a test off everywhere.

    :param node: Any node
    :param in_if: Whether ``node`` sits inside an ``if``
    :returns: (unconditional, conditional)
    """
    hard = soft = 0
    if isinstance(node, ast.Attribute) or isinstance(node, ast.Name):
        name = pyast.dotted(node)
        last = name.rsplit(".", 1)[-1]
        if last in SKIP_NAMES and (
            isinstance(node, ast.Name) or name.split(".", 1)[0] in {"pytest", "unittest", "mark"}
        ):
            if last in CONDITIONAL_SKIPS or in_if:
                soft += 1
            else:
                hard += 1
        if isinstance(node, ast.Attribute):
            return hard, soft  # the chain below names the same marker
    for child in ast.iter_child_nodes(node):
        h, c = _skip_counts(child, in_if or isinstance(node, ast.If))
        hard, soft = hard + h, soft + c
    return hard, soft


def _skips(node: ast.AST) -> list[str]:
    """
    Name every skip and xfail in a node, conditional or not.

    :param node: Any node
    :returns: One entry per marker or call
    """
    hard, soft = _skip_counts(node)
    return ["skip"] * hard + ["skipif"] * soft


def _similar(a: ast.AST, b: ast.AST) -> float:
    """
    Compare two test functions' bodies, ignoring their names.

    :param a: A function
    :param b: Another
    :returns: A ratio from 0 to 1
    """
    body_a = "\n".join(ast.unparse(x) for x in getattr(a, "body", []))
    body_b = "\n".join(ast.unparse(x) for x in getattr(b, "body", []))
    return difflib.SequenceMatcher(None, body_a, body_b, autojunk=False).ratio()


def _approx(node: ast.AST) -> list[float]:
    vals = []
    for c in _calls(node):
        if pyast.dotted(c.func).endswith("approx"):
            for kw in c.keywords:
                if (
                    kw.arg in {"rel", "abs"}
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                ):
                    vals.append(float(kw.value.value))
    return vals


def _swallowed(node: ast.AST) -> tuple[int, int]:
    """
    Count ``try`` blocks that hold assertions, and handlers whose body is only ``pass``.

    :param node: A test function
    :returns: (tries around assertions, bare pass handlers)
    """
    wrapped = passes = 0
    for n in ast.walk(node):
        if isinstance(n, ast.Try):
            if any(isinstance(x, ast.Assert) for s in n.body for x in ast.walk(s)) and n.handlers:
                wrapped += 1
            passes += sum(1 for h in n.handlers if all(isinstance(s, ast.Pass) for s in h.body))
    return wrapped, passes


def _parametrize_cases(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    total = 0
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and pyast.dotted(d.func).endswith("parametrize") and len(d.args) >= 2:
            if isinstance(d.args[1], (ast.List, ast.Tuple)):
                total += len(d.args[1].elts)
    return total


def _module_skips(tree: ast.Module | None) -> tuple[int, int]:
    """
    Count module level skips (``pytestmark``, ``importorskip``), unconditional and conditional.

    :param tree: A module
    :returns: (unconditional, conditional)
    """
    hard = soft = 0
    for stmt in tree.body if tree is not None else []:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            h, c = _skip_counts(stmt)
            hard, soft = hard + h, soft + c
    return hard, soft


def _module_skip_findings(path: str, b_tree: ast.Module | None, a_tree: ast.Module | None, what: str) -> list[Finding]:
    (bh, bc), (ah, ac) = _module_skips(b_tree), _module_skips(a_tree)
    if ah > bh:
        return [Finding("skip-added", HIGH, path, 1, "RIGHT", f"{what} gained a skip that turns tests off")]
    if ac > bc:
        return [Finding("skip-added", MEDIUM, path, 1, "RIGHT", f"{what} gained a conditional skip")]
    return []


def audit_test_file(path: str, old_path: str | None, before: str | None, after: str | None) -> list[Finding]:
    """
    Compare one test module's base and head versions.

    :param path: Head path, or base path when deleted
    :param old_path: Base path
    :param before: Base text
    :param after: Head text
    :returns: Findings
    """
    out: list[Finding] = []
    b_tree, a_tree = pyast.parse(before), pyast.parse(after)
    b_fns, a_fns = pyast.test_functions(b_tree), pyast.test_functions(a_tree)
    renamed: dict[str, str] = {}  # new name to old name
    gone = [n for n in b_fns if n not in a_fns]
    fresh = [n for n in a_fns if n not in b_fns]
    for old_name in gone:
        # a rename keeps most of the body, or a close name and half the body
        scored = []
        for n in fresh:
            if n in renamed:
                continue
            body = _similar(b_fns[old_name], a_fns[n])
            name_ratio = difflib.SequenceMatcher(None, old_name, n).ratio()
            if body >= 0.75 or (name_ratio >= 0.6 and body >= 0.5):
                scored.append((body + name_ratio, n))
        if scored:
            renamed[max(scored)[1]] = old_name
    for name, fn in b_fns.items():
        if name not in a_fns and name not in renamed.values():
            out.append(Finding("test-removed", HIGH, old_path or path, fn.lineno, "LEFT", f"test {name} was removed"))
    for name, fn in a_fns.items():
        old = b_fns.get(name) or (b_fns[renamed[name]] if name in renamed else None)
        if name in renamed:
            name = f"{name} (renamed from {renamed[name]})"
        if old is None:
            hard, soft = _skip_counts(fn)
            if hard or soft:
                why = "is skipped or xfailed" if hard else "is skipped under a condition"
                out.append(
                    Finding("skip-added", HIGH if hard else MEDIUM, path, fn.lineno, "RIGHT", f"new test {name} {why}")
                )
            if fn.body and not _assert_count(fn) and not _calls(fn):
                out.append(Finding("empty-test", HIGH, path, fn.lineno, "RIGHT", f"new test {name} asserts nothing"))
            continue
        if [pyast.dump(x) for x in old.body + old.decorator_list] == [
            pyast.dump(x) for x in fn.body + fn.decorator_list
        ]:
            continue
        bs, bw, _ = _strength(old)
        as_, aw, aw_text = _strength(fn)
        if as_ + aw < bs + bw:
            out.append(
                Finding(
                    "assertion-removed",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} has {bs + bw - as_ - aw} fewer assertion(s) than before",
                )
            )
        elif as_ < bs:
            out.append(
                Finding(
                    "assertion-loosened",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} traded exact assertions for weaker ones",
                    "",
                    "; ".join(aw_text[-3:]),
                )
            )
        if any("assert True" == t for t in aw_text) and not any("assert True" == t for t in _strength(old)[2]):
            out.append(Finding("assert-true", HIGH, path, fn.lineno, "RIGHT", f"{name} now asserts True"))
        (bh, bc), (ah, ac) = _skip_counts(old), _skip_counts(fn)
        if ah > bh:
            out.append(Finding("skip-added", HIGH, path, fn.lineno, "RIGHT", f"{name} gained a skip or xfail"))
        elif ac > bc:
            out.append(Finding("skip-added", MEDIUM, path, fn.lineno, "RIGHT", f"{name} gained a conditional skip"))
        bw_try, bw_pass = _swallowed(old)
        aw_try, aw_pass = _swallowed(fn)
        if aw_try > bw_try:
            out.append(
                Finding("assertion-in-try", HIGH, path, fn.lineno, "RIGHT", f"{name} wraps assertions in try/except")
            )
        if aw_pass > bw_pass:
            out.append(
                Finding("except-pass", HIGH, path, fn.lineno, "RIGHT", f"{name} swallows an exception with pass")
            )
        b_cases, a_cases = _parametrize_cases(old), _parametrize_cases(fn)
        if a_cases < b_cases:
            out.append(
                Finding(
                    "cases-removed",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} lost {b_cases - a_cases} parametrize case(s)",
                )
            )
        b_ap, a_ap = _approx(old), _approx(fn)
        if b_ap and a_ap and max(a_ap) > max(b_ap):
            out.append(
                Finding(
                    "approx-widened",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} widened a pytest.approx tolerance",
                    str(max(b_ap)),
                    str(max(a_ap)),
                )
            )
    out += _module_skip_findings(path, b_tree, a_tree, "the module")
    return out


def audit_conftest(path: str, before: str | None, after: str | None) -> list[Finding]:
    """
    Flag new collection or reporting hooks in a ``conftest.py``.

    :param path: Path
    :param before: Base text
    :param after: Head text
    :returns: Findings
    """

    def hooks(text: str | None) -> dict[str, int]:
        tree = pyast.parse(text)
        if tree is None:
            return {}
        return {n.name: n.lineno for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in HOOKS}

    b, a = hooks(before), hooks(after)
    out = [
        Finding(
            "harness-hook", HIGH, path, line, "RIGHT", f"conftest gained the hook {name}, which can change what runs"
        )
        for name, line in a.items()
        if name not in b
    ]
    b_tree, a_tree = pyast.parse(before), pyast.parse(after)
    out += _module_skip_findings(path, b_tree, a_tree, "conftest")
    return out


def _added_lines(text: str | None, added: set[int]) -> list[tuple[int, str]]:
    if text is None:
        return []
    lines = text.splitlines()
    return [(n, lines[n - 1]) for n in sorted(added) if 0 < n <= len(lines)]


def audit_config(path: str, before: str | None, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag test and coverage configuration that lets less run or count.

    :param path: Path
    :param before: Base text
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    out = []
    for n, line in _added_lines(after, added):
        if "addopts" in line or line.strip().startswith(("-", '"-', "'-")):
            if _ADDOPTS.search(line):
                out.append(
                    Finding(
                        "addopts-deselect",
                        HIGH,
                        path,
                        n,
                        "RIGHT",
                        "test options now deselect or skip tests",
                        "",
                        line.strip(),
                    )
                )
        had_coverage = before is not None and "coverage" in before
        omits = re.search(r"^\s*(omit|exclude_lines|exclude_also)\b", line) or (
            line.strip().startswith(('"', "'")) and "cover" in path
        )
        if had_coverage and omits:
            out.append(
                Finding("coverage-omit", HIGH, path, n, "RIGHT", "coverage now leaves code out", "", line.strip())
            )
    b = [float(x) for x in _FAIL_UNDER.findall(before or "")]
    a = [float(x) for x in _FAIL_UNDER.findall(after or "")]
    if b and (not a or min(a) < min(b)):
        out.append(
            Finding(
                "coverage-lowered",
                HIGH,
                path,
                1,
                "RIGHT",
                "the coverage floor was lowered or removed",
                str(min(b)),
                str(min(a)) if a else "removed",
            )
        )
    return out


def audit_workflow(path: str, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag CI steps that stop failing.

    :param path: Path
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    out = []
    for n, line in _added_lines(after, added):
        if re.search(r"\|\|\s*true\b|continue-on-error:\s*true", line):
            out.append(
                Finding(
                    "ci-ignores-failure", HIGH, path, n, "RIGHT", "a CI step no longer fails the job", "", line.strip()
                )
            )
    return out


def audit_source_pragmas(path: str, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag new ``# pragma: no cover`` in source.

    :param path: Path
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    return [
        Finding("no-cover-added", MEDIUM, path, n, "RIGHT", "source gained # pragma: no cover", "", line.strip())
        for n, line in _added_lines(after, added)
        if re.search(r"#\s*pragma:\s*no\s*cover", line)
    ]


def audit(
    repo: Path, base: str, head: str | None, files: list[gitio.FileChange], test_dirs: list[str]
) -> list[Finding]:
    """
    Run every tamper rule over a change.

    :param repo: Repository root
    :param base: Merge base
    :param head: Head commit or ``None``
    :param files: Changed files
    :param test_dirs: Test folder names
    :returns: Findings, high severity first
    """
    out: list[Finding] = []
    for f in files:
        before = gitio.read_at(repo, base, f.old_path) if f.old_path else None
        after = gitio.read_at(repo, head, f.new_path) if f.new_path else None
        name = PurePosixPath(f.path).name
        if pyast.is_test_file(f.path, test_dirs) or (f.old_path and pyast.is_test_file(f.old_path, test_dirs)):
            out += audit_test_file(f.path, f.old_path, before, after)
        elif name == "conftest.py":
            out += audit_conftest(f.path, before, after)
        elif name in CONFIG_FILES:
            out += audit_config(f.path, before, after, f.added)
        elif f.path.startswith(".github/workflows/") or name in {".gitlab-ci.yml", "noxfile.py", "Makefile"}:
            out += audit_workflow(f.path, after, f.added)
        elif name in {"sitecustomize.py", "usercustomize.py"} and before is None and after is not None:
            out.append(
                Finding("startup-hook", HIGH, f.path, 1, "RIGHT", f"new {name} runs code at every interpreter start")
            )
        elif f.path.endswith(".py") and after is not None:
            out += audit_source_pragmas(f.path, after, f.added)
    out.sort(key=lambda x: (x.severity != HIGH, x.file, x.line))
    return out
