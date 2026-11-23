"""
GitHub output: annotations, SARIF, and ``ruru post``, which writes the summary comment and one review.

``ruru post`` is built to run with a write token in a workflow that never checks out pull request code. It trusts
nothing in the report: it checks the schema, checks that the pull request's head is the commit the trusted event
names, and escapes and truncates every string before it reaches a comment.
"""

import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ruru_review import __version__
from ruru_review.render import MARK, escape, markdown

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
ITEM_MARK = re.compile(r"<!-- ruru:id=([0-9A-Za-z_-]{1,40}) -->")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
_SAFE_PATH = re.compile(r"^[^\x00-\x1f]{1,400}$")
CHECKS = {"tamper", "fail_first", "mutant"}
SEVERITIES = {"high", "medium", "info"}


class PostError(RuntimeError):
    """The report or the event cannot be trusted, or GitHub refused: exit code 2."""


def _s(value: Any, limit: int = 300) -> str:
    """
    Coerce a report value to a bounded string.

    :param value: Any JSON value
    :param limit: Most characters
    :returns: A string
    """
    text = value if isinstance(value, str) else ""
    return text[:limit]


def validate(report: Any) -> dict[str, Any]:
    """
    Check a report from an untrusted artifact and keep only well formed fields.

    :param report: Parsed JSON
    :returns: A cleaned report
    :raises PostError: When the shape is wrong
    """
    if not isinstance(report, dict) or report.get("schema") != "ruru.report/1":
        raise PostError("not a ruru.report/1 report")
    head = report.get("head")
    if not isinstance(head, dict) or not _SHA.match(str(head.get("sha", ""))):
        raise PostError("report has no head SHA")
    s = report.get("summary")
    if not isinstance(s, dict):
        raise PostError("report has no summary")

    def num(d: Any, k: str) -> int:
        v = d.get(k, 0) if isinstance(d, dict) else 0
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**6 else 0

    m, ff, t, g = s.get("mutants", {}), s.get("fail_first", {}), s.get("tamper", {}), s.get("gate", {})
    score = m.get("score") if isinstance(m, dict) else None
    clean_score = (
        float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 1 else None
    )
    result = g.get("result") if isinstance(g, dict) else None
    reasons = g.get("reasons", []) if isinstance(g, dict) else []
    summary = {
        "mutants": {k: num(m, k) for k in ("total", "run", "killed", "survived", "timeout", "not_covered", "skipped")}
        | {"score": clean_score},
        "fail_first": {
            k: num(ff, k)
            for k in (
                "tests",
                "proves_change",
                "needs_new_code",
                "passes_without_change",
                "new_passes_without_change",
                "fails_on_head",
                "not_run",
            )
        },
        "tamper": {"high": num(t, "high"), "medium": num(t, "medium")},
        "gate": {
            "result": result if result in ("pass", "fail", "advise") else "advise",
            "reasons": [escape(_s(r, 200)) for r in reasons[:10]] if isinstance(reasons, list) else [],
        },
    }
    items = []
    raw = report.get("items", [])
    for it in raw[:2000] if isinstance(raw, list) else []:
        if not isinstance(it, dict) or it.get("check") not in CHECKS or it.get("severity") not in SEVERITIES:
            continue
        line, file, iid = it.get("line"), _s(it.get("file"), 400), _s(it.get("id"), 40)
        if not (isinstance(line, int) and 0 < line < 10**7 and _SAFE_PATH.match(file) and _ID.match(iid)):
            continue
        if ".." in file.split("/") or file.startswith("/"):
            continue
        tests = it.get("tests", [])
        items.append(
            {
                "id": iid,
                "check": it["check"],
                "file": file,
                "line": line,
                "rule": _s(it.get("rule"), 40),
                "before": _s(it.get("before"), 200),
                "after": _s(it.get("after"), 200),
                "tests": [_s(x, 200) for x in tests[:5]] if isinstance(tests, list) else [],
                "outcome": _s(it.get("outcome"), 40),
                "severity": it["severity"],
                "side": "LEFT" if it.get("side") == "LEFT" else "RIGHT",
                "hint": _s(it.get("hint"), 300),
            }
        )
    return {
        "schema": "ruru.report/1",
        "tool": {"name": "ruru", "version": escape(_s((report.get("tool") or {}).get("version"), 40))},
        "base": {"ref": escape(_s((report.get("base") or {}).get("ref"), 100)), "merge_base": ""},
        "head": {"sha": head["sha"], "working_tree": False},
        "agent": {"agent": bool((report.get("agent") or {}).get("agent")), "reasons": []},
        "summary": summary,
        "items": items,
        "notes": [],
    }


def findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Rank the items worth a comment, most valuable first.

    High tamper findings, then survivors in the most changed files, then fail first findings, then the rest.

    :param report: A report
    :returns: Items
    """
    worth = [i for i in report["items"] if i["severity"] in ("high", "medium")]
    per_file = Counter(i["file"] for i in worth if i["check"] == "mutant")
    rank = {"tamper": 0, "mutant": 1, "fail_first": 2}
    return sorted(
        worth,
        key=lambda i: (i["severity"] != "high", rank[i["check"]], -per_file[i["file"]], i["file"], i["line"], i["id"]),
    )


def _code(text: str) -> str:
    """
    Wrap untrusted text in a code span. Inside one, GitHub renders HTML and mentions as plain text.

    :param text: Any text
    :returns: Inline code
    """
    text = text.replace("`", "'").replace("\r", " ").replace("\n", " ")
    return f"`{text[:159] + '…' if len(text) > 160 else text}`"


def comment_body(item: dict[str, Any]) -> str:
    """
    Write one inline comment: the gap and a hint for a test, never a code change.

    :param item: An item
    :returns: Markdown
    """
    if item["check"] == "mutant":
        what = escape(item["outcome"].replace("_", " "))
        head = f"**ruru: {what} mutant {escape(item['rule'])}** {_code(item['before'])} to {_code(item['after'])}"
        tests = ", ".join(_code(t) for t in item["tests"][:3])
        tail = f"\n\nTests run: {tests}" if tests else ""
        body = f"{head}\n\n{escape(item['hint'])}{tail}"
    elif item["check"] == "tamper":
        body = f"**ruru: tamper, {escape(item['rule'])}**\n\n{escape(item['hint'])}"
        if item["after"]:
            body += f"\n\nNow: {_code(item['after'])}"
    else:
        body = f"**ruru: fail first, {escape(item['rule'].replace('_', ' '))}**\n\n{escape(item['hint'])}"
    return f"{body}\n\n<!-- ruru:id={item['id']} -->"


def annotations(report: dict[str, Any], limit: int = 10) -> Iterator[str]:
    """
    Make GitHub workflow commands that mark findings on the diff.

    :param report: A report
    :param limit: Most lines
    :yields: ``::warning`` lines
    """

    def prop(text: str) -> str:
        return (
            text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")
        )

    def msg(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    for i in findings(report)[:limit]:
        what = f"{i['before']} -> {i['after']}: {i['hint']}" if i["check"] == "mutant" else i["hint"]
        title = prop(f"ruru {i['check']} {i['rule']}")
        yield f"::warning file={prop(i['file'])},line={i['line']},title={title}::{msg(what)}"


def sarif(report: dict[str, Any]) -> dict[str, Any]:
    """
    Convert the report to SARIF 2.1.0, for code scanning.

    :param report: A report
    :returns: A SARIF log
    """
    worth = findings(report)
    rules = sorted({f"{i['check']}/{i['rule']}" for i in worth})
    results = []
    for i in worth:
        text = f"{i['before']} -> {i['after']}: {i['hint']}" if i["check"] == "mutant" else i["hint"]
        results.append(
            {
                "ruleId": f"{i['check']}/{i['rule']}",
                "level": "error" if i["severity"] == "high" else "warning",
                "message": {"text": text},
                "partialFingerprints": {"ruruId": i["id"]},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": i["file"]}, "region": {"startLine": i["line"]}}}
                ],
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ruru",
                        "version": __version__,
                        "informationUri": "https://github.com/jpsanders/ruru-review",
                        "rules": [{"id": r, "shortDescription": {"text": r.replace("/", ": ")}} for r in rules],
                    }
                },
                "results": results,
            }
        ],
    }


class GitHub:
    """A small GitHub REST client on urllib."""

    def __init__(self, token: str, repo: str, api: str = API) -> None:
        """
        Prepare a client.

        :param token: A token with pull request write access
        :param repo: ``owner/name``
        :param api: API root
        :raises PostError: On a malformed repository name
        """
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            raise PostError(f"bad repository name {repo!r}")
        self.token, self.repo, self.api = token, repo, api.rstrip("/")

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """
        Make one API call.

        :param method: HTTP method
        :param path: Path under the repository, such as ``/pulls/3``
        :param body: JSON body
        :returns: Parsed JSON
        :raises PostError: When GitHub refuses
        """
        url = path if path.startswith("https://") else f"{self.api}/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # nosec B310: https API URL
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"ruru/{__version__}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310: https API URL
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise PostError(f"GitHub {method} {path}: {exc.code} {detail}") from exc
        return json.loads(raw) if raw else None

    def pages(self, path: str) -> list[Any]:
        """
        Read every page of a list endpoint.

        :param path: Path under the repository
        :returns: All entries
        """
        out: list[Any] = []
        for page in range(1, 31):
            sep = "&" if "?" in path else "?"
            got = self.call("GET", f"{path}{sep}per_page=100&page={page}")
            out += got
            if len(got) < 100:
                break
        return out


def commentable(files: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    """
    List the (path, side, line) triples a review comment may use: lines inside the pull request's diff hunks.

    :param files: ``GET /pulls/N/files``
    :returns: Allowed positions
    """
    ok: set[tuple[str, str, int]] = set()
    hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for f in files:
        name, patch = f.get("filename", ""), f.get("patch") or ""
        old = new = 0
        for line in patch.splitlines():
            m = hunk.match(line)
            if m:
                old, new = int(m[1]), int(m[3])
                continue
            if line.startswith("+"):
                ok.add((name, "RIGHT", new))
                new += 1
            elif line.startswith("-"):
                ok.add((name, "LEFT", old))
                old += 1
            else:
                ok.add((name, "RIGHT", new))
                ok.add((name, "LEFT", old))
                old += 1
                new += 1
    return ok


def trusted_target(event: dict[str, Any], pr_arg: int | None) -> tuple[int, str]:
    """
    Read the pull request number and head SHA from the trusted event.

    :param event: The workflow event payload
    :param pr_arg: The number the untrusted artifact gave, for ``workflow_run``
    :returns: (number, head SHA)
    :raises PostError: When the event names neither
    """
    if "pull_request" in event and isinstance(event["pull_request"], dict):
        pr = event["pull_request"]
        return int(pr["number"]), str(pr["head"]["sha"])
    run = event.get("workflow_run")
    if isinstance(run, dict):
        sha = str(run.get("head_sha", ""))
        listed = [p.get("number") for p in run.get("pull_requests") or [] if isinstance(p, dict)]
        number = listed[0] if listed else pr_arg
        if not isinstance(number, int) or not _SHA.match(sha):
            raise PostError("the workflow_run event names no pull request: pass --pr from the artifact")
        return number, sha
    raise PostError("run ruru post from a pull_request or workflow_run event")


def post(report_path: Path, gh: GitHub, event: dict[str, Any], pr_arg: int | None, cap: int) -> dict[str, Any]:
    """
    Write or update the summary comment and post one review with the new inline comments.

    :param report_path: The report from the read only run
    :param gh: Client
    :param event: The trusted event payload
    :param pr_arg: Pull request number from the artifact (checked against the event)
    :param cap: Most inline comments
    :returns: What was done
    :raises PostError: When anything does not match
    """
    try:
        report = validate(json.loads(report_path.read_text(encoding="utf-8")[: 20 * 1024 * 1024]))
    except (OSError, ValueError) as exc:
        raise PostError(f"cannot read the report: {exc}") from exc
    number, sha = trusted_target(event, pr_arg)
    pr = gh.call("GET", f"/pulls/{number}")
    if pr["head"]["sha"] != sha:
        raise PostError(f"pull request #{number} head is {pr['head']['sha'][:10]}, the event names {sha[:10]}")
    if report["head"]["sha"] != sha:
        raise PostError("the report was made for another commit")

    body = markdown(report)
    mine = [c for c in gh.pages(f"/issues/{number}/comments") if MARK in (c.get("body") or "")]
    if mine:
        gh.call("PATCH", f"/issues/comments/{mine[0]['id']}", {"body": body})
        summary = "updated"
    else:
        gh.call("POST", f"/issues/{number}/comments", {"body": body})
        summary = "created"

    posted = set()
    for c in gh.pages(f"/pulls/{number}/comments"):
        posted.update(ITEM_MARK.findall(c.get("body") or ""))
    allowed = commentable(gh.pages(f"/pulls/{number}/files"))
    comments: list[dict[str, Any]] = []
    for i in findings(report):
        if len(comments) >= cap:
            break
        if i["id"] in posted or (i["file"], i["side"], i["line"]) not in allowed:
            continue
        comments.append({"path": i["file"], "line": i["line"], "side": i["side"], "body": comment_body(i)})
    if comments:
        gh.call(
            "POST",
            f"/pulls/{number}/reviews",
            {
                "commit_id": sha,
                "event": "COMMENT",
                "body": f"ruru found {len(comments)} new gap(s) in the tests.",
                "comments": comments,
            },
        )
    return {"pull_request": number, "summary_comment": summary, "inline_comments": len(comments)}
