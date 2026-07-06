# Contributing to ND3X

Thanks for your interest in ND3X. Contributions are welcome — with one ground rule:

> **Every change lands only through a Pull Request that the maintainer reviews and
> approves.** Nobody pushes directly to `main`. This is enforced with branch
> protection and `CODEOWNERS`, so an approved review from the maintainer is
> required before anything merges.

## How to contribute

1. **Open an issue first** for anything non-trivial (a bug, a feature, a design
   change) so we can agree on the approach before you invest time.
2. **Fork** the repository and create a branch from `main`
   (`feat/…`, `fix/…`, `docs/…`).
3. Make your change. Keep it focused — one logical change per PR.
4. **Run the checks** (see below) and make sure they pass.
5. Open a **Pull Request** against `main`, describe *what* and *why*, and link the
   issue. The maintainer will review; expect questions and requested changes.

The maintainer may decline changes that don't fit the project's direction — please
open an issue first to avoid wasted effort.

## Development checks

Back-end (Python):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # the test suite must stay green
```

- Follow the existing style (see `CLAUDE.md` / the code around you): `from __future__
  import annotations`, layered `routers → services → repository → models + schemas`,
  4-space indent, double quotes, lowercase builtin generics (`list[X]`).
- **Never hard-code a model.** Every stage resolves its model from a routing slot.
- **Never commit secrets or data** — no `.env`, no `*.db`/`*.sqlite`, no dumps.
  These are git-ignored; keep it that way.
- Add or update tests for behavioural changes.

## Licensing of contributions

ND3X is licensed under **AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)). By
submitting a contribution you agree that it is licensed under the same terms.

## Reporting security issues

Do **not** open a public issue. See [`SECURITY.md`](SECURITY.md).
