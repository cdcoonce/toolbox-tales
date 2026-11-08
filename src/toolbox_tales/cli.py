"""Command line entry point."""

import argparse
import shutil
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

import anthropic
import yaml

from toolbox_tales import panels as panels_mod
from toolbox_tales import render as render_mod
from toolbox_tales import script as script_mod
from toolbox_tales import shot as shot_mod
from toolbox_tales import voice as voice_mod
from toolbox_tales.env import MissingKeyError, require_key
from toolbox_tales.ledger import Ledger, LedgerError
from toolbox_tales.manifest import (
    RUNTIME_MAX_S,
    RUNTIME_MIN_S,
    SHOT_ALLOWANCE_S,
    load_and_validate,
)
from toolbox_tales.oxen import OxenClient, OxenError, Prices
from toolbox_tales.spend import Estimate, SpendConfig, estimate, load_config

ClientFactory = Callable[[str], OxenClient]

_NO_PRICES = Prices(None, None, None, None)


def _money(value: float) -> str:
    return f"${value:.2f}"


def _remaining(config: SpendConfig) -> float:
    return config.total_cap_usd - Ledger().total_spent()


def _price(client: OxenClient, args: argparse.Namespace, config: SpendConfig) -> int:
    remaining = _remaining(config)
    for model in args.models:
        prices = client.get_prices(model)
        print(model)
        lines = [
            f"  {name}: {getattr(prices, name)}"
            for name in (
                "cost_per_image",
                "cost_per_second",
                "input_cost_per_token",
                "output_cost_per_token",
            )
            if getattr(prices, name) is not None
        ]
        print("\n".join(lines) if lines else "  unpriced")
    print(f"remaining: {_money(remaining)} of {_money(config.total_cap_usd)}")
    return 0


def _missing_images(manifest_path: Path) -> int:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    panels = data.get("panels") if isinstance(data, dict) else None
    count = 0
    for panel in panels if isinstance(panels, list) else []:
        image = panel.get("image") if isinstance(panel, dict) else None
        if not isinstance(image, str) or not (manifest_path.parent / image).exists():
            count += 1
    return count


def _shot_exists(manifest_path: Path) -> bool:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    shot = data.get("arrival_shot") if isinstance(data, dict) else None
    video = shot.get("video") if isinstance(shot, dict) else None
    return isinstance(video, str) and (manifest_path.parent / video).exists()


def _lookup(client: OxenClient, model: str) -> Prices:
    return client.get_prices(model) if model else _NO_PRICES


def _step(label: str, model: str, est: Estimate) -> tuple[str, float]:
    if est.cost_usd is None:
        return f"{label} = unpriced", 0.0
    return f"{label} = {_money(est.cost_usd)}", est.cost_usd


def _dry_run(client: OxenClient, args: argparse.Namespace, config: SpendConfig) -> int:
    errors = load_and_validate(args.manifest)
    if errors:
        print("\n".join(errors))
        return 1
    remaining = _remaining(config)
    path = Path(args.manifest)
    total = 0.0

    image = config.image_model
    n = _missing_images(path)
    est = estimate(_lookup(client, image), image, "image", n)
    line, cost = _step(f"panels: {n} x {image or '(unset)'}", image, est)
    print(line)
    total += cost

    video = config.video_model
    seconds = 0.0 if _shot_exists(path) else SHOT_ALLOWANCE_S
    est = estimate(_lookup(client, video), video, "video", seconds)
    line, cost = _step(f"shot: {seconds} s x {video or '(unset)'}", video, est)
    print(line)
    total += cost

    in_rate, out_rate = config.script_input_usd_per_mtok, config.script_output_usd_per_mtok
    script_prices = Prices(
        None,
        None,
        in_rate / 1e6 if in_rate > 0 else None,
        out_rate / 1e6 if out_rate > 0 else None,
    )
    model = config.script_model
    est = estimate(
        script_prices if model else _NO_PRICES,
        model,
        "text",
        config.est_input_tokens,
        config.est_output_tokens,
    )
    line, cost = _step(f"script: {model or '(unset)'}", model, est)
    print(line)
    total += cost

    print(f"total: {_money(total)} (remaining {_money(remaining)})")
    return 0


