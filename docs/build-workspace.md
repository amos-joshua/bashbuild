# The build-workspace convention

bashbuild drives a **build-workspace**: a directory whose layout *is* the build
graph. There is no central build engine — every step is a small, standalone
shell script with a uniform contract, discovered from the filesystem. The same
convention works for any project (it originated with gitkebab; jemdrive uses an
identical, thinner skeleton).

## Folder structure

```
build-workspace/
├── build-workspace.yaml      # the build plan (see README → Manifest)
├── scripts/                  # all build scripts, by component
│   ├── common/               # shared helpers sourced by scripts (not a component)
│   └── <component>/          # e.g. zlib-1.2.12 — name includes the version
│       ├── source/           # obtain/verify/remove the source
│       │   ├── clone.sh
│       │   ├── check.sh
│       │   └── delete.sh
│       └── build/            # build/verify/remove, per platform + arch + config
│           └── <platform>/<arch>-<config>/    # e.g. linux/x86_64-debug
│               ├── run.sh
│               ├── run-direct*.sh
│               ├── check.sh
│               └── delete.sh
├── source/                   # where clone.sh deposits source code
│   └── <component>/
└── build/                    # where run.sh installs finished artifacts
    ├── <component>/<platform>/<arch>-<config>/
    └── tmp/                  # scratch build trees (out-of-source builds)
```

Key terms:

- **Component** — a versioned library or app, named `<name>-<version>` (e.g.
  `openssl-1.1.1n`). The version is part of the directory name, so multiple
  versions can coexist; the manifest decides which are in the plan.
- **Target triplet** — `<platform>/<arch>-<config>`, e.g. `linux/x86_64-debug`
  or `android/arm64-release`. A component has one build script-set per triplet.

## The two invariants

1. **Working directory is the workspace root.** Every script is invoked with
   `cwd` set to the top of the workspace and addresses everything by relative
   path (sourcing `scripts/common/...`, reading `source/...`, writing
   `build/...`). bashbuild always runs `bash <relative/script.sh>` from the
   root — honour this if you run scripts by hand.

2. **Exit code is truth.** `0` = success / present; non-zero = failure /
   absent. This is what lets `check.sh` act as a pure state probe and what
   bashbuild's `●`/`○` badges reflect.

## Source scripts — `scripts/<component>/source/`

Operate on `source/<component>/`. Idempotent and runnable by hand.

| Script | Expected to… | Exit code |
|--------|--------------|-----------|
| `clone.sh` | Make the source appear under `source/<component>/` — `git clone`, or download + unpack a tarball (often via `source/tmp/`), or copy from a sibling checkout. Safe to re-run (remove + refetch). | `0` on success |
| `check.sh` | Report whether the source is present and usable, by testing a **sentinel** (e.g. `source/<component>/.git` or a key file). No side effects. | `0` = present, non-zero = absent |
| `delete.sh` | Remove `source/<component>/` (and any `source/tmp/<component>` scratch). Safe to run when already absent. | `0` |

## Build scripts — `scripts/<component>/build/<platform>/<arch>-<config>/`

Operate on `build/<component>/<triplet>/` (final artifacts) and
`build/tmp/<component>/<triplet>/` (scratch). Assume the source is already
cloned and that dependency components are already built (the manifest encodes
that order).

| Script | Expected to… | Exit code |
|--------|--------------|-----------|
| `run.sh` | **Set up the build environment, then invoke `run-direct*.sh` inside it.** This is the entry point — e.g. start a Docker toolchain container, export an NDK/SDK cross-compile environment, then call the `run-direct` script(s). Keep environment setup here, not in `run-direct`. | `0` on success |
| `run-direct*.sh` | **Do the actual build**, assuming the environment `run.sh` established. Typically: create the tmp build dir, configure (cmake/configure) pointing at dependencies under `build/`, compile, and install into `build/<component>/<triplet>/`. When a build has ordered stages, split them as `run-direct-1_*.sh`, `run-direct-2_*.sh`, … and call them in order from `run.sh`. Meant to be invoked *by* `run.sh`, not directly (it presumes the env). | `0` on success |
| `check.sh` | Report whether the build is installed, by testing a sentinel artifact (e.g. `build/<component>/<triplet>/lib/libz.a`). No side effects. | `0` = built, non-zero = not |
| `delete.sh` | Remove `build/<component>/<triplet>/` and the matching `build/tmp/...` scratch. Safe when already absent. | `0` |

### Why `run.sh` vs `run-direct.sh`?

The split separates **where you build** from **what you build**. `run.sh` owns
the environment (container, cross-compile toolchain, exported variables);
`run-direct.sh` owns the build commands and runs *inside* that environment. This
keeps the build logic identical whether it executes on the host, in Docker, or
under an SDK, and lets multi-stage builds be expressed as numbered
`run-direct-N_*.sh` steps driven from a single `run.sh`.

## How bashbuild uses this

- Discovers components and target triplets by globbing `scripts/`.
- Runs `clone.sh` / `run.sh` / `check.sh` / `delete.sh` on demand, streaming
  their output, always from the workspace root.
- Treats each `check.sh` exit code as the source of truth for state badges and
  component roll-ups.
- Reads `build-workspace.yaml` to order components into phases and to know which
  versions are in the plan.

bashbuild never assumes a specific component set — point it at any workspace
that follows this convention.
