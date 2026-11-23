"""Reports for people: the terminal score card and Markdown."""

from typing import Any

MARK = "<!-- ruru -->"


def _score(s: float | None) -> str:
    return "n/a" if s is None else f"{s:.0%}"


def terminal(report: dict[str, Any], limit: int = 20) -> str:
    """
    Print a short score card per check, then the survivors and findings.

    :param report: A report
    :param limit: Most items to list
    :returns: Text
    """
    s = report["summary"]
    m, ff, t, g = s["mutants"], s["fail_first"], s["tamper"], s["gate"]
    head = report["head"]
    lines = [
        f"ruru {report['tool']['version']}  base {report['base']['ref']}"
        f"{'' if report['base']['ref'].startswith('(') else ' (' + report['base']['merge_base'][:10] + ')'}  "
        f"head {'working tree' if head['working_tree'] else head['sha'][:10]}",
        f"  tamper      {t['high']} high, {t['medium']} medium",
        f"  fail first  {ff['tests']} test(s): {ff['proves_change'] + ff['needs_new_code']} prove the change, "
        f"{ff['passes_without_change']} pass without it, {ff['fails_on_head']} fail at the head",
        f"  mutants     score {_score(m['score'])}: {m['killed'] + m['timeout']} killed, {m['survived']} survived, "
        f"{m['not_covered']} not covered, {m['skipped']} skipped, of {m['total']}",
    ]
    who = "agent" if report["agent"]["agent"] else "human"
    why = f" ({', '.join(report['agent']['reasons'])})" if report["agent"]["reasons"] else ""
    lines.append(f"  gate        {g['result'].upper()} for {'an' if who == 'agent' else 'a'} {who} change{why}")
    for r in g["reasons"]:
        lines.append(f"              - {r}")
    shown = [i for i in report["items"] if i["severity"] in ("high", "medium")][:limit]
    if shown:
        lines.append("")
    for i in shown:
        if i["check"] == "mutant":
            lines.append(
                f"{i['outcome'].upper():<12}{i['file']}:{i['line']} {i['rule']} "
                f"{i['before']!r} -> {i['after']!r}  [{i['id']}]"
            )
        else:
            lines.append(f"{i['check'].upper().replace('_', ' '):<12}{i['file']}:{i['line']} {i['rule']}: {i['hint']}")
    for n in report["notes"]:
        lines.append(f"note: {n}")
    return "\n".join(lines) + "\n"


def markdown(report: dict[str, Any], limit: int = 15) -> str:
    """
    Write the summary comment and job summary.

    :param report: A report
    :param limit: Most items to list
    :returns: Markdown starting with the hidden marker
    """
    s = report["summary"]
    m, ff, t, g = s["mutants"], s["fail_first"], s["tamper"], s["gate"]
    icon = {"pass": "Pass", "fail": "Fail", "advise": "Advice"}[g["result"]]  # nosec B105: a heading word
    out = [
        MARK,
        f"### ruru: {icon}",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Tamper audit | {t['high']} high, {t['medium']} medium |",
        f"| Fail first | {ff['tests']} tests; {ff['passes_without_change']} pass without the change |",
        f"| Mutants on the diff | score {_score(m['score'])}; {m['survived']} survived, "
        f"{m['not_covered']} not covered, of {m['total']} |",
        "",
    ]
    for r in g["reasons"]:
        out.append(f"- {r}")
    rows = [i for i in report["items"] if i["severity"] in ("high", "medium")][:limit]
    if rows:
        out += ["", "| Where | Check | What |", "|---|---|---|"]
        for i in rows:
            what = escape(i["hint"])
            if i["check"] == "mutant":
                what = f"{code(i['before'])} to {code(i['after'])}: {what}"
            out.append(
                f"| {code(i['file'] + ':' + str(i['line']))} | {escape(i['check'] + ' ' + i['rule'])} | {what} |"
            )
    return "\n".join(out) + "\n"


def code(text: str, limit: int = 120) -> str:
    """
    Wrap untrusted text in a code span for a Markdown table cell.

    :param text: Any text
    :param limit: Most characters kept
    :returns: Inline code with backticks, newlines and pipes neutralised
    """
    text = text.replace("`", "'").replace("\r", " ").replace("\n", " ")
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return "`" + text.replace("|", "\\|") + "`"


def escape(text: str, limit: int = 300) -> str:
    """
    Make untrusted text safe for a Markdown table cell: no pipes, newlines, HTML or mentions.

    :param text: Any text
    :param limit: Most characters kept
    :returns: Safe text
    """
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("@", "@​")
    return text if len(text) <= limit else text[: limit - 1] + "…"