def _render(manifest: str) -> int:
    errors = load_and_validate(manifest)
    if errors:
        print("\n".join(errors))
        return 1
    path = Path(manifest)
    try:
        plan = render_mod.build_plan(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent)
    except ValueError as exc:
        print(exc)
        return 1
    if not plan.output.resolve().is_relative_to(path.parent.resolve()):
        print("render: output path escapes the manifest directory")
        return 1
    runtime = render_mod.plan_runtime(plan)
    if not RUNTIME_MIN_S <= runtime <= RUNTIME_MAX_S:
        print(f"runtime: {runtime:.2f} s outside {RUNTIME_MIN_S:g}-{RUNTIME_MAX_S:g} s")
        return 1
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found: install it (brew install ffmpeg)")
        return 3
    try:
        render_mod.render(plan, ffmpeg)
    except render_mod.RenderError as exc:
        print(exc)
        return 1
    print(plan.output)
    return 0


def _voice(manifest: str, redo: bool) -> int:
    try:
        result = voice_mod.voice_episode(Path(manifest), redo=redo)
    except voice_mod.VoiceError as exc:
        print(f"voice: {exc}")
        return 1
    print(f"voiced {result.voiced}, skipped {result.skipped}")
    for failure in result.failures:
        print(f"voice: {failure}")
    return 1 if result.failures else 0


