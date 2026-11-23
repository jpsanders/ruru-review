"""
Fail first: does each new or changed test fail without the change?

The new tests run twice: at the head, and in a worktree at the base with only the head's test files laid over it. A
test that passes on both sides passes without the change, so it proves nothing about it.
"""

import ast
import shutil
from dataclasses import dataclass
from pathlib import Path

from ruru_review import gitio, pyast
from ruru_review.runner import Project, run_selected

PROVES, PROVES_IMPORT, PASSES_WITHOUT, HEAD_FAILS, NOT_RUN = (
    "proves_change",
    "needs_new_code",
    "passes_without_change",
    "fails_on_head",
    "not_run",
)


MISSING_MARK = "ruru-missing-on-base"
_SHIM = f"""
class _RuruMissing:
    def __init__(self, name):
        object.__setattr__(self, "_n", name)

    def _fail(self, *a, **k):
        raise ImportError("{MISSING_MARK}: " + object.__getattribute__(self, "_n"))

    __call__ = __getattr__ = __getitem__ = __iter__ = __bool__ = __eq__ = __hash__ = _fail
"""


def lenient_imports(text: str) -> str:
    """
    Rewrite a test module's top level imports so names missing on the base become placeholders.

    A test that uses new code then fails on the base on its own, instead of the whole module failing to import and
    hiding which tests really depend on the change.

    :param text: The head's test module
    :returns: The rewritten module, or the text unchanged when it does not parse
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    body: list[ast.stmt] = []
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module != "__future__"
            and not any(a.name == "*" for a in node.names)
        ):
            for alias in node.names:
                one = ast.ImportFrom(node.module, [alias], node.level)
                local = alias.asname or alias.name
                full = f"{'.' * node.level}{node.module or ''}.{alias.name}"
                fallback = ast.parse(f"{local} = _RuruMissing({full!r})").body
                body.append(ast.Try([one], [ast.ExceptHandler(ast.Name("ImportError"), None, fallback)], [], []))
        else:
            body.append(node)
    shim = ast.parse(_SHIM).body
    future = [n for n in body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    rest = [n for n in body if n not in future]
    lead = (
        1 if rest and isinstance(rest[0], ast.Expr) and isinstance(getattr(rest[0], "value", None), ast.Constant) else 0
    )
    tree.body = rest[:lead] + future + shim + rest[lead:] if not future else future + rest[:lead] + shim + rest[lead:]
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


@dataclass
class TestVerdict:
    """What one new or changed test showed."""

    node_id: str
    file: str
    line: int
    status: str
    base: str
    head: str
    new: bool


def changed_tests(
    repo: Path, base: str, head: str | None, files: list[gitio.FileChange], test_dirs: list[str]
) -> list[tuple[str, str, int, bool]]:
    """
    Find test functions the change adds or edits, by comparing each function's AST by qualified name.

    :param repo: Repository root
    :param base: Merge base
    :param head: Head commit or ``None``
    :param files: Changed files
    :param test_dirs: Test folder names
    :returns: (node id, file, line, is new) for each
    """
    out = []
    for f in files:
        if not f.new_path or not pyast.is_test_file(f.new_path, test_dirs):
            continue
        before = pyast.test_functions(pyast.parse(gitio.read_at(repo, base, f.old_path) if f.old_path else None))
        after = pyast.test_functions(pyast.parse(gitio.read_at(repo, head, f.new_path)))
        for name, node in after.items():
            old = before.get(name)
            if old is None or pyast.dump(old) != pyast.dump(node):
                out.append((f"{f.new_path}::{name}", f.new_path, node.lineno, old is None))
    return out


def check(
    project: Project,
    head_tree: Path,
    base_commit: str,
    tests: list[tuple[str, str, int, bool]],
    test_files: list[str],
    timeout: float,
    packages: list[str] | None = None,
) -> list[TestVerdict]:
    """
    Run the changed tests at the head and at the base with the head's test files.

    :param project: The project
    :param head_tree: A worktree at the head
    :param base_commit: The merge base
    :param tests: From :func:`changed_tests`
    :param test_files: The head's changed test files to lay over the base
    :param timeout: Seconds per run
    :param packages: The changed packages; any missing at the base gets a stub that fails to import, so an installed
        copy of the head's package cannot stand in for it
    :returns: One verdict per test
    """
    if not tests:
        return []
    ids = [t[0] for t in tests]
    head_proc, head_out, _ = run_selected(project, head_tree, ids, timeout)
    with gitio.Worktree(project.root, base_commit) as base_tree:
        src_root = base_tree / project.src_root
        for name in packages or []:
            if not (src_root / name).exists() and not (src_root / f"{name}.py").exists():
                (src_root / name).mkdir(parents=True, exist_ok=True)
                (src_root / name / "__init__.py").write_text(f"raise ImportError('{MISSING_MARK}: {name}')\n")
        for rel in test_files:
            dest = base_tree / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(lenient_imports((head_tree / rel).read_text(encoding="utf-8")), encoding="utf-8")
            for init in (head_tree / rel).parents:  # packages that hold new test folders
                if init == head_tree:
                    break
                src_init = init / "__init__.py"
                dst_init = base_tree / src_init.relative_to(head_tree)
                if src_init.exists() and not dst_init.exists():
                    shutil.copy2(src_init, dst_init)
        _, base_out, base_msgs = run_selected(project, base_tree, ids, timeout, messages=True)
    verdicts = []
    for node_id, file, line, new in tests:
        h = head_out.get(node_id, "error" if head_proc.code not in (0, 1) else NOT_RUN)
        b = base_out.get(node_id, "collection_error")
        if h == "skipped":
            status = NOT_RUN
        elif h != "passed":
            status = HEAD_FAILS
        elif b in ("failed", "error"):
            status = PROVES_IMPORT if MISSING_MARK in base_msgs.get(node_id, "") else PROVES
        elif b == "collection_error":
            status = PROVES_IMPORT
        elif b == "passed":
            status = PASSES_WITHOUT
        else:
            status = NOT_RUN
        verdicts.append(TestVerdict(node_id, file, line, status, b, h, new))
    return verdicts
