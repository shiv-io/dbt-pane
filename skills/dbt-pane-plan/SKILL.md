---
name: dbt-pane-plan
description: Write a dbt change plan to .dbt-pane/plan.yml for the user to review in dbt-pane before any model, macro, seed, snapshot, or source changes. Use when the user asks to plan dbt work, write a dbt plan, or approve a plan. Also offer a plan before a risky dbt change, such as removing or renaming a column, deleting or renaming a model, changing a model's grain, or changing an incremental model's config.
---

# dbt change plans for dbt-pane

The plan is a YAML file that you write before you change the dbt project. The user reads
it in dbt-pane, approves it in the conversation, and later dbt-pane compares the plan
with what you actually changed.

The file is the plan. Your chat message only summarises it. If the chat and the file
disagree, the file is correct, so fix the file.

## When to write a plan

- Write one when the user asks for a plan.
- Do not write one for other dbt work unless the work is risky. Before you make a risky
  change, stop and offer a plan. A change is risky when it:
  - removes or renames a column of a model or snapshot,
  - deletes or renames a model,
  - changes the grain of a model, which is what one row means,
  - changes the config of an incremental model (`materialized`, `unique_key`,
    `incremental_strategy`, `on_schema_change`, `partition_by`, `cluster_by`).
- If the user says no plan is needed, do the work without one.

## Workflow

1. **Find the file.** The project root is the folder that holds `dbt_project.yml`. The
   plan is `<project root>/.dbt-pane/plan.yml`. Run
   `git check-ignore -q .dbt-pane/plan.yml` from the project root. If git does not ignore
   it, tell the user to add `.dbt-pane/` to their global gitignore. Do not edit the
   repo's `.gitignore`.

2. **Deal with an old plan.** If a plan exists, read its `branch`. If `branch` is not the
   current branch (`git branch --show-current`), the plan belongs to earlier work. Run
   `dbt-pane plan archive`. If dbt-pane is too old to have the command, ask the user
   before you move or delete the old file. If `branch` matches, this is a follow-up task. Edit the
   existing plan and do not start a new one.

3. **Read before you write.** Read the models you will change and their YAML. Find the
   downstream models with `grep -r "ref('<model>')"` or from `target/manifest.json`.
   Check that every model and column you name exists, unless the plan creates it.

4. **Write the plan.** Follow [references/plan-format.md](references/plan-format.md).
   [references/example-plan.yml](references/example-plan.yml) shows a full plan.
   - State only what you know. Leave a field out when you are not sure. A missing field
     means "not stated", and dbt-pane does not check it.
   - Write `unchanged` only when you are sure. dbt-pane checks it after the work.
   - Put each decision that the user must make in `open_questions`. Do not decide it
     yourself in the plan.
   - Describe the structure of the data, never the data itself. Do not copy values,
     names, or identifiers from tables into the plan.

5. **Check the plan.** Run `dbt-pane plan check` from the project root. Fix every
   error, then run it again. Report the warnings to the user, because they name the
   downstream models, tests, and metrics that the change affects. If dbt-pane is too old
   to have the command, do the checks in the "Checks" section of the format reference
   yourself.

6. **Open dbt-pane.** The user reviews the plan in dbt-pane, so make sure it is open.
   - If `HERDR_ENV` is `1`, you run inside Herdr. List the panes with
     `herdr pane list --workspace "$HERDR_WORKSPACE_ID"`. A pane in the current tab
     (`HERDR_TAB_ID`) already shows dbt-pane when its `label` is `dbt` or `dbt (follow)`,
     or its `terminal_title` contains `dbt-pane`. If no pane does, open one beside you:

     ```sh
     herdr plugin pane open --plugin shiv-io.dbt-pane --entrypoint panel \
       --placement split --direction right --target-pane "$HERDR_PANE_ID" --no-focus \
       --env DBT_PANE_PROJECT_DIR="<project root>"
     ```

     `DBT_PANE_PROJECT_DIR` makes the pane show your project. Without it, the pane shows
     the project of the pane that has focus, which can be a different worktree. If the
     command fails, for example because the plugin is not installed, tell the user to
     run `dbt-pane` from the project root in another terminal.
   - Outside Herdr, tell the user to run `dbt-pane` from the project root in another
     terminal, if it is not open already.

7. **Ask for approval.** In chat, give a short summary: the title, one line per change,
   the warnings, and the open questions. Tell the user to review the Plan tab in dbt-pane
   (key `p`). Then stop and wait.

8. **Record the approval.** Only after the user explicitly approves the plan in this
   conversation, set `approved_at` to the current time in ISO 8601 with a time zone, for
   example `2026-10-24T14:32:00-04:00`. Never set it for any other reason. If the user
   approves with changes, edit the plan first, then set `approved_at`.

9. **Do the work.** Before the first edit, run `dbt-pane plan status`. Stop if it does not
   print `approved`. Keep to the plan.

10. **Change the plan when the work shows it is wrong.** Edit `plan.yml`, delete the
    `approved_at` line, tell the user what changed and why, and stop until they approve
    again. Do not keep working on a plan that you changed after approval.

11. **Report at the end.** Run `dbt-pane plan show`. Say which changes are done, which
    are not, and anything you changed that the plan does not list.
