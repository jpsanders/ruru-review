import pytest

from ruru_review import tamper
from ruru_review.gitio import parse_diff
from ruru_review.pyast import is_source_file, is_test_file


def rules(before: str | None, after: str | None) -> list[str]:
    return [f.rule for f in tamper.audit_test_file("tests/test_x.py", "tests/test_x.py", before, after)]


@pytest.mark.parametrize(
    "before, after, expected",
    [
        (
            "def test_a():\n    assert f() == 1\n    assert g() == 2\n",
            "def test_a():\n    assert f() == 1\n",
            ["assertion-removed"],
        ),
        ("def test_a():\n    assert f() == 1\n", "def test_a():\n    assert f()\n", ["assertion-loosened"]),
        ("def test_a():\n    assert f() == 1\n", "def test_a():\n    assert f() is not None\n", ["assertion-loosened"]),
        (
            "def test_a():\n    assert f() == 1\n",
            "def test_a():\n    assert True\n",
            ["assertion-loosened", "assert-true"],
        ),
        ("def test_a():\n    assert f() == 1\n", "def test_b():\n    assert f() == 1\n", []),
        (
            "def test_a():\n    assert f() == 1\n",
            "def test_parse_json_file():\n    data = load('data.json')\n    assert data['k'] == [1, 2]\n",
            ["test-removed"],
        ),
        (
            "def test_a():\n    assert f() == 1\n",
            "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert f() == 1\n",
            ["skip-added"],
        ),
        (
            "def test_a():\n    assert f() == 1\n",
            "def test_a():\n    pytest.skip('x')\n    assert f() == 1\n",
            ["skip-added"],
        ),
        (
            "def test_a():\n    assert f() == 1\n",
            "def test_a():\n    try:\n        assert f() == 1\n    except AssertionError:\n        pass\n",
            ["assertion-in-try", "except-pass"],
        ),
        (
            "def test_a():\n    with pytest.raises(KeyError):\n        f()\n",
            "def test_a():\n    with pytest.raises(Exception):\n        f()\n",
            ["assertion-loosened"],
        ),
        (
            "class T:\n    def test_a(self):\n        self.assertEqual(f(), 1)\n",
            "class T:\n    def test_a(self):\n        self.assertTrue(f())\n",
            [],
        ),
        (
            "class TestT:\n    def test_a(self):\n        self.assertEqual(f(), 1)\n",
            "class TestT:\n    def test_a(self):\n        self.assertTrue(f())\n",
            ["assertion-loosened"],
        ),
        (
            "def test_a():\n    assert f() == pytest.approx(1, rel=0.01)\n",
            "def test_a():\n    assert f() == pytest.approx(1, rel=0.5)\n",
            ["approx-widened"],
        ),
        (
            "@pytest.mark.parametrize('a', [1, 2, 3])\ndef test_a(a):\n    assert f(a) == a\n",
            "@pytest.mark.parametrize('a', [1])\ndef test_a(a):\n    assert f(a) == a\n",
            ["cases-removed"],
        ),
        ("def test_a():\n    assert f() == 1\n", "def test_a():\n    assert f() == 2\n", []),
        (None, "def test_new():\n    x = 1\n", ["empty-test"]),
        ("x = 1\n", "import pytest\npytestmark = pytest.mark.skip\nx = 1\n", ["skip-added"]),
        ("def test_a():\n    assert f() == 1\n", None, ["test-removed"]),
    ],
)
def test_test_file_rules(before: str | None, after: str | None, expected: list[str]) -> None:
    assert rules(before, after) == expected


def test_conftest_hook() -> None:
    after = "def pytest_collection_modifyitems(items):\n    items.clear()\n"
    assert [f.rule for f in tamper.audit_conftest("conftest.py", "", after)] == ["harness-hook"]
    assert tamper.audit_conftest("conftest.py", after, after) == []


