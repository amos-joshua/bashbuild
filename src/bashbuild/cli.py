import argparse
import sys
from pathlib import Path

from .discovery import discover
from .manifest import ManifestError, load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bashbuild",
        description="TUI to drive a shell-script build-workspace.",
    )
    parser.add_argument(
        "workspace",
        type=Path,
        help="path to a build-workspace directory (containing scripts/)",
    )
    args = parser.parse_args()

    root = args.workspace.expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    try:
        workspace = discover(root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not workspace.components:
        print(f"error: no components found under {root}/scripts", file=sys.stderr)
        return 1

    known = {c.name for c in workspace.components}
    try:
        manifest = load_manifest(root, known)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from .app import BashBuildApp

    BashBuildApp(workspace, manifest).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
