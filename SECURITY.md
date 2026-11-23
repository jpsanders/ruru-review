# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's
[private vulnerability reporting](https://github.com/jpsanders/ruru-review/security/advisories/new),
not in a public issue. You will get an answer within a week.

## What ruru does on your machine

- **It runs your tests.** `ruru check` runs the project's own test command, in temporary git worktrees under the
  system temp folder, once for the baseline and once per mutant. Anything your tests do, they do again. It never
  edits your checkout: mutants are written into the worktrees, which are removed afterwards.
- **It sends nothing.** No model calls, no telemetry, no network access except `ruru post`, which calls the GitHub
  API with the token you give it.
- **`ruru mcp`** answers on stdin and stdout only; it opens no port.
- **`ruru man --install`** writes one file to your per user man folder.

## Untrusted pull requests

Never run pull request code with a write token. The two workflows in `examples/workflows/` split the work: the job
that runs the code has `contents: read`, and the job that comments (`ruru post`) has `pull-requests: write` but never
checks out the pull request. `ruru post` treats the report as untrusted: it checks the schema, drops malformed items
and paths outside the repository, checks that the pull request's head is the commit the trusted event names, and
escapes and truncates every string before it reaches a comment.

## What guards this repository

bandit and mypy in strict mode run on every push; Dependabot opens monthly updates; commits and tags are signed;
`main` is protected; PyPI releases use trusted publishing and wait for the maintainer's approval.
