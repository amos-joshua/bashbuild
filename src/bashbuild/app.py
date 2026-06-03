from __future__ import annotations

import asyncio
import codecs
import os
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    RichLog,
    SelectionList,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from .discovery import Component, Script, Workspace, discover
from .manifest import Manifest

# Node state
UNKNOWN, PRESENT, ABSENT, RUNNING, OK, FAILED, PARTIAL = (
    "unknown",
    "present",
    "absent",
    "running",
    "ok",
    "failed",
    "partial",
)

# ● present/built (check.sh passed) · ✓ a script ran successfully ·
# ◐ some targets built · ○ absent · ⟳ running · ✗ failed
_BADGE = {
    UNKNOWN: "[dim]○[/]",
    PRESENT: "[green]●[/]",
    OK: "[green]✓[/]",
    ABSENT: "[grey50]○[/]",
    RUNNING: "[yellow]⟳[/]",
    FAILED: "[red]✗[/]",
    PARTIAL: "[yellow]◐[/]",
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
    state_key: tuple[str, str] | None = None  # source/target: (component, "source"|triplet)


# Keep per-script captured output bounded, mirroring the live pane's max_lines.
_HISTORY_CAP = 20000


@dataclass
class ScriptRun:
    """The captured output of one script execution, replayable into the pane."""

    lines: list = field(default_factory=list)  # renderables in emit order
    line_count: int = 0
    rc: int | None = None  # None while still running
    elapsed: float = 0.0


def _count_lines(renderable) -> int:
    text = renderable.plain if isinstance(renderable, Text) else str(renderable)
    return text.count("\n") + 1


def _rollup(target_states: list[str], source_state: str | None) -> str:
    """Summarise a component from its source/target children.

    Built-ness is judged by check.sh (PRESENT), not by a script merely running.
    """
    if RUNNING in target_states or source_state == RUNNING:
        return RUNNING
    if FAILED in target_states:
        return FAILED
    if not target_states:
        if source_state in (PRESENT, ABSENT):
            return source_state
        return UNKNOWN
    built = target_states.count(PRESENT)
    if built == len(target_states):
        return PRESENT
    if built > 0:
        return PARTIAL
    if all(s == UNKNOWN for s in target_states):
        return UNKNOWN
    return ABSENT


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


class FilterScreen(ModalScreen["set[str] | None"]):
    """Multi-select of build targets. Returns the selected set, or None on cancel.

    Source is always shown in the tree and is intentionally not filterable.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+a", "all", "All"),
    ]

    def __init__(self, all_targets: list[str], selected: set[str]) -> None:
        super().__init__()
        self.all_targets = all_targets
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Label("Filter build targets  ·  space toggles, Apply confirms")
            yield SelectionList[str](
                *[(t, t, t in self.selected) for t in self.all_targets],
                id="filter-list",
            )
            with Horizontal(id="filter-buttons"):
                yield Button("Apply", variant="primary", id="apply")
                yield Button("All", id="all")
                yield Button("None", id="none")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        sl = self.query_one(SelectionList)
        if event.button.id == "apply":
            self.dismiss(set(sl.selected))
        elif event.button.id == "all":
            sl.select_all()
        elif event.button.id == "none":
            sl.deselect_all()
        else:
            self.dismiss(None)

    def action_all(self) -> None:
        self.query_one(SelectionList).select_all()

    def action_cancel(self) -> None:
        self.dismiss(None)


class SelectableRichLog(RichLog):
    """A RichLog that participates in Textual's mouse text selection.

    RichLog renders pre-styled Strips and never bakes selection offsets into
    them, so a plain (non-Shift) mouse drag has nothing to anchor to. We overlay
    the selection style and call apply_offsets per visible line — mirroring how
    the Log widget does it — and expose the line text via get_selection.
    """

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        line_index = scroll_y + y
        width = self.scrollable_content_region.width
        if line_index >= len(self.lines):
            return Strip.blank(width, self.rich_style)

        strip = self.lines[line_index]
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(line_index)) is not None:
            start, end = span
            if end == -1:
                end = strip.cell_length
            strip = self._stylize_span(strip, start, end, self._selection_style())

        strip = strip.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        strip = strip.apply_style(self.rich_style)
        return strip.apply_offsets(scroll_x, line_index)

    def _selection_style(self) -> Style:
        """A background-only highlight: composite the theme's translucent
        selection colour to opaque over the pane, leaving text foreground (and
        thus readability) untouched."""
        styles = self.screen.get_component_styles("screen--selection")
        bg = self.background_colors[1] + styles.background
        style = Style(bgcolor=bg.rich_color)
        if styles.color.a:  # honour an explicit opaque selection foreground
            style += Style(color=styles.color.rich_color)
        return style

    @staticmethod
    def _stylize_span(strip: Strip, start: int, end: int, style: Style) -> Strip:
        n = strip.cell_length
        start = max(0, min(start, n))
        end = max(0, min(end, n))
        if end <= start:
            return strip
        left, middle, right = strip.divide([start, end, n])
        # overlay (post_style) so the highlight wins over any cell background
        # while each segment keeps its own foreground colour
        highlighted = Strip(
            list(Segment.apply_style(middle._segments, None, post_style=style)),
            middle.cell_length,
        )
        return Strip.join([left, highlighted, right])

    def get_selection(self, selection):
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"


class BashBuildApp(App):
    CSS = """
    #filterbar { height: 1; padding: 0 1; }
    #main { height: 1fr; }
    #tree { width: 40%; border-right: solid $panel; }
    #right { width: 1fr; }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    #log { height: 1fr; padding: 0 1; background: $surface; }
    #spinner { height: 1; padding: 0 1; background: $surface; color: $text-muted; }
    ConfirmScreen { align: center middle; }
    #confirm-box { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #confirm-buttons { height: auto; align: center middle; padding-top: 1; }
    #confirm-buttons Button { margin: 0 1; }
    FilterScreen { align: center middle; }
    #filter-box { width: 56; height: auto; max-height: 80%; border: thick $accent; background: $surface; padding: 1 2; }
    #filter-list { height: auto; max-height: 18; margin: 1 0; }
    #filter-buttons { height: auto; align: center middle; }
    #filter-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("c", "check", "Check"),
        Binding("d", "delete", "Delete"),
        Binding("f5", "refresh", "Refresh state"),
        Binding("R", "refresh", "Refresh state", show=False),
        Binding("t", "filter_targets", "Filter targets"),
        Binding("k", "kill", "Kill job"),
        Binding("y", "copy_selection", "Copy sel"),
        Binding("w", "save_log", "Save log"),
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
        self._pending: list[str] = []  # streamed log lines awaiting render
        # last-execution output per script, keyed by script path; the pane shows
        # the captured run for whichever script the tree cursor is on.
        self._runs: dict[Path, ScriptRun] = {}
        self._capture: ScriptRun | None = None  # the in-progress run, if any
        self._displayed: Path | None = None  # script whose output the pane shows
        # deduped, sorted aggregate of every target triplet across components
        self._all_targets: list[str] = sorted(
            {t.triplet for c in workspace.components for t in c.targets}
        )
        self._visible_targets: set[str] = set(self._all_targets)  # filter (all on)
        # state persists across tree rebuilds, keyed independent of node identity
        self._states: dict[tuple[str, str], str] = {}
        # bottom-of-log activity line (spinner while a job streams)
        self._spin_active = False
        self._spin_label = ""
        self._spin_start = 0.0
        self._spin_frame = 0
        self._spin_idle = "[dim]idle[/]"

    # ---- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        self.filterbar = Static("", id="filterbar")
        yield self.filterbar
        with Horizontal(id="main"):
            self.nav: Tree = Tree(self.root.name, id="tree")
            yield self.nav
            with Vertical(id="right"):
                self.status = Static("", id="status")
                yield self.status
                self.out = SelectableRichLog(
                    id="log",
                    markup=True,
                    highlight=False,
                    wrap=True,
                    auto_scroll=True,
                    max_lines=20000,
                )
                yield self.out
                self.spinner = Static(self._spin_idle, id="spinner")
                yield self.spinner
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"bashbuild · {self.manifest.name}"
        self.sub_title = str(self.root)
        self.set_interval(0.05, self._drain_pending)
        self.set_interval(0.1, self._tick_spinner)
        self._build_tree()
        self._update_filter_bar()
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
        self.out.write(
            "[dim]legend  [green]●[/] built (check passed)  "
            "[green]✓[/] script ran ok  [yellow]◐[/] partly built  "
            "[grey50]○[/] absent  [yellow]⟳[/] running  [red]✗[/] failed[/]"
        )
        self.out.write("Select a node and press [bold]r[/] run · [bold]c[/] check · [bold]d[/] delete · [bold]R[/] refresh state.")
        self.refresh_states()

    def _build_tree(self) -> None:
        tree = self.nav
        self._state_nodes.clear()
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
                self._recompute_component(cnode)  # roll up over visible targets
            pnode.expand()
        self._relabel_all(tree.root)

    def _add_component(self, cnode: TreeNode, comp: Component) -> None:
        if comp.source:  # source is always shown — never filtered
            s = comp.source
            sinfo = NodeInfo(
                "source",
                "source",
                run_script=s.scripts.get("clone.sh"),
                check_script=s.scripts.get("check.sh"),
                delete_script=s.scripts.get("delete.sh"),
                disk_dir=s.src_dir,
                state_key=(comp.name, "source"),
            )
            sinfo.state = self._states.get(sinfo.state_key, UNKNOWN)
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
            if t.triplet not in self._visible_targets:
                continue
            tinfo = NodeInfo(
                "target",
                t.triplet,
                run_script=t.scripts.get("run.sh"),
                check_script=t.scripts.get("check.sh"),
                delete_script=t.scripts.get("delete.sh"),
                disk_dir=t.build_dir,
                state_key=(comp.name, t.triplet),
            )
            tinfo.state = self._states.get(tinfo.state_key, UNKNOWN)
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
            label = Text()
            badge = _BADGE.get(info.state, "")
            if badge:
                label.append_text(Text.from_markup(badge + " "))
            label.append(info.label, style="bold")
            if info.consumes:
                label.append("  ⬑ " + ", ".join(info.consumes), style="dim")
            return label
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
        if info.state_key is not None:
            self._states[info.state_key] = state  # persist across tree rebuilds
        self._relabel(node)
        if info.kind in ("source", "target"):
            parent = node.parent
            if parent is not None and parent.data is not None and parent.data.kind == "component":
                self._recompute_component(parent)

    def _recompute_component(self, comp_node: TreeNode) -> None:
        target_states: list[str] = []
        source_state: str | None = None
        for child in comp_node.children:
            ci = child.data
            if ci is None:
                continue
            if ci.kind == "target":
                target_states.append(ci.state)
            elif ci.kind == "source":
                source_state = ci.state
        comp_node.data.state = _rollup(target_states, source_state)
        self._relabel(comp_node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        info = event.node.data
        self._update_status(info)
        # While a job streams it owns the pane; otherwise browse the highlighted
        # script's last-run output.
        if not self.busy and info is not None and info.run_script is not None:
            self._display_run(info.run_script)

    def _display_run(self, script: Script) -> None:
        if script.path == self._displayed:
            return
        self._displayed = script.path
        self.out.clear()
        run = self._runs.get(script.path)
        if run is None or not run.lines:
            self.out.write(
                Text.from_markup(
                    f"[dim]no output captured for {script.name} yet — "
                    f"press r to run · c to check · d to delete[/]"
                )
            )
            self.spinner.update(self._spin_idle)
            return
        for renderable in run.lines:
            self.out.write(renderable)
        self.out.scroll_home(animate=False)
        if run.rc is not None:
            mark = "[green]✓[/]" if run.rc == 0 else "[red]✗[/]"
            self.spinner.update(
                Text.from_markup(
                    f"{mark} {script.name} · last run exit {run.rc} "
                    f"· {self._fmt_elapsed(run.elapsed)}"
                )
            )

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
        # Read fixed-size chunks (not readline) so a long line with no newline —
        # e.g. a carriage-return progress bar — can't overrun asyncio's buffer or
        # stall us. Decode incrementally so multibyte chars split across chunk
        # boundaries survive. Batch writes on a short timer so a fast build can't
        # flood the event loop and freeze the UI.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=2**20,
            # Detach from our controlling terminal: a build can spawn tools that
            # would otherwise read our stdin or reconfigure the TTY (mouse mode,
            # tcsetattr) and leave it wedged after exit. New session + no stdin =
            # no access to the terminal at all.
            start_new_session=True,
        )
        self.proc = proc
        assert proc.stdout is not None

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buf = ""
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += decoder.decode(chunk)
            *lines, buf = buf.split("\n")
            for line in lines:
                self._enqueue(line)
            # bound the unflushed buffer for newline-less output (progress bars)
            if len(buf) > 8192:
                self._enqueue(buf)
                buf = ""
            # yield every chunk so the drain timer and input keep running, no
            # matter how fast the process floods its pipe
            await asyncio.sleep(0)

        buf += decoder.decode(b"", final=True)
        if buf:
            self._enqueue(buf)
        await self._flush_pending()  # drain so the caller's exit line stays ordered
        rc = await proc.wait()
        self.proc = None
        return rc

    def _enqueue(self, line: str) -> None:
        # collapse carriage-return redraws to their final visible state
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        # cap absurd line lengths so one giant line can't stall wrapping
        if len(line) > 4000:
            line = line[:4000] + f" …(+{len(line) - 4000} chars)"
        self._pending.append(line)
        # backpressure: if rendering can't keep up, drop oldest (display is
        # capped by max_lines anyway) so the buffer can't grow without bound
        if len(self._pending) > 60000:
            del self._pending[:20000]
            self._pending.insert(0, "… [dropped 20000 lines to keep the UI responsive] …")

    def _emit(self, renderable) -> None:
        """Write to the live pane and, during a job, capture it for replay."""
        self.out.write(renderable)
        cap = self._capture
        if cap is None:
            return
        cap.lines.append(renderable)
        cap.line_count += _count_lines(renderable)
        while cap.line_count > _HISTORY_CAP and len(cap.lines) > 1:
            cap.line_count -= _count_lines(cap.lines.pop(0))

    def _drain_pending(self, limit: int = 300) -> None:
        """Render a bounded slice of buffered output — runs on a timer so the
        per-frame cost stays fixed regardless of how fast output arrives."""
        if not self._pending:
            return
        take = self._pending[:limit]
        del self._pending[:limit]
        self._emit(Text.from_ansi("\n".join(take)))

    async def _flush_pending(self) -> None:
        while self._pending:
            self._drain_pending()
            await asyncio.sleep(0)

    async def _probe(self, script: Script) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            script.rel_to(self.root),
            cwd=str(self.root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
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
        rc = -1
        # start a fresh capture for this script and show it live (replacing any
        # previously displayed run); it becomes the script's saved last-run.
        run = self._runs[script.path] = ScriptRun()
        self._capture = run
        self._displayed = script.path
        self.out.clear()
        self._begin_spinner(f"{info.label} · {script.name}")
        try:
            self._set_state(node, info, RUNNING)
            rel = script.rel_to(self.root)
            self._emit(f"[bold cyan]$ bash {rel}[/]")
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
            self._emit(
                "[green]✓ exit 0[/]" if rc == 0 else f"[red]✗ exit {rc}[/]"
            )
        finally:
            run.rc = rc
            run.elapsed = monotonic() - self._spin_start
            self._capture = None
            self.busy = False
            self._end_spinner(rc)

    # ---- activity spinner (bottom line of the log pane) -------------------

    _SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 60}:{s % 60:02d}"

    def _begin_spinner(self, label: str) -> None:
        self._spin_active = True
        self._spin_label = label
        self._spin_start = monotonic()
        self._spin_frame = 0

    def _end_spinner(self, rc: int) -> None:
        self._spin_active = False
        elapsed = self._fmt_elapsed(monotonic() - self._spin_start)
        mark = "[green]✓[/]" if rc == 0 else "[red]✗[/]"
        self.spinner.update(
            Text.from_markup(f"{mark} {self._spin_label} · done in {elapsed}")
        )

    def _tick_spinner(self) -> None:
        if not self._spin_active:
            return
        self._spin_frame += 1
        frame = self._SPIN[self._spin_frame % len(self._SPIN)]
        elapsed = self._fmt_elapsed(monotonic() - self._spin_start)
        self.spinner.update(
            Text.from_markup(
                f"[yellow]{frame}[/] {self._spin_label} · running  {elapsed}"
            )
        )

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
        if info.kind in ("source", "target", "script"):
            # always check (and roll up) the owning source/target, even when the
            # cursor is on a script leaf inside it
            owner = self._owning_state_node(node)
            if owner is None or owner[1].check_script is None:
                self.notify("No check script here", severity="warning")
                return
            onode, oinfo = owner
            self.run_worker(
                self._run_job(onode, oinfo, oinfo.check_script, "check"),
                exclusive=False,
            )
        else:  # component or phase: check everything beneath it
            items = [
                (n, i)
                for n, i in self._descendant_state_nodes(node)
                if i.check_script is not None
            ]
            if not items:
                self.notify("Nothing to check here", severity="warning")
                return
            self.run_worker(self._check_group(node, items), exclusive=False)

    def _owning_state_node(self, node: TreeNode) -> tuple[TreeNode, NodeInfo] | None:
        cur: TreeNode | None = node
        while cur is not None and cur.data is not None:
            if cur.data.kind in ("source", "target"):
                return cur, cur.data
            cur = cur.parent
        return None

    def _descendant_state_nodes(
        self, node: TreeNode
    ) -> list[tuple[TreeNode, NodeInfo]]:
        out: list[tuple[TreeNode, NodeInfo]] = []

        def walk(n: TreeNode) -> None:
            if n.data is not None and n.data.kind in ("source", "target"):
                out.append((n, n.data))
            for child in n.children:
                walk(child)

        walk(node)
        return out

    async def _check_group(
        self, group_node: TreeNode, items: list[tuple[TreeNode, NodeInfo]]
    ) -> None:
        if self.busy:
            self.notify("A job is already running", severity="warning")
            return
        self.busy = True
        # group check isn't a single script's output; show it transiently and
        # let the next navigation re-render a script's saved run.
        self._displayed = None
        self.out.clear()
        try:
            label = group_node.data.label
            self.out.write(
                f"[bold cyan]checking {label} — {len(items)} target(s)…[/]"
            )
            sem = asyncio.Semaphore(8)

            async def one(n: TreeNode, i: NodeInfo) -> None:
                async with sem:
                    ok = await self._probe(i.check_script)
                self._set_state(n, i, PRESENT if ok else ABSENT)

            await asyncio.gather(*(one(n, i) for n, i in items))
            built = sum(1 for _, i in items if i.state == PRESENT)
            self.out.write(f"[green]✓[/] {label}: {built}/{len(items)} present")
        finally:
            self.busy = False

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
        proc = self.proc
        if proc is None or proc.returncode is not None:
            self.notify("Nothing is running")
            return
        self._emit("[yellow]⚠ terminating job…[/]")
        self.run_worker(self._terminate(proc), exclusive=False)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        # The job runs in its own session (start_new_session=True), so signal the
        # whole process group — not just bash — or its children keep running and
        # hold the stdout pipe open, leaving _stream blocked on read.
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(30):  # give it ~3s to exit cleanly, then SIGKILL
            if proc.returncode is not None:
                return
            await asyncio.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def action_clear_log(self) -> None:
        self.out.clear()

    # ---- target filter ----------------------------------------------------

    def action_filter_targets(self) -> None:
        if not self._all_targets:
            self.notify("No build targets to filter")
            return

        def applied(result: "set[str] | None") -> None:
            if result is not None:
                self._apply_filter(result)

        self.push_screen(
            FilterScreen(self._all_targets, set(self._visible_targets)), applied
        )

    def _apply_filter(self, selected: set[str]) -> None:
        self._visible_targets = {t for t in self._all_targets if t in selected}
        keep = self._current_component_name()
        self.nav.clear()
        self._build_tree()
        self._update_filter_bar()
        if keep is not None:
            # defer until the rebuilt nodes have been laid out (lines assigned)
            self.call_after_refresh(self._move_cursor_to_component, keep)
        self.notify(
            f"Showing {len(self._visible_targets)}/{len(self._all_targets)} targets"
        )

    def _update_filter_bar(self) -> None:
        total = len(self._all_targets)
        sel = [t for t in self._all_targets if t in self._visible_targets]
        if len(sel) == total:
            self.filterbar.update(
                Text.from_markup(
                    f"[dim]Targets: all {total}  ·  press [b]t[/] to filter[/]"
                )
            )
        else:
            shown = ", ".join(sel) if sel else "none — sources only"
            self.filterbar.update(
                Text.from_markup(
                    f"[black on yellow] FILTER [/] [yellow]{len(sel)}/{total} targets: "
                    f"{shown}  ·  press [b]t[/] to edit[/]"
                )
            )

    def _current_component_name(self) -> str | None:
        node = self.nav.cursor_node
        while node is not None and node.data is not None:
            if node.data.kind == "component":
                return node.data.label
            node = node.parent
        return None

    def _move_cursor_to_component(self, name: str) -> None:
        def walk(n: TreeNode):
            yield n
            for c in n.children:
                yield from walk(c)

        for n in walk(self.nav.root):
            if n.data is not None and n.data.kind == "component" and n.data.label == name:
                self.nav.move_cursor(n)
                return

    def action_copy_selection(self) -> None:
        # Textual captures the mouse, so native terminal selection is blocked —
        # drag over the log to make a Textual selection, then copy it here.
        text = self.screen.get_selected_text()
        if not text:
            self.notify(
                "Drag over the log to select text first (or Shift+drag for native selection)",
                severity="warning",
            )
            return
        self.copy_to_clipboard(text)
        self.notify(f"Copied {len(text)} chars to clipboard")

    def action_save_log(self) -> None:
        text = "\n".join(strip.text for strip in self.out.lines)
        path = Path(tempfile.gettempdir()) / f"bashbuild-{self.manifest.name}.log"
        path.write_text(text)
        self.out.write(f"[bold cyan]saved log[/] → {path}")
        self.notify(f"Saved log → {path}")

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
