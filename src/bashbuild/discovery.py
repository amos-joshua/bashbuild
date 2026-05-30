"""Scan a build-workspace directory into a component/target/script model.

The workspace is convention-driven: the filesystem layout *is* the build graph.
All scripts run with cwd = workspace root, so paths are kept relative to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Display ordering for the scripts inside a single source/build folder.
_ORDER = {"run.sh": 0, "clone.sh": 0, "check.sh": 90, "delete.sh": 99}


def _script_sort_key(name: str) -> tuple[int, str]:
    return (_ORDER.get(name, 50), name)


def _scripts_in(folder: Path) -> dict[str, "Script"]:
    if not folder.is_dir():
        return {}
    found = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix == ".sh" and not p.name.endswith("~")
    ]
    found.sort(key=lambda p: _script_sort_key(p.name))
    return {p.name: Script(name=p.name, path=p) for p in found}


@dataclass
class Script:
    name: str
    path: Path

    def rel_to(self, root: Path) -> str:
        return str(self.path.relative_to(root))


@dataclass
class Source:
    component: str
    scripts: dict[str, Script]
    src_dir: Path  # where the source lands on disk (source/<component>)


@dataclass
class Target:
    component: str
    platform: str
    arch_config: str  # e.g. "x86_64-debug"
    scripts: dict[str, Script]
    build_dir: Path  # where artifacts land (build/<component>/<triplet>)

    @property
    def triplet(self) -> str:
        return f"{self.platform}/{self.arch_config}"


@dataclass
class Component:
    name: str
    source: Source | None = None
    targets: list[Target] = field(default_factory=list)


@dataclass
class Workspace:
    root: Path
    components: list[Component]


def discover(root: Path) -> Workspace:
    root = root.resolve()
    scripts_root = root / "scripts"
    if not scripts_root.is_dir():
        raise ValueError(f"{root} is not a build-workspace (no scripts/ folder)")

    components: list[Component] = []
    for comp_dir in sorted(p for p in scripts_root.iterdir() if p.is_dir()):
        name = comp_dir.name
        if name == "common":
            continue

        source = None
        src_scripts = _scripts_in(comp_dir / "source")
        if src_scripts:
            source = Source(
                component=name,
                scripts=src_scripts,
                src_dir=root / "source" / name,
            )

        targets: list[Target] = []
        build_root = comp_dir / "build"
        if build_root.is_dir():
            for platform_dir in sorted(p for p in build_root.iterdir() if p.is_dir()):
                for triplet_dir in sorted(
                    p for p in platform_dir.iterdir() if p.is_dir()
                ):
                    scripts = _scripts_in(triplet_dir)
                    if not scripts:
                        continue
                    triplet = f"{platform_dir.name}/{triplet_dir.name}"
                    targets.append(
                        Target(
                            component=name,
                            platform=platform_dir.name,
                            arch_config=triplet_dir.name,
                            scripts=scripts,
                            build_dir=root / "build" / name / triplet,
                        )
                    )

        if source or targets:
            components.append(Component(name=name, source=source, targets=targets))

    return Workspace(root=root, components=components)