def test_config_rules() -> None:
    before = "[tool.pytest.ini_options]\naddopts = '-q'\n[tool.coverage.report]\nfail_under = 90\n"
    after = (
        "[tool.pytest.ini_options]\naddopts = '-q --deselect tests/test_x.py::test_a'\n"
        "[tool.coverage.run]\nomit = ['src/x.py']\n[tool.coverage.report]\nfail_under = 50\n"
    )
    found = tamper.audit_config("pyproject.toml", before, after, {2, 4, 6})
    assert [f.rule for f in found] == ["addopts-deselect", "coverage-omit", "coverage-lowered"]


def test_workflow_and_pragma_rules() -> None:
    wf = "steps:\n  - run: pytest || true\n  - run: x\n    continue-on-error: true\n"
    assert len(tamper.audit_workflow(".github/workflows/ci.yml", wf, {2, 4})) == 2
    src = "def f():  # pragma: no cover\n    pass\n"
    assert [f.severity for f in tamper.audit_source_pragmas("a.py", src, {1})] == ["medium"]


def test_file_kinds() -> None:
    dirs = ["tests"]
    assert (
        is_test_file("tests/test_a.py", dirs)
        and is_test_file("pkg/a_test.py", dirs)
        and is_test_file("tests/helpers.py", dirs)
    )
    assert not is_test_file("tests/conftest.py", dirs) and not is_test_file("tests/__init__.py", dirs)
    assert (
        is_source_file("src/pkg/a.py", dirs)
        and not is_source_file("setup.py", dirs)
        and not is_source_file("README.md", dirs)
    )
    assert not is_source_file(".github/x.py", dirs)


def test_parse_diff() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -2,0 +3,2 @@\n+x\n+y\n@@ -9 +11 @@\n-a\n+b\n"
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    a, r, g = parse_diff(diff)
    assert (a.path, a.added) == ("a.py", {3, 4, 11})
    assert (r.old_path, r.new_path, r.added) == ("old.py", "new.py", set())
    assert (g.new_path, g.path, g.removed) == (None, "gone.py", {1, 2})


def test_renamed_test_is_compared_not_removed() -> None:
    before = "def test_room_temp_low():\n    x = read()\n    assert x.low == 18\n    assert x.high == 21\n"
    after = "def test_room_low():\n    x = read()\n    assert x.low == 18\n"
    found = tamper.audit_test_file("t.py", "t.py", before, after)
    assert [(f.rule, f.message) for f in found] == [
        ("assertion-removed", "test_room_low (renamed from test_room_temp_low) has 1 fewer assertion(s) than before")
    ]


def test_conditional_skips_are_medium() -> None:
    base = "def test_a():\n    assert f() == 1\n"
    cond = (
        "import shutil\n@pytest.mark.skipif(shutil.which('git') is None, reason='git')\n"
        "def test_a():\n    assert f() == 1\n"
    )
    inside = "def test_a():\n    if not ok():\n        pytest.skip('no')\n    assert f() == 1\n"
    new = "@pytest.mark.skipif(True, reason='x')\ndef test_new():\n    assert f() == 1\n"
    for after in (cond, inside):
        assert [(f.rule, f.severity) for f in tamper.audit_test_file("t.py", "t.py", base, after)] == [
            ("skip-added", "medium")
        ]
    assert [(f.rule, f.severity) for f in tamper.audit_test_file("t.py", "t.py", base, base + new)] == [
        ("skip-added", "medium")
    ]
    mod = "pytest.importorskip('numpy')\n" + base
    assert [(f.rule, f.severity) for f in tamper.audit_test_file("t.py", "t.py", base, mod)] == [
        ("skip-added", "medium")
    ]


def test_new_coverage_config_is_not_tampering() -> None:
    after = "[tool.coverage.run]\nomit = ['src/x/__main__.py']\n"
    assert tamper.audit_config("pyproject.toml", "[project]\nname = 'x'\n", after, {1, 2}) == []
    assert tamper.audit_config("pyproject.toml", None, after, {1, 2}) == []
