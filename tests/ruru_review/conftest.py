"""A small git repository with a deliberately weak agent pull request, built fresh in a temp folder."""

import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

BASE_SRC = '''\
"""A tiny calculator."""


def add(a, b):
    return a + b


def parity(n):
    if n % 2 == 0:
        return "even"
    return "odd"
'''

BASE_TESTS = """\
import pytest

from calc import add, parity


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_add_identity():
    assert add(1, 1) == 2


def test_parity():
    assert parity(4) == "even"
    assert parity(3) == "odd"


def test_negative():
    assert add(-2, -2) == -4
"""

HEAD_SRC = BASE_SRC + """

def discount(price, pct):
    if pct > 100:
        raise ValueError("pct over 100")
    return price - price * pct / 100
"""

HEAD_TESTS = """\
import pytest

from calc import add, discount, parity


def test_add():
    assert add(2, 3) == 5


def test_add_identity():
    assert add(1, 1)


def test_parity():
    assert parity(4) == "even"
    assert parity(3) == "odd"


@pytest.mark.skip(reason="flaky")
def test_negative():
    assert add(-2, -2) == -4


def test_discount_runs():
    assert discount(100, 10) is not None


def test_add_commutes():
    assert add(2, 5) == add(5, 2)
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """
    Build a repo: a base commit on main, then the head files on a branch, committed unless ``commit`` is false.

    :param tmp_path: Pytest's temp folder
    :returns: A builder
    """

    def build(
        head: dict[str, str] | None = None,
        branch: str = "claude/discount",
        commit: bool = True,
        base: dict[str, str] | None = None,
        trailer: str = "",
    ) -> Path:
        repo = tmp_path / "proj"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        write(
            repo,
            base
            or {
                "src/calc/__init__.py": BASE_SRC,
                "tests/test_calc.py": BASE_TESTS,
                "pyproject.toml": "[tool.ruru]\nworkers = 2\n",
            },
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", branch)
        write(repo, head if head is not None else {"src/calc/__init__.py": HEAD_SRC, "tests/test_calc.py": HEAD_TESTS})
        if commit:
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "Add discount" + (f"\n\n{trailer}" if trailer else ""))
        return repo

    return build


@pytest.fixture
def py() -> str:
    """
    Give the interpreter running these tests, which has pytest and pytest-cov.

    :returns: Its path
    """
    return sys.executable


@pytest.fixture(autouse=True)
def no_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Run every test as if outside CI, even inside a pull request job.

    :param monkeypatch: Pytest's monkeypatch
    """
    for name in (
        "GITHUB_ACTIONS",
        "GITHUB_HEAD_REF",
        "GITHUB_WORKSPACE",
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_EVENT_PATH",
        "RURU_PR_AUTHOR",
        "RURU_PR_LABELS",
    ):
        monkeypatch.delenv(name, raising=False)
