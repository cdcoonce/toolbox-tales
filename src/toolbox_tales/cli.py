"""Command line entry point."""

import argparse
import sys
from collections.abc import Sequence

from toolbox_tales.manifest import load_and_validate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolbox-tales")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="validate an episode manifest")
    val.add_argument("manifest")
    args = parser.parse_args(argv)

    errors = load_and_validate(args.manifest)
    if errors:
        print("\n".join(errors))
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
