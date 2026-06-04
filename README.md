# bashbuild

A [Textual](https://textual.textualize.io/) TUI to drive `build-workspace`-style
shell-script build trees (see examples). It discovers components,
their source/build scripts, and target triplets directly from the filesystem —
no configuration.

## Run

```sh
uv run bashbuild ../gitkebab/build-workspace
```

The argument is any directory containing a `scripts/` folder following the
workspace convention:

```
scripts/<component>/source/{clone,check,delete}.sh
scripts/<component>/build/<platform>/<arch>-<config>/{run,run-direct*,check,delete}.sh
```

All scripts run with the working directory set to the workspace root (the
convention every script relies on).

## The build-workspace, in brief

A workspace's layout *is* its build graph — no central engine, just small
standalone shell scripts with a uniform contract, discovered from the
filesystem. A **component** is a versioned library/app (`scripts/zlib-1.2.12/`);
a **target triplet** is `<platform>/<arch>-<config>` (e.g. `linux/x86_64-debug`).

Two invariants hold everything together: every script runs with the **working
directory at the workspace root**, and **exit code is truth** (`0` = success /
present). Each component has two script families:

**Source** (`scripts/<component>/source/`, acting on `source/<component>/`):

- `clone.sh` — make the source appear (git clone, or download + unpack, or copy).
- `check.sh` — is the source present? (tests a sentinel; no side effects)
- `delete.sh` — remove the source.

**Build** (`scripts/<component>/build/<triplet>/`, acting on `build/<component>/<triplet>/`):

- `run.sh` — set up the build **environment** (e.g. Docker/NDK), then invoke `run-direct*.sh` inside it.
- `run-direct*.sh` — do the actual build (configure → compile → install), assuming that environment. Ordered stages are split as `run-direct-1_*.sh`, `run-direct-2_*.sh`, … and called from `run.sh`.
- `check.sh` — is the build installed? (tests a sentinel artifact)
- `delete.sh` — remove the build output.

→ Full reference: [docs/build-workspace.md](docs/build-workspace.md).

## Manifest (required)

Each workspace must declare its build plan in `build-workspace.yaml` at the
root. Every component lists what it **consumes**; bashbuild topologically sorts
those edges into ordered phases — so the right build order is recorded once and
never has to be remembered again.

```yaml
name: libgit2
build:
  zlib-1.2.12:    []
  openssl-1.1.1n: []
  libssh2-1.10.0: [zlib-1.2.12, openssl-1.1.1n]
  libgit2-1.4.2:  [zlib-1.2.12, openssl-1.1.1n, libssh2-1.10.0]
```

- Component names are the versioned `scripts/<name>` directories, so the
  manifest also pins **which version** of each library is in the plan.
- Components present in `scripts/` but absent from `build:` are not part of the
  plan (alternate versions, platform-only libs); bashbuild lists them dimmed at
  startup and leaves them out of the tree.
- Dangling references, self-deps, and cycles are rejected at load time with a
  clear message.

### Secrets (optional)

A workspace may declare secrets its scripts need (API keys, signing
credentials). bashbuild prompts for them **once, hidden, before the TUI
starts** — the only point input is possible, since scripts run with stdin
detached — and injects any provided value into the environment, which every
spawned script inherits. Nothing is written to disk.

```yaml
secrets:
  - PLAY_SERVICE_ACCOUNT_B64                      # bare name: prompt == env var
  - env: PLAY_SERVICE_ACCOUNT_B64                 # or a mapping:
    prompt: Google Play service-account key (base64)
    base64: true                                  # validate it decodes as base64
```

- **Press Enter to skip** any secret; a script that actually needs one fails
  with its own message. Skipping is always allowed.
- A value already set in the environment (e.g. exported beforehand) is kept and
  not re-prompted.
- `base64: true` rejects a value that doesn't decode, so a fat-fingered paste
  fails at the prompt rather than mid-build.

## Keys

| Key | Action |
|-----|--------|
| `r` | Run the selected node (clone for source, `run.sh` for a target, or the selected script) |
| `c` | Check — runs `check.sh` for the owning source/target (from anywhere in its subtree) and rolls the result up to the component. On a component/phase node, checks everything beneath it. |
| `d` | Run the node's `delete.sh` (with confirmation) |
| `R` / `F5` | Re-probe all states via every `check.sh` |
| `t` | Filter visible build targets (multi-select) |
| `k` | Terminate the running job |
| `y` | Copy the selected log text to the clipboard |
| `w` | Save the full log to a file (path is shown) |
| `ctrl+l` | Clear the log |
| `q` | Quit |

## Copying from the log

Textual captures the mouse, so the terminal's native click-drag selection is
blocked. To copy:

- **Drag** over the log to make a selection, then press **`y`** to copy it.
- Or press **`w`** to dump the whole log to a file and open/grep it elsewhere.
- Most terminals also let you **Shift+drag** to bypass the app and use native
  selection.

## Layout

- **Left** — `Phase N` → components (each showing `⬑ consumes…`) → `source` +
  build targets → individual scripts. Badges:
  - **●** built / present — `check.sh` passed (green)
  - **✓** a script ran successfully (green) — ran, but not necessarily verified built
  - **◐** partly built — some of a component's targets pass `check.sh` (yellow)
  - **○** absent (grey) · **⟳** running (yellow) · **✗** failed (red)

  A component badge rolls up its targets: ● all built, ◐ some built, ○ none.
- **Top-right** — for a component: its phase and what it consumes; for a
  source/target: the disk state of its `source/` or `build/` folder.
- **Right** — live streamed stdout/stderr of the running script.

State is read from each `check.sh` (exit 0 = present), faithful to the
workspace's "exit code = truth" contract. Build order comes from the manifest's
dependency edges; bashbuild shows the phases but you still drive each step.

## Filtering targets

Press **`t`** to multi-select which build targets (the deduped set of triplets
across all components, e.g. `linux/x86_64-debug`, `android/arm64-debug`) are
shown. The filter:

- hides unselected target nodes and **recomputes component roll-ups over only
  the visible targets**;
- always keeps **source** visible (it isn't a build target, so it's never
  filtered);
- stays visible in a bar under the header — when a filter is active the bar is
  highlighted (`FILTER 2/7 targets: …`) so it's never a hidden mode.
