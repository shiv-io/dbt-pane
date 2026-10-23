#!/bin/sh
# Herdr runs plugin commands with the server's environment, which often lacks the
# PATH from the user's shell profile. Borrow the login shell's PATH so uv and dbt resolve.
set -eu

login_path=$("${SHELL:-/bin/sh}" -lc 'printf %s "$PATH"' 2>/dev/null || true)
PATH="${login_path:+$login_path:}$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
export PATH

herdr_bin=${HERDR_BIN_PATH:-herdr}

case "${1:-panel}" in
  build)
    exec uv sync --frozen --no-dev --project "${HERDR_PLUGIN_ROOT:-.}"
    ;;
  panel)
    status=0
    uv run --frozen --no-dev --quiet --project "$HERDR_PLUGIN_ROOT" dbt-pane || status=$?
    if [ "$status" -ne 0 ]; then
      printf '\ndbt-pane exited with status %s. Press Enter to close this pane.\n' "$status"
      read -r _
    fi
    exit "$status"
    ;;
  open-split)
    exec "$herdr_bin" plugin pane open --plugin "$HERDR_PLUGIN_ID" --entrypoint panel \
      --placement split --direction right --no-focus
    ;;
  open-tab)
    exec "$herdr_bin" plugin pane open --plugin "$HERDR_PLUGIN_ID" --entrypoint panel \
      --placement tab --focus
    ;;
  *)
    echo "usage: launch.sh [build|panel|open-split|open-tab]" >&2
    exit 2
    ;;
esac
