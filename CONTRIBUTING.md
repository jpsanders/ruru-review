# Contributing

Issues and pull requests are welcome.

## Set up

```sh
git clone https://github.com/jpsanders/ruru-review && cd ruru-review
python3.15 -m venv .venv && . .venv/bin/activate
make init-dev
```

## Before a pull request

```sh
make format      # black and isort
make checks      # every check CI runs, in the same order
```

`make checks` runs isort, black, editorconfig-checker, darglint, flake8, mypy in strict mode, bandit, the man page
check and pytest with coverage. CI runs the same on Linux and macOS with Python 3.14 and 3.15. ruru also reviews
every pull request to itself: write a test for each mutant it reports as survived.

## Changing things

- **A new mutation operator** goes in `src/ruru_review/mutants.py` with a code, a hint and a test in
  `tests/ruru_review/test_mutants.py`. Keep operators that rarely make equivalent mutants.
- **A new tamper rule** goes in `src/ruru_review/tamper.py`, with a case in `tests/ruru_review/test_tamper_unit.py`
  and a row in the manual's rule table. Prefer medium severity when the change is often legitimate.
- **The report schema** is `ruru.report/1`. Adding a field is fine; removing or renaming one needs a new schema
  version, because agents and `ruru post` read it.
- **The manual** is `docs/MANUAL.md`. `make man` rebuilds the packaged copy and the man page from it, and
  `make checks` fails while either is stale.
- **No runtime dependencies.** ruru uses the standard library only; keep it that way.
- **Commits** are signed. Tags carry the bare version, `1.2.0`, never `v1.2.0`.
