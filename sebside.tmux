#!/usr/bin/env bash
# sebside-tmux: TPM entry point. Loaded by `set -g @plugin 'swojtyna/sebside-tmux'` and `run '~/.tmux/plugins/tpm/tpm'`.
#
# Options (set them in tmux.conf BEFORE the plugin loads; empty = default; nothing is written back into tmux):
#   @sebside-key    key after the prefix that toggles the sidebar     (default: S, bound only if S is free)
#   @sebside-width  sidebar width in columns or percent, e.g. 36, 25% (default: `sebside config get width`)
#   @sebside-hook   "off" to skip the session-closed nudge hook       (default: on)
#   @sebside-auto   "on" opens the sidebar in every new session and window (default: off)
#   @sebside-focus  "main": Enter on a card leaves the cursor in the main pane, not the sidebar (read by sebside)
set -u

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

option() { # option <name> <default>
    local value
    value="$(tmux show-option -gqv "$1")"
    printf '%s' "${value:-$2}"
}

# tmux >= 3.0 (pane options, hook arrays). Builds that do not print a number (`tmux master`, `tmux next-3.6`,
# `tmux openbsd-7.5`) are treated as new enough.
version_ok() {
    local raw major
    raw="$(tmux -V 2>/dev/null)"
    raw="${raw#tmux }"
    case "$raw" in
        [0-9]*) major="${raw%%.*}"; major="${major%%[!0-9]*}" ;;
        *) return 0 ;;
    esac
    [ -z "$major" ] || [ "$major" -ge 3 ]
}

# Quote a word for sh: 'it'\''s'.
shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# A run-shell command is format-expanded by tmux first: '#' must be doubled.
format_escape() {
    printf '%s' "$1" | sed 's/#/##/g'
}

# Quote a whole string as one tmux double-quoted argument (for a command that tmux parses itself, like a hook).
tmux_quote() {
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\$/\\$/g')"
}

main() {
    if ! version_ok; then
        tmux display-message "sebside-tmux needs tmux 3.0 or newer ($(tmux -V))"
        return 0
    fi
    local sebside
    sebside="$(command -v sebside || true)"
    if [ -z "$sebside" ]; then
        tmux display-message "sebside-tmux: 'sebside' not found on PATH. Install: uv tool install sebside-tmux (or pipx install sebside-tmux)"
        return 0
    fi
    tmux set-environment -g SEBSIDE_BIN "$sebside"

    local key user_key toggle
    user_key="$(tmux show-option -gqv @sebside-key)"
    key="${user_key:-S}"
    toggle="$(format_escape "$(shell_quote "$CURRENT_DIR/scripts/toggle.sh")") #{pane_id}"
    if [ -n "$user_key" ] || ! tmux list-keys -T prefix "$key" >/dev/null 2>&1; then
        tmux bind-key -T prefix "$key" run-shell -b "$toggle"
    fi

    if [ "$(option @sebside-hook on)" != "off" ]; then
        # Fixed index: idempotent on reload and never clobbers the user's own hooks at [0].
        # `#{q:socket_path}` is already quoted for the shell by tmux.
        local reconcile
        reconcile="$(format_escape "$(shell_quote "$sebside")") --socket #{q:socket_path} reconcile"
        tmux set-hook -g 'session-closed[97]' "run-shell -b $(tmux_quote "$reconcile")"
    fi

    # Auto-open: fixed indexes, so re-sourcing is idempotent and the user's own hooks are never touched.
    # tmux expands `#{window_id}` in run-shell; in session-created it is the new session's first window.
    if [ "$(option @sebside-auto off)" = "on" ]; then
        local open
        open="$(format_escape "$(shell_quote "$sebside")") --socket #{q:socket_path} open --if-missing --target #{window_id}"
        tmux set-hook -g 'session-created[96]' "run-shell -b $(tmux_quote "$open")"
        tmux set-hook -g 'after-new-window[98]' "run-shell -b $(tmux_quote "$open")"
    else
        tmux set-hook -gu 'session-created[96]' 2>/dev/null || true
        tmux set-hook -gu 'after-new-window[98]' 2>/dev/null || true
    fi
}

main
