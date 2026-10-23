# dbt pane

A Herdr plugin that opens a side panel for the dbt project in your current worktree.
The panel refreshes by itself and has four tabs:

| Tab | Shows |
|---|---|
| Models | Models and other dbt resources changed on this worktree compared with the base branch, including uncommitted and untracked files. Each model shows its materialization and the relation it builds. |
| Target | The database, schema, target, and adapter from `dbt debug`, the profiles file that dbt used, and where each changed model builds. |
| Docs | Description changes for models, columns, sources, macros, and doc blocks, as word-level diffs. It also flags new columns that have no description. |
| Lineage | Upstream and downstream trees for each changed model, seed, snapshot, or source. Changed nodes are yellow. Tests and metrics are summarised. |

It works with dbt v2 and with dbt-core 1.x.

## Getting started

### 1. Install the requirements

- [Herdr](https://herdr.dev) 0.9.0 or later
- [uv](https://docs.astral.sh/uv/)
- git
- dbt v2 or dbt-core 1.x, with a working `profiles.yml` for your project

### 2. Install the plugin

From GitHub:

```sh
herdr plugin install shiv-io/herdr-dbt
```

Or from a local clone:

```sh
git clone git@github.com:shiv-io/herdr-dbt.git
herdr plugin link "$PWD/herdr-dbt"
```

The first time the panel opens, `uv` creates the plugin's Python environment, so the
first open is slower. Check that the plugin is there:

```sh
herdr plugin list
```

### 3. Open the panel

Focus a pane whose folder is inside your dbt project or worktree, then run:

```sh
herdr plugin action invoke shiv-io.herdr-dbt.open
```

The panel opens as a split to the right of the focused pane. The panel finds the dbt
project from the focused pane's folder, so run the command from the worktree you want
to watch. To open it in its own tab instead:

```sh
herdr plugin action invoke shiv-io.herdr-dbt.open-tab
```

The first load takes a few seconds, because the panel runs `dbt parse`, plus
`dbt deps` if the worktree has no packages yet, and `dbt debug`.

### 4. Bind a key (optional)

Add this to `~/.config/herdr/config.toml`, then reload with `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+alt+d"
type = "shell"
command = "herdr plugin action invoke shiv-io.herdr-dbt.open"
```

### If you closed the panel

Run the open command again from a pane in the dbt project:

```sh
herdr plugin action invoke shiv-io.herdr-dbt.open
```

Or press your key binding. Closing the panel loses nothing: it keeps its parsed manifest
in the Herdr plugin state folder, so it opens again quickly.

If the panel opens and closes straight away, or shows an error and asks you to press
Enter, read the message in the pane. The most common causes are that `uv` or `dbt`
is not installed, or that there is no `dbt_project.yml` in or near the focused pane's
folder.

## Use

Keys inside the panel:

| Key | Action |
|---|---|
| `1` to `4` | Switch tab |
| `r` | Refresh now |
| `d` | Run `dbt debug` again |
| `[` and `]` | Show less or more lineage depth |
| `q` | Quit |

Open one panel for each worktree you are working in. Each panel stays on the worktree
it was opened from.

You can also run it without Herdr:

```sh
uv run dbt-pane --project-dir path/to/dbt/project
uv run dbt-pane --once --project-dir path/to/dbt/project   # print one report and exit
```

## How it works

- **Project.** The panel finds the dbt project from the folder of the pane that was
  focused when you opened it. It searches that folder, its parents, and up to three
  levels below it for `dbt_project.yml`.
- **Worktrees.** Every git command runs inside that worktree. The panel therefore shows
  that worktree's branch, commits, and uncommitted files. Open one panel per worktree.
- **Base branch.** The first ref that exists among `origin/main`, `main`, `origin/master`,
  and `master`. The remote branch is checked first, because a stale local `main` gives an
  older merge base, and the diff then includes other people's merged work. Changes are
  measured from the merge base to the working tree.
- **Manifest.** When a project file changes, the panel runs `dbt parse` into its own
  folder under the Herdr plugin state directory, so it never writes to your `target/`.
  If `dbt_packages/` is missing, as in a new worktree, it runs `dbt deps` once first.
- **dbt debug.** Runs when the panel starts, when `dbt_project.yml` or `profiles.yml`
  changes, and when you press `d`. Values that look like secrets are masked.
- **Refresh.** Every 2 seconds the panel checks git status and file modification times.
  It does more work only when something changed.

### dbt executable

The panel uses the first match from this list:

1. `DBT_PANE_DBT`
2. `.venv/bin/dbt` in the dbt project
3. `.venv/bin/dbt` at the repository root
4. `dbt` on `PATH`

Herdr starts plugin commands with the server's environment, which often lacks the
`PATH` from your shell profile. `scripts/launch.sh` adds your login shell's `PATH` before
it starts the panel.

### Settings

| Variable | Effect |
|---|---|
| `DBT_PANE_PROJECT_DIR` | Use this dbt project instead of searching. |
| `DBT_PANE_BASE` | Compare against this ref instead of main or master. |
| `DBT_PANE_DBT` | Path to the dbt executable. |

## Develop

```sh
uv sync
uv run pytest
```

To test against [jaffle-shop](https://github.com/dbt-labs/jaffle-shop) on DuckDB:

```sh
git clone https://github.com/dbt-labs/jaffle-shop.git
cd jaffle-shop
cat > profiles.yml <<'YAML'
default:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: jaffle_shop.duckdb
      database: jaffle_shop
      schema: dev
      threads: 4
YAML
dbt deps
dbt build --vars '{load_source_data: true}'
git worktree add -b my-change ../jaffle-shop.worktrees/my-change main
cp profiles.yml ../jaffle-shop.worktrees/my-change/
```

`profiles.yml` is gitignored in jaffle-shop, so each new worktree needs its own copy.
If you keep it in `~/.dbt/` instead, all worktrees share it.

jaffle-shop requires dbt v2. To test with dbt-core, change `require-dbt-version` in
`dbt_project.yml` to `">=1.10.0"`.
