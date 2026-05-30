from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, RichLog, Static, Tree
from textual.widgets.tree import TreeNode

from .discovery import Component, Script, Workspace, discover
from .manifest import Manifest

# Node state
UNKNOWN, PRESENT, ABSENT, RUNNING, OK, FAILED = (
    "unknown",
    "present",
    "absent",
    "running",
    "ok",
    "failed",
)

_BADGE = {
    UNKNOWN: "[dim]○[/]",
    PRESENT: "[green]●[/]",
    OK: "[green]●[/]",
    ABSENT: "[grey50]○[/]",
    RUNNING: "[yellow]⟳[/]",
    FAILED: "[red]✗[/]",
}


@dataclass
class NodeInfo:
    kind: str  # phase | component | source | target | script
    label: str
    state: str = UNKNOWN
    run_script: Script | None = None
    check_script: Script | None = None
    delete_script: Script | None = None
    disk_dir: Path | None = None
    consumes: list[str] | None = None  # component nodes: direct deps from manifest
    phase: int | None = None  # component nodes: 0-based phase index


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class BashBuildApp(App):
    CSS = """
    #main { height: 1fr; }
    #tree { width: 40%; border-right: solid $panel; }
    #right { width: 1fr; }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    #log { height: 1fr; padding: 0 1; background: $surface; }
    ConfirmScreen { align: center middle; }
    #confirm-box { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #confirm-buttons { height: auto; align: center middle; padding-top: 1; }
    #confirm-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("c", "check", "Check"),
        Binding("d", "delete", "Delete"),
        Binding("f5", "refresh", "Refresh state"),
        Binding("R", "refresh", "Refresh state", show=False),
        Binding("k", "kill", "Kill job"),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, workspace: Workspace, manifest: Manifest) -> None:
        super().__init__()
        self.workspace = workspace
        self.manifest = manifest
        self.root = workspace.root
        self.busy = False
        self.proc: asyncio.subprocess.Process | None = None
        self._state_nodes: list[tuple[TreeNode, NodeInfo]] = []

    # ---- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            self.nav: Tree = Tree(self.root.name, id="tree")
            yield self.nav
            with Vertical(id="right"):
                self.status = Static("", id="status")
                yield self.status
                self.out = RichLog(
                    id="log", markup=True, highlight=False, wrap=True, auto_scroll=True
                )
                yield self.out
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"bashbuild · {self.manifest.name}"
        self.sub_title = str(self.root)
        self._build_tree()
        self.out.write(f"[bold cyan]workspace[/] {self.root}")
        self.out.write(
            f"[bold cyan]manifest[/] {self.manifest.path.name} "
            f"· {len(self.manifest.components)} components · {len(self.manifest.phases)} phases"
        )
        extras = sorted(
            c.name for c in self.workspace.components if c.name not in self.manifest.deps
        )
        if extras:
            self.out.write(
                f"[dim]not in build plan ({len(extras)}): {', '.join(extras)}[/]"
            )
        self.out.write("Select a node and press [bold]r[/] run · [bold]c[/] check · [bold]d[/] delete · [bold]R[/] refresh state.")
        self.refresh_states()

    def _build_tree(self) -> None:
        tree = self.nav
        tree.root.expand()
        by_name = {c.name: c for c in self.workspace.components}
        for i, level in enumerate(self.manifest.phases):
            pnode = tree.root.add(
                f"Phase {i + 1}", data=NodeInfo("phase", f"Phase {i + 1}")
            )
            for name in level:
                comp = by_name[name]
                cinfo = NodeInfo(
                    "component",
                    comp.name,
                    consumes=self.manifest.consumes(comp.name),
                    phase=i,
                )
                cnode = pnode.add(comp.name, data=cinfo)
                self._add_component(cnode, comp)
                cnode.expand()
            pnode.expand()
        self._relabel_all(tree.root)

    def _add_component(self, cnode: TreeNode, comp: Component) -> None:
        if comp.source:
            s = comp.source
            sinfo = NodeInfo(
                "source",
                "source",
                run_script=s.scripts.get("clone.sh"),
                check_script=s.scripts.get("check.sh"),
                delete_script=s.scripts.get("delete.sh"),
                disk_dir=s.src_dir,
            )
            snode = cnode.add("source", data=sinfo)
            self._state_nodes.append((snode, sinfo))
            for name, sc in s.scripts.items():
                snode.add_leaf(
                    name,
                    data=NodeInfo(
                        "script",
                        name,
                        run_script=sc,
                        check_script=sinfo.check_script,
                        delete_script=sinfo.delete_script,
                        disk_dir=s.src_dir,
                    ),
                )
        for t in comp.targets:
            tinfo = NodeInfo(
                "target",
                t.triplet,
                run_script=t.scripts.get("run.sh"),
                check_script=t.scripts.get("check.sh"),
                delete_script=t.scripts.get("delete.sh"),
                disk_dir=t.build_dir,
            )
            tnode = cnode.add(t.triplet, data=tinfo)
            self._state_nodes.append((tnode, tinfo))
            for name, sc in t.scripts.items():
                tnode.add_leaf(
                    name,
                    data=NodeInfo(
                        "script",
                        name,
                        run_script=sc,
                        check_script=tinfo.check_script,
                        delete_script=tinfo.delete_script,
                        disk_dir=t.build_dir,
                    ),
                )

    # ---- labels & status --------------------------------------------------

    def _label(self, info: NodeInfo) -> Text:
        if info.kind == "phase":
            return Text(info.label, style="bold magenta")
        if info.kind == "component":
            t = Text(info.label, style="bold")
            if info.consumes:
                t.append("  ⬑ " + ", ".join(info.consumes), style="dim")
            return t
        badge = _BADGE.get(info.state, "")
        if info.kind == "script":
            return Text.from_markup(f"{badge} [dim]{info.label}[/]")
        return Text.from_markup(f"{badge} {info.label}")

    def _relabel(self, node: TreeNode) -> None:
        if node.data is not None:
            node.set_label(self._label(node.data))

    def _relabel_all(self, node: TreeNode) -> None:
        self._relabel(node)
        for child in node.children:
            self._relabel_all(child)

    def _set_state(self, node: TreeNode, info: NodeInfo, state: str) -> None:
        info.state = state
        self._relabel(node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._update_status(event.node.data)

    def _update_status(self, info: NodeInfo | None) -> None:
        if info is None:
            self.status.update("")
            return
        if info.kind == "component":
            phase = "" if info.phase is None else f"phase {info.phase + 1}  ·  "
            consumes = (
                "consumes: " + ", ".join(info.consumes)
                if info.consumes
                else "no dependencies"
            )
            self.status.update(Text.from_markup(f"[bold]{info.label}[/]  ·  {phase}{consumes}"))
            return
        if info.disk_dir is None:
            self.status.update("")
            return
        d = info.disk_dir
        if d.is_dir():
            try:
                n = sum(1 for _ in d.iterdir())
            except OSError:
                n = 0
            self.status.update(Text.from_markup(f"[green]●[/] {d}  ({n} entries)"))
        else:
            self.status.update(Text.from_markup(f"[grey50]○[/] {d}  (not on disk)"))

    # ---- subprocess plumbing ---------------------------------------------

    async def _stream(self, cmd: list[str]) -> int:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.proc = proc
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            self.out.write(Text(line.decode("utf-8", "replace").rstrip("\n")))
        rc = await proc.wait()
        self.proc = None
        return rc

    async def _probe(self, script: Script) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            script.rel_to(self.root),
            cwd=str(self.root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0

    async def _run_job(
        self,
        node: TreeNode,
        info: NodeInfo,
        script: Script,
        interpret: str,  # run | check | delete
        refresh_check: Script | None = None,
    ) -> None:
        if self.busy:
            self.notify("A job is already running", severity="warning")
            return
        self.busy = True
        try:
            self._set_state(node, info, RUNNING)
            rel = script.rel_to(self.root)
            self.out.write(f"\n[bold cyan]$ bash {rel}[/]")
            rc = await self._stream(["bash", rel])
            if interpret == "check":
                self._set_state(node, info, PRESENT if rc == 0 else ABSENT)
            elif interpret == "delete":
                self._set_state(node, info, ABSENT if rc == 0 else FAILED)
            else:  # run
                if rc == 0 and refresh_check is not None:
                    ok = await self._probe(refresh_check)
                    self._set_state(node, info, PRESENT if ok else OK)
                else:
                    self._set_state(node, info, OK if rc == 0 else FAILED)
            self.out.write(
                "[green]✓ exit 0[/]" if rc == 0 else f"[red]✗ exit {rc}[/]"
            )
        finally:
            self.busy = False

    # ---- actions ----------------------------------------------------------

    def _current(self) -> tuple[TreeNode, NodeInfo] | None:
        node = self.nav.cursor_node
        if node is None or node.data is None:
            return None
        return node, node.data

    def action_run(self) -> None:
        cur = self._current()
        if cur is None:
            return
        node, info = cur
        if info.run_script is None:
            self.notify("Nothing to run here", severity="warning")
            return
        refresh = info.check_script if info.kind in ("source", "target") else None
        self.run_worker(
            self._run_job(node, info, info.run_script, "run", refresh),
            exclusive=False,
        )

    def action_check(self) -> None:
        cur = self._current()
        if cur is None:
            return
        node, info = cur
        if info.check_script is None:
            self.notify("No check script here", severity="warning")
            return
        self.run_worker(
            self._run_job(node, info, info.check_script, "check"), exclusive=False
        )

    def action_delete(self) -> None:
        cur = self._current()
        if cur is None:
            return
        node, info = cur
        if info.delete_script is None:
            self.notify("No delete script here", severity="warning")
            return
        self.run_worker(self._delete_flow(node, info), exclusive=False)

    async def _delete_flow(self, node: TreeNode, info: NodeInfo) -> None:
        if self.busy:
            self.notify("A job is already running", severity="warning")
            return
        ok = await self.push_screen_wait(
            ConfirmScreen(f"Delete '{info.label}'?  Runs {info.delete_script.name}")
        )
        if ok:
            await self._run_job(node, info, info.delete_script, "delete")

    def action_kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            self.out.write("[yellow]⚠ job terminated[/]")
        else:
            self.notify("Nothing is running")

    def action_clear_log(self) -> None:
        self.out.clear()

    def action_refresh(self) -> None:
        self.refresh_states()

    def refresh_states(self) -> None:
        self.run_worker(self._refresh_states(), exclusive=True, group="probe")

    async def _refresh_states(self) -> None:
        sem = asyncio.Semaphore(8)

        async def one(node: TreeNode, info: NodeInfo) -> None:
            if info.check_script is None:
                return
            async with sem:
                ok = await self._probe(info.check_script)
            self._set_state(node, info, PRESENT if ok else ABSENT)

        await asyncio.gather(*(one(n, i) for n, i in self._state_nodes))
