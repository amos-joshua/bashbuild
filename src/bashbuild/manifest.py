"""The build-workspace manifest: the recorded build plan.

A workspace declares, in `build-workspace.yaml` at its root, which components
participate in the build and what each one consumes:

    name: gitkebab
    build:
      zlib-1.2.12:    []
      openssl-1.1.1n: []
      libssh2-1.10.0: [zlib-1.2.12, openssl-1.1.1n]
      ...

bashbuild topologically sorts these edges into ordered phases. The edges double
as the "consumes" information shown in the UI. Cycles and dangling references
are rejected at load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

MANIFEST_NAMES = ("build-workspace.yaml", "build-workspace.yml")

_STUB = """\
name: <workspace-name>
build:
  # component-with-version: [list of components it consumes]
  zlib-1.2.12:    []
  openssl-1.1.1n: []
  libssh2-1.10.0: [zlib-1.2.12, openssl-1.1.1n]
"""


class ManifestError(Exception):
    pass


@dataclass
class Manifest:
    name: str
    path: Path
    deps: dict[str, list[str]]  # component -> direct deps (all within the plan)
    phases: list[list[str]]  # topological levels of component names

    @property
    def components(self) -> list[str]:
        return list(self.deps)

    def consumes(self, component: str) -> list[str]:
        return self.deps.get(component, [])

    def phase_of(self, component: str) -> int | None:
        for i, level in enumerate(self.phases):
            if component in level:
                return i
        return None


def find_manifest(root: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def load_manifest(root: Path, known_components: set[str]) -> Manifest:
    path = find_manifest(root)
    if path is None:
        raise ManifestError(
            f"no manifest found in {root}\n"
            f"bashbuild requires a build-workspace.yaml recording the build plan, e.g.:\n\n{_STUB}"
        )

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: top level must be a mapping")

    build = data.get("build")
    if not isinstance(build, dict) or not build:
        raise ManifestError(f"{path}: needs a non-empty 'build:' mapping of component -> [deps]")

    deps: dict[str, list[str]] = {}
    for comp, raw in build.items():
        comp = str(comp)
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ManifestError(f"{path}: '{comp}' dependencies must be a list, got {type(raw).__name__}")
        deps[comp] = [str(d) for d in raw]

    for comp in deps:
        if comp not in known_components:
            raise ManifestError(f"{path}: '{comp}' is in the manifest but scripts/{comp} does not exist")
    for comp, ds in deps.items():
        for d in ds:
            if d not in deps:
                raise ManifestError(
                    f"{path}: '{comp}' depends on '{d}', which is not declared in build:"
                )
            if d == comp:
                raise ManifestError(f"{path}: '{comp}' depends on itself")

    phases = _topo_phases(deps, path)
    name = str(data.get("name") or root.name)
    return Manifest(name=name, path=path, deps=deps, phases=phases)


def _topo_phases(deps: dict[str, list[str]], path: Path) -> list[list[str]]:
    remaining = {c: set(ds) for c, ds in deps.items()}
    done: set[str] = set()
    phases: list[list[str]] = []
    while remaining:
        level = sorted(c for c, ds in remaining.items() if ds <= done)
        if not level:
            raise ManifestError(
                f"{path}: dependency cycle among: {', '.join(sorted(remaining))}"
            )
        phases.append(level)
        for c in level:
            del remaining[c]
        done.update(level)
    return phases