def _script(args: argparse.Namespace) -> int:
    try:
        config = load_config()
    except (TypeError, ValueError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    try:
        result = script_mod.write_script(
            Path(args.gotcha),
            args.tool,
            Path(args.cast),
            Path(args.out),
            confirmed=args.yes,
            tool_doc_path=Path(args.tool_doc) if args.tool_doc else None,
            ledger=Ledger(),
            config=config,
            force=args.force,
        )
    except script_mod.ScriptError as exc:
        print(exc)
        return 1
    except LedgerError as exc:
        print(f"ledger: {exc}")
        return 1
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        print(f"script: API error: {type(exc).__name__}: {exc}")
        return 1
    status = result.status
    if status == "written":
        print(f"written {result.path} (cost ${result.cost_usd or 0:.4f})")
        return 0
    if status == "draft":
        if result.path is not None:
            print(f"draft {result.path}")
        print("\n".join(result.messages))
        return 1
    if status == "rejected":
        print(f"rejected: {result.messages[0]} - {result.messages[1]}")
        return 2
    if status == "refused":
        print("\n".join(result.messages))
        return 3
    print(f"spend: {result.messages[0]}")
    return 4


def _panels(args: argparse.Namespace, client_factory: ClientFactory | None) -> int:
    try:
        key = require_key("OXEN_API_KEY")
    except MissingKeyError as exc:
        print(exc)
        return 2
    client = (client_factory or (lambda k: OxenClient(k)))(key)
    try:
        config = load_config()
    except (TypeError, ValueError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    try:
        result = panels_mod.generate_panels(
            Path(args.manifest),
            client,
            confirmed=args.yes,
            panel=args.panel,
            ledger=Ledger(),
            config=config,
        )
    except panels_mod.PanelsError as exc:
        print(exc)
        return 1
    except OxenError as exc:
        print(exc)
        return 1
    except LedgerError as exc:
        print(f"ledger: {exc}")
        return 1
    base = Path(args.manifest).parent
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8"))
    for n in result.generated:
        print(f"generated panel {n} -> {base / manifest['panels'][n - 1]['image']}")
    print(f"generated {len(result.generated)}, skipped {result.skipped}")
    for failure in result.failures:
        print(f"panels: {failure}")
    if result.refused is not None:
        print(f"spend: {result.refused}")
        return 4
    return 1 if result.failures else 0


def _shot(args: argparse.Namespace, client_factory: ClientFactory | None) -> int:
    try:
        key = require_key("OXEN_API_KEY")
    except MissingKeyError as exc:
        print(exc)
        return 2
    client = (client_factory or (lambda k: OxenClient(k)))(key)
    try:
        config = load_config()
    except (TypeError, ValueError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"config: {exc}")
        return 1
    try:
        result = shot_mod.make_shot(
            Path(args.manifest), client, confirmed=args.yes, ledger=Ledger(), config=config
        )
    except shot_mod.ShotError as exc:
        print(exc)
        return 1
    except OxenError as exc:
        print(exc)
        return 1
    except LedgerError as exc:
        print(f"ledger: {exc}")
        return 1
    status = result.status
    if status == "exists":
        print(f"exists {result.message}")
        return 0
    if status == "succeeded":
        print(f"succeeded {result.message} ({result.duration_s} s, {result.model})")
        return 0
    if status == "spend-refused":
        print(f"spend: {result.message}")
        return 4
    print(f"{status}: {result.message}")
    return 1


def main(argv: Sequence[str] | None = None, client_factory: ClientFactory | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolbox-tales")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="validate an episode manifest")
    val.add_argument("manifest")
    price = sub.add_parser("price", help="show model prices and the remaining budget")
    price.add_argument("models", nargs="+")
    dry = sub.add_parser("dry-run", help="estimate the paid steps of an episode")
    dry.add_argument("manifest")
    rend = sub.add_parser("render", help="render a placeholder episode to MP4")
    rend.add_argument("manifest")
    voi = sub.add_parser("voice", help="synthesize line audio with edge-tts")
    voi.add_argument("manifest")
    voi.add_argument("--redo", action="store_true", help="re-synthesize every line")
    scr = sub.add_parser("script", help="write an episode manifest from a gotcha with Claude")
    scr.add_argument("--gotcha", required=True, help="file holding the gotcha text")
    scr.add_argument("--tool", required=True, help="tool name; a cast member must play it")
    scr.add_argument("--cast", required=True, help="cast sheet YAML")
    scr.add_argument("--out", required=True, help="episode manifest to write")
    scr.add_argument("--tool-doc", help="optional file describing the tool")
    scr.add_argument("--yes", action="store_true", help="confirm the paid call")
    scr.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    pan = sub.add_parser("panels", help="generate missing panel images with Oxen")
    pan.add_argument("manifest")
    pan.add_argument("--panel", type=int, help="regenerate only this 1-based panel")
    pan.add_argument("--yes", action="store_true", help="confirm the paid calls")
    sho = sub.add_parser("shot", help="generate the arrival shot with Oxen")
    sho.add_argument("manifest")
    sho.add_argument("--yes", action="store_true", help="confirm the paid call")
    args = parser.parse_args(argv)

    if args.command == "shot":
        return _shot(args, client_factory)

    if args.command == "panels":
        return _panels(args, client_factory)

    if args.command == "script":
        return _script(args)

    if args.command == "voice":
        return _voice(args.manifest, args.redo)

    if args.command == "render":
        return _render(args.manifest)

    if args.command in ("price", "dry-run"):
        try:
            key = require_key("OXEN_API_KEY")
        except MissingKeyError as exc:
            print(exc)
            return 2
        client = (client_factory or (lambda k: OxenClient(k)))(key)
        try:
            config = load_config()
        except (TypeError, ValueError, tomllib.TOMLDecodeError, OSError) as exc:
            print(f"config: {exc}")
            return 1
        try:
            if args.command == "price":
                return _price(client, args, config)
            return _dry_run(client, args, config)
        except OxenError as exc:
            print(exc)
            return 1
        except LedgerError as exc:
            print(f"ledger: {exc}")
            return 1

    errors = load_and_validate(args.manifest)
    if errors:
        print("\n".join(errors))
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
