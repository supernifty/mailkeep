# Contributing to Mailkeep

Thanks for your interest in improving Mailkeep.

## Specification-first workflow

Every new feature or intentional behavior change starts in [`specs/`](specs/README.md).
Write or update the relevant specification before implementing it, and keep the
specification synchronized with the code. The specifications are the source of
truth for intended behavior and acceptance criteria.

## Development setup

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --extra dev
mkdir -p tmp
TMPDIR="$PWD/tmp" uv run pytest
```

The optional `jev` extra (`uv sync --extra jev`) is only needed for the hosted
Typesafe enrichment backend.

## Guidelines

- Keep changes focused and consistent with the existing structure and style.
- Update `specs/` and `README.md` when behavior or usage changes.
- Add or update tests; the full suite must pass. CI runs it on macOS and Linux.
- Never commit real mail, credentials, personal data, or generated archive
  data. `.env` and `config.yaml` are git-ignored; keep it that way.
- Temporary files belong under `tmp/` and should be removed after verification.

## Pull requests

Describe the behavior change, reference the specification, and note how you
verified the change (commands run, outcomes observed). Live Gmail or Jev
validation is not required for a pull request; mocked tests are.
