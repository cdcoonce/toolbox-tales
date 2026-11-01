
## Usage

Validate an episode manifest (and the cast sheet it points to):

    uv run toolbox-tales validate tests/fixtures/episode.yaml

It prints `valid` and exits 0, or prints one error per line and exits 1.
Each error starts with its rule code, for example `panel-count:` or `runtime:`.

## Spend guard

Keys come from a gitignored `.env` (or the environment): `OXEN_API_KEY`, `ANTHROPIC_API_KEY`.
Caps and model ids live in `config/spend.toml`; paid calls are recorded in `ledger/spend.csv`.

Show a model's prices and the remaining budget (a free lookup):

    uv run toolbox-tales price flux-2-dev

Estimate the paid steps of an episode without calling anything paid:

    uv run toolbox-tales dry-run tests/fixtures/episode.yaml

Both exit 2 if `OXEN_API_KEY` is missing, and 1 on an Oxen error. `dry-run` also exits 1 on an
invalid manifest. A model with no applicable price prints `unpriced` and is never treated as free.

## Render

Render a placeholder episode (solid-colour panels and shot, overlays burned in, silent audio):

    uv run toolbox-tales render tests/fixtures/episode.yaml

It needs ffmpeg and writes the manifest's `render` path. Exit 1 on an invalid manifest, a
runtime outside 45-60 s, or an ffmpeg failure; exit 3 if ffmpeg is missing.
