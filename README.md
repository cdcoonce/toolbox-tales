
## Usage

Validate an episode manifest (and the cast sheet it points to):

    uv run toolbox-tales validate tests/fixtures/episode.yaml

It prints `valid` and exits 0, or prints one error per line and exits 1.
Each error starts with its rule code, for example `panel-count:` or `runtime:`.
