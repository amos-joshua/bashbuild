# bashbuild

A [Textual](https://textual.textualize.io/) TUI to drive `build-workspace`-style
shell-script build trees (see gitkebab / jemdrive). It discovers components,
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

## Manifest (required)

Each workspace must declare its build plan in `build-workspace.yaml` at the
root. Every component lists what it **consumes**; bashbuild topologically sorts
those edges into ordered phases — so the right build order is recorded once and
never has to be remembered again.

```yaml
name: gitkebab
build:
  zlib-1.2.12:    []
  openssl-1.1.1n: []
  libssh2-1.10.0: [zlib-1.2.12, openssl-1.1.1n]
  libgit2-1.4.2:  [zlib-1.2.12, openssl-1.1.1n, libssh2-1.10.0]
  gitkebab-head:  [libgit2-1.4.2]
```

- Component names are the versioned `scripts/<name>` directories, so the
  manifest also pins **which version** of each library is in the plan.
- Components present in `scripts/` but absent from `build:` are not part of the
  plan (alternate versions, platform-only libs); bashbuild lists them dimmed at
  startup and leaves them out of the tree.
- Dangling references, self-deps, and cycles are rejected at load time with a
  clear message.

## Keys

| Key | Action |
|-----|--------|
| `r` | Run the selected node (clone for source, `run.sh` for a target, or the selected script) |
| `c` | Run the node's `check.sh` (updates the ● / ○ state badge) |
| `d` | Run the node's `delete.sh` (with confirmation) |
| `R` / `F5` | Re-probe all states via every `check.sh` |
| `k` | Terminate the running job |
| `ctrl+l` | Clear the log |
| `q` | Quit |

## Layout

- **Left** — `Phase N` → components (each showing `⬑ consumes…`) → `source` +
  build targets → individual scripts. Badges: ● built/present (green),
  ○ absent (grey), ⟳ running, ✗ failed.
- **Top-right** — for a component: its phase and what it consumes; for a
  source/target: the disk state of its `source/` or `build/` folder.
- **Right** — live streamed stdout/stderr of the running script.

State is read from each `check.sh` (exit 0 = present), faithful to the
workspace's "exit code = truth" contract. Build order comes from the manifest's
dependency edges; bashbuild shows the phases but you still drive each step.
