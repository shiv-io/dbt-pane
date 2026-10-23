# dbt-pane

A terminal panel that shows what your dbt worktree changes. It refreshes by itself while
you work, and runs in any terminal, in tmux, or as a [Herdr](https://herdr.dev) plugin.

| Tab | Shows |
|---|---|
| Models | Models and other dbt resources changed on this worktree compared with the base branch, including uncommitted and untracked files. Each model shows its materialization and the relation it builds. |
| Target | The database, schema, target, and adapter from `dbt debug`, the profiles file that dbt used, and where each changed model builds. |
| Docs | Description changes for models, columns, sources, macros, and doc blocks, as word-level diffs. It also flags new columns that have no description. |
| Lineage | Upstream and downstream trees for each changed model, seed, snapshot, or source. Each changed model says what kind of change it is: columns, joins, filters, grain, column formulas, inputs, config, or formatting only. Unchanged downstream models show the columns they gain or lose. |
| CLL | Column-level lineage for one changed model: where each column comes from, where it goes, and which columns it uses as join, filter, or grouping keys. New, removed, and changed columns come first. Needs dbt v2. |

It works with dbt v2 and with dbt-core 1.x.

## Getting started

### 1. Install the requirements

- [uv](https://docs.astral.sh/uv/)
- git
- dbt v2 or dbt-core 1.x, with a working `profiles.yml` for your project

### 2. Install dbt-pane

```sh
uv tool install git+ssh://git@github.com/shiv-io/dbt-pane.git
```

This puts a `dbt-pane` command on your `PATH`. Check it:

```sh
dbt-pane --version
```

To upgrade later, run `uv tool upgrade dbt-pane`.

### 3. Run it

Open a second terminal, or a split in your terminal or tmux, and run:

```sh
cd path/to/your/dbt/worktree
dbt-pane
```

The first load takes a few seconds, because dbt-pane runs `dbt parse`, plus `dbt deps`
if the worktree has no packages yet, and `dbt debug`. After that it updates within about
2 seconds of any change.

To watch a project without changing folder:

```sh
dbt-pane --project-dir path/to/your/dbt/worktree
```

To print one report and exit, for example to paste into a pull request:

```sh
dbt-pane --once
```

### 4. Follow the pane you are working in (tmux or Herdr)

```sh
dbt-pane --follow
```

With `--follow`, dbt-pane watches which pane has focus and switches to the dbt project in
that pane's folder. When you move between worktrees, the panel moves with you. When you
focus a pane that is not inside a dbt project, it keeps showing the last project. It keeps
the results for each project it has seen, so switching back is quick.

`--follow` needs tmux or Herdr, because a plain terminal cannot report which pane has focus.

## Keys

| Key | Action |
|---|---|
| `1` to `5` | Switch tab |
| `↑` `↓` and `Enter`, or a mouse click | Open a changed model in the code view |
| `r` | Refresh now |
| `d` | Run `dbt debug` again |
| `a` | Compare with the merge base now, without waiting |
| `[` and `]` | Show less or more lineage depth |
| `q` | Quit |

### Code view

Open a model from the Models tab to see its code. The **Source** tab shows the file as it
is in your worktree now. The **Compiled** tab shows the SQL with Jinja rendered, the same
SQL that `dbt compile` writes. dbt-pane compiles only that model, in the background, as
soon as you open it.

| Key | Action |
|---|---|
| `1` and `2` | Switch between Source and Compiled |
| `c` | Copy the compiled SQL |
| `s` | Copy the source |
| `f` | Copy a `select` of every column, one per line, from the model's full name |
| `n` | Copy only the full name, `database.schema.table`, without quotes |
| `r` | Compile again |
| `Esc` or `q` | Back to the list |

Copying uses `pbcopy`, `wl-copy`, `xclip`, or `xsel`, whichever is installed. Without
one, for example over SSH, it asks the terminal to copy (OSC 52). Most modern terminals
support that, but some turn it off by default.

Compiling needs the same warehouse connection as `dbt compile`, because some macros run
queries while they compile.

`f` takes the column list from the best source it has: dbt v2 static analysis, then the
final select of the compiled SQL, then the columns declared in YAML. The message after the
copy says which source it used. YAML can miss columns, so the message warns you when it
falls back to YAML.

## Change types and column-level lineage

When a worktree changes models, dbt-pane compiles them twice in the background: once at
the merge base and once as the worktree is now. It then compares the two, model by model.

### When the analysis runs

Each compile covers only the changed models, everything upstream of them, and their direct
children, for example `--select "+customers+1 +orders+1"`. Upstream models are included
because static analysis needs their columns. Direct children are included so the Lineage tab
can show which downstream models gain or lose columns.

| Run | When |
|---|---|
| Merge base | Once for each merge base and set of changed models. The result is saved, so restarting the panel reuses it. It runs again after a rebase or merge moves the merge base, or when another model changes. |
| Worktree | When the panel opens, and then after project files have been quiet for 10 seconds. A burst of saves causes one run. |
| Neither | When there are no changed models, or the only edits are to descriptions in YAML or to `.md` doc blocks. Those cannot change the compiled SQL, so the Docs tab updates and the analysis does not run. |

Set how it runs with `--analysis` or the `DBT_PANE_ANALYSIS` variable:

| Mode | Behaviour |
|---|---|
| `auto` | The default. Runs as described above. |
| `manual` | Runs only when you press `a`. The Lineage and CLL tabs say when the result is out of date. Use this for very large projects, or warehouses where metadata queries are slow or costly. |
| `off` | Never runs. The Lineage tab shows only the dependency trees. |

`DBT_PANE_ANALYSIS_QUIET` changes the 10-second wait, in seconds.

The Lineage tab shows when the last comparison ran and how long each compile took, so you
can judge whether `auto` suits a project.

| Label | Meaning | How it is found |
|---|---|---|
| `schema` | Columns added or removed, or a column type changed | Column lists from both compiles |
| `columns via upstream` | The model's SQL did not change, but it gains or loses columns through `select *` | Column lists from both compiles |
| `join` | A join was added or removed, or its type or `on` condition changed | Parsing the compiled SQL |
| `filter` | A `where`, `having`, or `qualify` clause was added, removed, or changed | Parsing the compiled SQL |
| `grain` | A `group by` changed | Parsing the compiled SQL |
| `column logic` | The formula behind an output column changed | Tracing each output column through the SQL |
| `inputs` | The model reads from different models or sources | The two manifests |
| `config` | Materialization, unique key, incremental strategy, or similar changed | The two manifests |
| `formatting only` | Only comments, spacing, or case changed | Parsing the compiled SQL |
| `no SQL change` | The compiled SQL is identical, for example a docs-only change | Parsing the compiled SQL |

With **dbt v2**, both compiles use `--static-analysis strict --generate-info-schema`. That
gives dbt's own column lists, column types, and column-level lineage, and it is what the CLL
tab shows. Static analysis reads the column names and types of your source tables from the
warehouse. That is a metadata lookup and does not read any rows.

With **dbt-core**, dbt-pane uses SQL parsing only. Joins, filters, grain, and formatting are
found the same way. Columns are known only where the final select lists them, so a model
that ends in `select *` shows `columns: unknown`. The CLL tab is not available.

In the CLL tab, a column is **copied** when its value passes through unchanged, and
**derived** when it is computed from other columns. A **key** is a column that decides which
rows exist, through a join, filter, or `group by`, without being part of the value. These
labels come from dbt's static analysis.

The merge base is extracted with `git archive` into the dbt-pane state folder, so no extra
git worktree is registered in your repository. dbt runs from your worktree with
`--project-dir` pointing at that copy, so relative paths in `profiles.yml`, such as a DuckDB
file, still resolve. A project-local `profiles.yml` and the installed `dbt_packages/` are
reused for the merge-base compile.

## Use with Herdr

The repository is also a Herdr plugin. The plugin opens dbt-pane in a Herdr pane next to
the pane you are working in.

### Install the plugin

From GitHub:

```sh
herdr plugin install shiv-io/dbt-pane
```

Or from a local clone:

```sh
git clone git@github.com:shiv-io/dbt-pane.git
herdr plugin link "$PWD/dbt-pane"
```

The plugin runs the `dbt-pane` on your `PATH` if it is at least as new as the plugin.
Otherwise it runs its own copy, which `uv` sets up the first time you open the panel.
You do not need to install dbt-pane separately.

### Open the panel

Focus a pane whose folder is inside your dbt project or worktree, then run:

```sh
herdr plugin action invoke shiv-io.dbt-pane.open
```

The panel opens as a split to the right and stays on the worktree it was opened from.
Open one panel for each worktree you are working in.

| Action | Opens |
|---|---|
| `shiv-io.dbt-pane.open` | A split to the right, fixed on the focused pane's project |
| `shiv-io.dbt-pane.open-tab` | A new tab, fixed on the focused pane's project |
| `shiv-io.dbt-pane.open-follow` | A split to the right that follows focus, as `--follow` does |

### If you closed the panel

Run the open command again from a pane in the dbt project:

```sh
herdr plugin action invoke shiv-io.dbt-pane.open
```

Closing the panel loses nothing. dbt-pane keeps its parsed manifest in the Herdr plugin
state folder, so it opens again quickly.

If the panel shows an error and asks you to press Enter, read the message in the pane.
The most common causes are that `uv` or `dbt` is not installed, or that there is no
`dbt_project.yml` in or near the focused pane's folder.

### Bind a key

Add this to `~/.config/herdr/config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+alt+d"
type = "shell"
command = "herdr plugin action invoke shiv-io.dbt-pane.open"
```

## How it works

- **Project.** dbt-pane looks for `dbt_project.yml` in the starting folder and its
  parents, then up to three levels below it. The starting folder is `--project-dir`, or
  the focused Herdr pane's folder when it runs as a plugin, or the current folder.
  `--follow` searches only upwards, so focusing your home folder does not pick a random
  project below it.
- **Worktrees.** Every git command runs inside the project's worktree, so dbt-pane shows
  that worktree's branch, commits, and uncommitted files.
- **Base branch.** The first ref that exists among `origin/main`, `main`, `origin/master`,
  and `master`. The remote branch is checked first, because a stale local `main` gives an
  older merge base, and the diff then includes other people's merged work. Changes are
  measured from the merge base to the working tree.
- **Manifest.** When a project file changes, dbt-pane runs `dbt parse` into its own
  folder, so it never writes to your `target/`. If `dbt_packages/` is missing, as in a new
  worktree, it runs `dbt deps` once first. The folder is under the Herdr plugin state
  directory when it runs as a plugin, and under `~/.cache/dbt-pane` otherwise.
- **dbt debug.** Runs when the panel starts, when `dbt_project.yml` or `profiles.yml`
  changes, and when you press `d`. Values that look like secrets are masked.
- **Refresh.** Every 2 seconds dbt-pane checks git status and file modification times.
  It does more work only when something changed.

### dbt executable

dbt-pane uses the first match from this list, so a dbt v2 binary on `PATH` never runs a
dbt-core project by accident:

1. `DBT_PANE_DBT`
2. `.venv/bin/dbt` in the dbt project
3. `.venv/bin/dbt` at the repository root
4. `dbt` on the `PATH` that the project's `.envrc` sets, if direnv changes it
5. `.venv/bin/dbt` in the main checkout, for linked worktrees that have no `.venv` of their own
6. `dbt` on `PATH`

The Target tab shows which one it used.

### Environment variables and direnv

Many projects put credentials in environment variables that `profiles.yml` reads with
`env_var()`. When direnv is installed, dbt-pane asks it which variables the project's
`.envrc` sets (`direnv export json`) and passes them to every dbt command. This works in
the Herdr panel too, which otherwise starts without your shell's environment. Each worktree
gets the variables of its own `.envrc`, or of the nearest one in a parent folder.

dbt-pane never approves an `.envrc`. If direnv has not been allowed for a folder, the panel
says so; run `direnv allow` in that folder. After you change `.envrc`, the panel picks up the
new variables within about 15 seconds.

If a worktree's setup script made symlinks, for example `.venv` or `.envrc` pointing at the
main checkout, and that checkout has moved, the Models tab lists the broken links and where
they point.

Herdr starts plugin commands with the server's environment, which often lacks the
`PATH` from your shell profile. `herdr/launch.sh` adds your login shell's `PATH` before
it starts the panel.

### Settings

| Variable | Effect |
|---|---|
| `DBT_PANE_PROJECT_DIR` | Use this dbt project instead of searching. |
| `DBT_PANE_BASE` | Compare against this ref instead of main or master. |
| `DBT_PANE_DBT` | Path to the dbt executable. |
| `DBT_PANE_ANALYSIS` | `auto`, `manual`, or `off`. See [When the analysis runs](#when-the-analysis-runs). |
| `DBT_PANE_ANALYSIS_QUIET` | Seconds without edits before an automatic analysis. Defaults to 10. |

Herdr does not pass your shell's environment to plugins. To set these for the Herdr panel,
put them in a file called `env`, one `KEY=value` per line, in the folder that this command
prints:

```sh
herdr plugin config-dir shiv-io.dbt-pane
```

For example:

```sh
config_dir=$(herdr plugin config-dir shiv-io.dbt-pane)
mkdir -p "$config_dir"
echo "DBT_PANE_ANALYSIS=manual" >> "$config_dir/env"
```

The setting applies to panels opened after the change.

## Repository layout

| Path | Contents |
|---|---|
| `src/dbt_pane/` | The dbt-pane terminal app |
| `tests/` | Tests for the app |
| `herdr-plugin.toml` | The Herdr plugin manifest. Herdr reads it from the repository root. |
| `herdr/` | The Herdr plugin launcher |

## Develop

```sh
uv sync
uv run pytest
uv run dbt-pane --project-dir path/to/dbt/project
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
