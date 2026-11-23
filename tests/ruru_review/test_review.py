import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ruru_review import __version__
from ruru_review.cli import PROG, main
from ruru_review.failfirst import lenient_imports
from tests.ruru_review.conftest import git

Repo = Callable[..., Path]


def check(repo: Path, py: str, tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    out = tmp_path / "report.json"
    code = main(["check", "--repo", str(repo), "--python", py, "--json", str(out), "--quiet", *extra])
    return code, json.loads(out.read_text()) if out.exists() else {}


def test_weak_agent_pr(make_repo: Repo, py: str, tmp_path: Path) -> None:
    repo = make_repo()
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    assert code == 1
    s = r["summary"]
    assert s["gate"]["result"] == "fail" and r["agent"] == {"agent": True, "reasons": ["branch claude/discount"]}
    tamper = {(i["rule"], i["line"]) for i in r["items"] if i["check"] == "tamper"}
    assert tamper == {("assertion-removed", 6), ("assertion-loosened", 10), ("skip-added", 20)}
    ff = {i["tests"][0].split("::")[1]: i["outcome"] for i in r["items"] if i["check"] == "fail_first"}
    assert ff == {
        "test_add": "passes_without_change",
        "test_add_identity": "passes_without_change",
        "test_negative": "not_run",
        "test_discount_runs": "needs_new_code",
        "test_add_commutes": "passes_without_change",
    }
    severity = {i["tests"][0].split("::")[1]: i["severity"] for i in r["items"] if i["check"] == "fail_first"}
    assert severity == {
        "test_add": "medium",
        "test_add_identity": "medium",
        "test_negative": "info",
        "test_discount_runs": "info",
        "test_add_commutes": "high",
    }
    assert "1 new test(s) pass without the change" in s["gate"]["reasons"]
    survivors = {(i["file"], i["line"], i["rule"]) for i in r["items"] if i["outcome"] == "survived"}
    assert ("src/calc/__init__.py", 15, "ROR") in survivors
    assert ("src/calc/__init__.py", 17, "AOR") in survivors
    assert {(i["line"], i["rule"]) for i in r["items"] if i["outcome"] == "not_covered"} == {(16, "CRP")}
    assert s["mutants"]["score"] < 0.7 and s["mutants"]["run"] >= 5
    assert s["tests_only_weakening"] is False


def test_same_pr_from_a_person_only_advises(make_repo: Repo, py: str, tmp_path: Path) -> None:
    repo = make_repo(branch="feature/discount")
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    assert code == 0
    assert r["summary"]["gate"]["result"] == "advise" and r["agent"]["agent"] is False


def test_trailer_and_label_detect_agents(make_repo: Repo, py: str, tmp_path: Path) -> None:
    repo = make_repo(branch="feature/x", trailer="Co-Authored-By: Claude <noreply@anthropic.com>")
    code, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0", "--label", "agent")
    assert r["agent"]["reasons"] == ["label agent", "co author trailer noreply@anthropic.com"]


def test_reports_are_identical_across_runs(make_repo: Repo, py: str, tmp_path: Path) -> None:
    repo = make_repo()
    _, first = check(repo, py, tmp_path, "--head", "HEAD")
    _, second = check(repo, py, tmp_path, "--head", "HEAD")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_good_pr_passes(make_repo: Repo, py: str, tmp_path: Path) -> None:
    from tests.ruru_review.conftest import BASE_TESTS

    good_tests = BASE_TESTS.replace("from calc import add, parity", "from calc import add, double, parity") + (
        "\n\ndef test_double():\n    assert double(3) == 6\n    assert double(0) == 0\n"
    )
    src = "def double(n):\n    return n * 2\n"
    repo = make_repo(head={"src/calc/__init__.py": src_base() + "\n\n" + src, "tests/test_calc.py": good_tests})
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    assert code == 0, r["summary"]
    assert r["summary"]["gate"]["result"] == "pass"
    assert r["summary"]["mutants"]["score"] == 1.0


def src_base() -> str:
    from tests.ruru_review.conftest import BASE_SRC

    return BASE_SRC


def test_working_tree_mode(make_repo: Repo, py: str, tmp_path: Path) -> None:
    repo = make_repo(commit=False)
    code, r = check(repo, py, tmp_path, "--max-mutants", "3")
    assert r["head"]["working_tree"] is True
    assert r["summary"]["mutants"]["total"] == 9 and r["summary"]["mutants"]["skipped"] == 6


def test_import_check_stops_cleanly(
    make_repo: Repo, py: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.ruru_review.conftest import BASE_SRC, BASE_TESTS

    base = {
        "src/calc/__init__.py": BASE_SRC,
        "tests/test_calc.py": BASE_TESTS,
        "pyproject.toml": '[tool.ruru]\nsrc = "lib"\n',
    }
    repo = make_repo(base=base)
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    assert code == 2 and r == {}
    assert "import check" in capsys.readouterr().err


def test_failing_baseline_is_exit_2(
    make_repo: Repo, py: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.ruru_review.conftest import HEAD_SRC

    repo = make_repo(head={"src/calc/__init__.py": HEAD_SRC.replace("return a + b", "return a + b + 1")})
    code, _ = check(repo, py, tmp_path, "--head", "HEAD")
    assert code == 2
    assert "baseline" in capsys.readouterr().err


def test_audit_mutants_and_verify(make_repo: Repo, py: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo()
    assert main(["audit", "--repo", str(repo), "--head", "HEAD", "--mode", "gate", "--quiet"]) == 1
    assert "3 high" in capsys.readouterr().out
    assert main(["mutants", "--repo", str(repo), "--head", "HEAD", "--json-stdout"]) == 0
    found = json.loads(capsys.readouterr().out)
    ror = next(m for m in found if m["operator"] == "ROR")
    # the agent adds the missing boundary test in the working tree, then verifies
    tests = repo / "tests" / "test_calc.py"
    tests.write_text(tests.read_text() + "\n\ndef test_discount_full():\n    assert discount(50, 100) == 0\n")
    assert main(["verify", "--repo", str(repo), ror["id"], "--python", py]) == 0
    assert "killed" in capsys.readouterr().out
    assert main(["verify", "--repo", str(repo), "ROR-nothere", "--python", py]) == 2


def test_tests_only_weakening(make_repo: Repo, py: str, tmp_path: Path) -> None:
    from tests.ruru_review.conftest import BASE_TESTS

    repo = make_repo(head={"tests/test_calc.py": BASE_TESTS.replace("    assert add(-1, 1) == 0\n", "")})
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    assert r["summary"]["tests_only_weakening"] is True and code == 1


def test_lenient_imports_keeps_working_names() -> None:
    text = '"""doc"""\nfrom __future__ import annotations\nfrom os.path import join, nothere as nh\n'
    ns: dict[str, object] = {}
    exec(compile(lenient_imports(text), "t", "exec"), ns)
    assert callable(ns["join"])
    with pytest.raises(ImportError, match="ruru-missing-on-base: os.path.nothere"):
        ns["nh"]()  # type: ignore[operator]


def test_cli_basics(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"{PROG} {__version__}"
    assert main([]) == 2
    capsys.readouterr()
    assert main(["guide"]) == 0
    assert capsys.readouterr().out.startswith("# ruru")
    assert main(["man", "--raw"]) == 0
    assert ".TH" in capsys.readouterr().out
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert main(["man", "--install"]) == 0
    assert (tmp_path / "man" / "man1" / "ruru.1").exists()


def test_not_a_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--repo", str(tmp_path), "--quiet"]) == 2
    assert main(["mutants", "--repo", str(tmp_path)]) == 2


_ = (sys, git)


def test_github_actions_outputs(
    make_repo: Repo, py: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo()
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = main(
        [
            "audit",
            "--repo",
            str(repo),
            "--head",
            "HEAD",
            "--json",
            str(tmp_path / "r.json"),
            "--sarif",
            str(tmp_path / "r.sarif"),
            "--quiet",
        ]
    )
    assert code == 1
    assert "tamper-high=3" in out.read_text() and "gate=fail" in out.read_text()
    assert summary.read_text().startswith("<!-- ruru -->")
    assert json.loads((tmp_path / "r.sarif").read_text())["version"] == "2.1.0"
    assert "::warning file=tests/test_calc.py" in capsys.readouterr().err


def test_selection_keeps_every_test_of_a_line(make_repo: Repo, py: str, tmp_path: Path) -> None:
    # a weak test runs the line first, a strong one second; selection must keep both (sysmon kept only the first)
    base = {"src/calc/__init__.py": "def f(n):\n    return n\n", "tests/test_calc.py": "def test_zero():\n    pass\n"}
    head = {
        "src/calc/__init__.py": "def f(n):\n    return n * 2\n",
        "tests/test_calc.py": (
            "from calc import f\n\n\ndef test_a_weak():\n    assert f(3) is not None\n\n\n"
            "def test_b_strong():\n    assert f(3) == 6\n"
        ),
    }
    repo = make_repo(base=base, head=head)
    code, r = check(repo, py, tmp_path, "--head", "HEAD")
    aor = next(i for i in r["items"] if i["rule"] == "AOR")
    assert aor["outcome"] == "killed"
    assert aor["tests"] == ["tests/test_calc.py::test_a_weak", "tests/test_calc.py::test_b_strong"]


def test_whole_repository_mode(make_repo: Repo, py: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo()
    code, r = check(repo, py, tmp_path, "--all", "--head", "HEAD", "--mode", "advise")
    assert code == 0 and r["base"]["ref"] == "(whole repository)"
    m = r["summary"]["mutants"]
    assert m["total"] > 9 and m["run"] > 0
    assert {i["check"] for i in r["items"]} == {"mutant"} and r["summary"]["tamper"]["high"] == 0
    assert main(["mutants", "--repo", str(repo), "--all"]) == 0
    assert capsys.readouterr().out.strip().endswith(f"{m['total']} mutant(s)")


def test_new_package_cannot_borrow_an_installed_copy(make_repo: Repo, py: str, tmp_path: Path) -> None:
    from tests.ruru_review.conftest import BASE_TESTS, HEAD_SRC

    # the base has no calc package at all; the head adds it with a test
    repo = make_repo(
        base={"README.md": "x\n"}, head={"src/calc/__init__.py": HEAD_SRC, "tests/test_calc.py": BASE_TESTS}
    )
    code, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0")
    outcomes = {i["outcome"] for i in r["items"] if i["check"] == "fail_first"}
    assert outcomes == {"needs_new_code"}


def test_ci_variables_apply_only_to_the_ci_checkout(
    make_repo: Repo, py: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(branch="feature/x")
    monkeypatch.setenv("GITHUB_HEAD_REF", "claude/from-ci")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "elsewhere"))
    _, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0")
    assert r["agent"]["agent"] is False
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    _, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0")
    assert r["agent"]["reasons"] == ["branch claude/from-ci"]
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/x")
    monkeypatch.setenv("RURU_PR_AUTHOR", "copilot-swe-agent[bot]")
    monkeypatch.setenv("RURU_PR_LABELS", "bug,agent")
    _, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0")
    assert r["agent"]["reasons"] == ["author copilot-swe-agent[bot]", "label agent"]
    _, r = check(repo, py, tmp_path, "--head", "HEAD", "--max-mutants", "0", "--label", "x")
    assert "label agent" in r["agent"]["reasons"]


def test_tests_for_existing_code_are_not_gated(make_repo: Repo, py: str, tmp_path: Path) -> None:
    from tests.ruru_review.conftest import BASE_TESTS

    repo = make_repo(
        head={"tests/test_calc.py": BASE_TESTS + "\n\ndef test_parity_zero():\n    assert parity(0) == 'even'\n"}
    )
    code, r = check(repo, py, tmp_path, "--head", "HEAD", "--mode", "gate")
    assert code == 0 and r["summary"]["fail_first"]["advice_only"] is True
    assert r["summary"]["fail_first"]["passes_without_change"] == 1
    assert {i["severity"] for i in r["items"] if i["check"] == "fail_first"} == {"info"}
