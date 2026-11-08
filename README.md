
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

## Voice

Generate missing panel images (paid; refused without `--yes`):

    uv run toolbox-tales panels tests/fixtures/episode.yaml --yes [--panel N]

`--panel N` regenerates only panel N, overwriting it. Cast references or descriptions come from
`[panels]` in `config/spend.toml`.

Speak every line in its character's voice with edge-tts (free; the only network call):

    uv run toolbox-tales voice tests/fixtures/episode.yaml [--redo]

It writes `media/voice/<manifest>/pPP-lLL.mp3`, measures each file with ffprobe and writes `audio`
and `duration` back into the manifest. Unchanged lines (same text and voice) are skipped; `--redo`
forces all. A failed line is named (`voice: panel 1 line 2: ...`), keeps its old duration, and the
command exits 1. `render` then mixes the line audio under a panel only if every line has audio.

## Script

Write an episode manifest from a gotcha with one Claude call (paid, so it needs `--yes`):

    uv run toolbox-tales script --gotcha gotcha.txt --tool detector-teeth-check \
        --cast cast.yaml --out episode.yaml [--tool-doc tool.md] [--yes] [--force]

The model first judges whether the gotcha has a physical wood-shop equivalent. The result is then
validated like any manifest, plus a 105-145 word dialogue check. Exit codes: 0 written, 1 draft
(written as `<out>.draft` with the errors printed) or error, 2 metaphor rejected, 3 model refused
or truncated, 4 spend guard refused. Needs `ANTHROPIC_API_KEY`; the cost is booked from actual
usage in `ledger/spend.csv`.

## Shot

Generate the arrival shot through the Oxen async queue (paid, so it needs `--yes`):

    uv run toolbox-tales shot tests/fixtures/episode.yaml [--yes]

Needs `OXEN_API_KEY` and the panel the shot follows (run `panels` first). The preferred model is
`[models] video`; if three attempts at its price exceed the remaining budget the cheaper
`[shot] fallback_video` is used. The generation id is saved in the manifest, so an interrupted
run resumes polling instead of paying again. Exit codes: 0 done or already there, 1 failed,
cancelled, timed out or error, 2 no key, 4 spend guard refused.
