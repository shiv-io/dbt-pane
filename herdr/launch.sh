#!/bin/sh
# Herdr runs plugin commands with the server's environment, which often lacks the
# PATH from the user's shell profile. Borrow the login shell's PATH so uv and dbt resolve.
set -eu

login_path=$("${SHELL:-/bin/sh}" -lc 'printf %s "$PATH"' 2>/dev/null || true)
PATH="${login_path:+$login_path:}$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
export PATH

plugin_root=${HERDR_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
herdr_bin=${HERDR_BIN_PATH:-herdr}

bundled_version() {
  sed -n 's/^version = "\(.*\)"/\1/p' "$plugin_root/pyproject.toml" | head -n 1
}

# An installed dbt-pane wins when it is at least as new as the copy in this plugin,
# so the plugin never runs a panel older than the flags it passes.
installed_dbt_pane() {
  command -v dbt-pane >/dev/null 2>&1 || return 1
  installed=$(dbt-pane --version 2>/dev/null | awk '{print $2}')
  [ -n "$installed" ] || return 1
  required=$(bundled_version)
  oldest=$(printf '%s\n%s\n' "$required" "$installed" | sort -V | head -n 1)
  [ "$oldest" = "$required" ]
}

run_panel() {
  status=0
  if installed_dbt_pane; then
    dbt-pane "$@" || status=$?
  else
    uv run --frozen --no-dev --quiet --project "$plugin_root" dbt-pane "$@" || status=$?
  fi
  if [ "$status" -ne 0 ]; then
    printf '\ndbt-pane exited with status %s. Press Enter to close this pane.\n' "$status"
    read -r _
  fi
  exit "$status"
}

open_pane() {
  entrypoint=$1
  shift
  exec "$herdr_bin" plugin pane open --plugin "$HERDR_PLUGIN_ID" --entrypoint "$entrypoint" "$@"
}

case "${1:-panel}" in
  build)
    exec uv sync --frozen --no-dev --project "$plugin_root"
    ;;
  panel)
    run_panel
    ;;
  panel-follow)
    run_panel --follow
    ;;
  open-split)
    open_pane panel --placement split --direction right --no-focus
    ;;
  open-tab)
    open_pane panel --placement tab --focus
    ;;
  open-follow)
    open_pane panel-follow --placement split --direction right --no-focus
    ;;
  *)
    echo "usage: launch.sh [build|panel|panel-follow|open-split|open-tab|open-follow]" >&2
    exit 2
    ;;
esac
