import argparse
import base64
import binascii
import getpass
import os
import sys
from pathlib import Path

from .discovery import discover
from .manifest import ManifestError, Secret, load_manifest, resolve_workspace_root


def prompt_secrets(secrets: list[Secret]) -> None:
    """Prompt (hidden) for declared secrets before the TUI starts, exporting
    any that are provided into the environment so spawned scripts inherit them.

    Runs on the real terminal via getpass — bashbuild detaches script stdin, so
    this is the only point a secret can be entered interactively. Empty input
    skips a secret; a value already present in the environment is left as-is."""
    if not secrets:
        return
    print("Secrets (press Enter to skip — a script that needs one will say so):")
    for s in secrets:
        if os.environ.get(s.env):
            print(f"  {s.env}: already set in the environment — keeping it.")
            continue
        try:
            value = getpass.getpass(f"  {s.prompt} [{s.env}]: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not value:
            continue
        if s.base64:
            try:
                base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                print(f"    not valid base64 — {s.env} left unset.")
                continue
        os.environ[s.env] = value
        print(f"    {s.env} set ({len(value)} chars).")


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
        root = resolve_workspace_root(root)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
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

    prompt_secrets(manifest.secrets)

    from .app import BashBuildApp

    BashBuildApp(workspace, manifest).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
