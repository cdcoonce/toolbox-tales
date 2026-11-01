"""Command line entry point."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from toolbox_tales.env import MissingKeyError, require_key
from toolbox_tales.ledger import Ledger
from toolbox_tales.manifest import SHOT_ALLOWANCE_S, load_and_validate
from toolbox_tales.oxen import OxenClient, OxenError, Prices
from toolbox_tales.spend import Estimate, SpendConfig, estimate, load_config

ClientFactory = Callable[[str], OxenClient]

_NO_PRICES = Prices(None, None, None, None)


def _money(value: float) -> str:
    return f"${value:.2f}"


def _remaining(config: SpendConfig) -> float:
    return config.total_cap_usd - Ledger().total_spent()


def _price(client: OxenClient, args: argparse.Namespace, config: SpendConfig) -> int:
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
    print(f"remaining: {_money(_remaining(config))} of {_money(config.total_cap_usd)}")
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

    print(f"total: {_money(total)} (remaining {_money(_remaining(config))})")
    return 0


def main(argv: Sequence[str] | None = None, client_factory: ClientFactory | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolbox-tales")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="validate an episode manifest")
    val.add_argument("manifest")
    price = sub.add_parser("price", help="show model prices and the remaining budget")
    price.add_argument("models", nargs="+")
    dry = sub.add_parser("dry-run", help="estimate the paid steps of an episode")
    dry.add_argument("manifest")
    args = parser.parse_args(argv)

    if args.command in ("price", "dry-run"):
        try:
            key = require_key("OXEN_API_KEY")
        except MissingKeyError as exc:
            print(exc)
            return 2
        client = (client_factory or (lambda k: OxenClient(k)))(key)
        config = load_config()
        try:
            if args.command == "price":
                return _price(client, args, config)
            return _dry_run(client, args, config)
        except OxenError as exc:
            print(exc)
            return 1

    errors = load_and_validate(args.manifest)
    if errors:
        print("\n".join(errors))
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
