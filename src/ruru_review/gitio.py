"""
Git plumbing: the merge base, the changed lines of a diff, file text at a commit, and worktrees.

The base of a review is the merge base of the target branch and the head. The head is a commit (in CI) or ``None``,
meaning the working tree, so an agent can check its work before it commits.
"""

import re
import shutil
import subprocess  # nosec B404: runs git with argument lists, never a shell
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: diff against it to see everything
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    """A git command failed."""


def git(repo: Path, *args: str, check: bool = True, stdin: bytes | None = None) -> str:
    """
    Run git in ``repo`` and return its standard output.

    :param repo: Repository folder
    :param args: Git arguments
    :param check: Raise on a non zero exit
    :param stdin: Bytes for standard input
    :returns: Standard output as text
    :raises GitError: When git fails and ``check`` is set
    """
    proc = subprocess.run(  # nosec B603 B607: git with an argument list
        ["git", "-c", "core.quotepath=off", *args], cwd=repo, input=stdin, capture_output=True
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def toplevel(path: Path) -> Path:
    """
    Find the root of the repository that holds ``path``.

    :param path: Any folder inside a repository
    :returns: The repository root
    """
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def resolve(repo: Path, ref: str) -> str:
    """
    Resolve a ref to a full commit SHA.

    :param repo: Repository folder
    :param ref: Branch, tag or SHA
    :returns: The commit SHA
    """
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def default_base(repo: Path) -> str:
    """
    Pick the target branch when none is given: origin's HEAD, then main, then master.

    :param repo: Repository folder
    :returns: A ref that exists
    :raises GitError: When none of them exists
    """
    head = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    for ref in (head, "origin/main", "main", "origin/master", "master"):
        if ref and git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip():
            return ref
    raise GitError("no base branch found: pass --base")


def merge_base(repo: Path, base: str, head: str | None) -> str:
    """
    Find the merge base of the target branch and the head.

    :param repo: Repository folder
    :param base: Target branch
    :param head: Head commit, or ``None`` for the working tree (whose commit is HEAD)
    :returns: The merge base SHA
    """
    return git(repo, "merge-base", base, head or "HEAD").strip()


@dataclass
class FileChange:
    """One file in the diff: its paths on both sides and the lines the head added or modified."""

    old_path: str | None
    new_path: str | None
    added: set[int] = field(default_factory=set)
    removed: set[int] = field(default_factory=set)

    @property
    def path(self) -> str:
        """
        The path to report: the head's, or the base's for a deleted file.

        :returns: A path relative to the root
        """
        return self.new_path or self.old_path or ""


def parse_diff(text: str) -> list[FileChange]:
    """
    Read a ``git diff -U0`` into changed files and line numbers.

    :param text: The diff
    :returns: One entry per file
    """
    files: list[FileChange] = []
    cur: FileChange | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = FileChange(None, None)
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("--- "):
            cur.old_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("+++ "):
            cur.new_path = None if line[4:] == "/dev/null" else line[6:]
        elif line.startswith("rename from "):
            cur.old_path = line[12:]
        elif line.startswith("rename to "):
            cur.new_path = line[10:]
        elif m := _HUNK.match(line):
            o_start, o_len = int(m[1]), int(m[2] if m[2] is not None else 1)
            n_start, n_len = int(m[3]), int(m[4] if m[4] is not None else 1)
            cur.removed.update(range(o_start, o_start + o_len))
            cur.added.update(range(n_start, n_start + n_len))
    return files


def changed_files(repo: Path, base: str, head: str | None) -> list[FileChange]:
    """
    List the files a change touches, with its added lines, untracked files included for the working tree.

    :param repo: Repository folder
    :param base: The merge base SHA
    :param head: Head commit, or ``None`` for the working tree
    :returns: One entry per changed file
    """
    args = ["diff", "-U0", "--no-color", "--no-ext-diff", "-M", base]
    if head:
        args.append(head)
    files = parse_diff(git(repo, *args))
    if head is None:
        for path in git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if path:
                text = (repo / path).read_text(encoding="utf-8", errors="replace")
                files.append(FileChange(None, path, set(range(1, text.count("\n") + 2))))
    return files


def read_at(repo: Path, ref: str | None, path: str) -> str | None:
    """
    Read a file at a commit, or from the working tree when ``ref`` is ``None``.

    :param repo: Repository folder
    :param ref: Commit, or ``None`` for the working tree
    :param path: Path relative to the root
    :returns: Its text, or ``None`` when it does not exist there
    """
    if ref is None:
        p = repo / path
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    proc = subprocess.run(  # nosec B603 B607: git with an argument list
        ["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True
    )
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else None


class Worktree:
    """A detached git worktree in a temporary folder, removed on exit."""

    def __init__(self, repo: Path, commit: str, apply_working_tree: bool = False) -> None:
        """
        Prepare a worktree.

        :param repo: Repository folder
        :param commit: Commit to check out
        :param apply_working_tree: Also apply the uncommitted changes and untracked files of ``repo``
        """
        self.repo = repo
        self.commit = commit
        self.apply = apply_working_tree
        self.path = Path()

    def __enter__(self) -> Path:
        """
        Create the worktree.

        :returns: Its folder
        """
        self.path = Path(tempfile.mkdtemp(prefix="ruru-wt-")) / "tree"
        git(self.repo, "worktree", "add", "--detach", "-q", str(self.path), self.commit)
        if self.apply:
            patch = subprocess.run(  # nosec B603 B607: git with an argument list
                ["git", "diff", "--binary", "HEAD"], cwd=self.repo, capture_output=True, check=True
            ).stdout
            if patch.strip():
                git(self.path, "apply", "--whitespace=nowarn", "-", stdin=patch)
            for rel in git(self.repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
                if rel:
                    dest = self.path / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.repo / rel, dest)
        return self.path

    def __exit__(self, *exc: object) -> None:
        """
        Remove the worktree and its folder.

        :param exc: Exception details, ignored
        """
        git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self.path.parent, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)
