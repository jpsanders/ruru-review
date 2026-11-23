# Changelog

Versions follow [semantic versioning](https://semver.org). Tags carry the bare version, such as `1.0.0`.

## 1.0.0 (2026-11-21)

The first release.

- `ruru check --all` scores a whole repository's tests; `ruru mutants --all` lists every mutant.
- A renamed test is compared with its old self instead of being reported as removed.
- `skipif`, `importorskip` and a skip inside an `if` are medium findings; a bare skip or xfail stays high.
- Coverage `omit` and `exclude` lines count as tamper only when they change existing coverage configuration.
- Fail first gates on new tests that pass without the change; an edited old test that passes on both sides is
  medium, and a change with no source (new tests for existing code) gets advice only.
- GitHub's pull request variables apply only when ruru reviews the CI checkout itself.
- Fail first stubs a package that does not exist at the base, so an installed copy cannot stand in for it.
- The command line imports only what each command needs: `ruru --version` starts in about 40 ms.
- Coverage based test selection forces coverage's ctrace core, because the sysmon core (the default on Python 3.14
  and later) records only the first test to reach each line.

- `ruru check`: the tamper audit, fail first and mutants on the diff, with a JSON report (schema `ruru.report/1`),
  a Markdown summary and the GitHub job summary.
- Fail first lays the head's test files over a worktree at the base; imports of names missing on the base become
  placeholders, so only the tests that use new code count as needing it.
- Mutants: nine operators on changed lines only, arid nodes never mutated, `# ruru: ignore` pragmas, sampling seeded
  by the head SHA so reruns agree.
- Runner: one git worktree per worker, an import check against installed copies shadowing the worktree, a baseline
  run, test selection by pytest-cov contexts, timeouts that kill the whole process group, a time budget.
- Agent detection by branch prefix, co author trailer, label or bot author; the gate enforces for agents and advises
  for people.
- `ruru audit`, `ruru mutants`, `ruru verify`, `ruru guide`, `ruru man`.
- `ruru post`: one summary comment updated in place and one review of at most five inline comments, deduplicated
  across runs; it validates the report, checks the pull request head against the trusted event, and escapes every
  string. SARIF 2.1.0 (`--sarif`), `::warning` annotations and step outputs in GitHub Actions.
- The composite action "ruru PR reviewer" and two fork safe workflow templates; ruru runs them on its own pull
  requests.
- `ruru mcp`: five tools over stdio for coding agents. A Claude Code plugin with the `ruru` skill, and an opt in Stop
  hook example.
