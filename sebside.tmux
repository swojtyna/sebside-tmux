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

# Helpers return their result in REPLY: no subshell, and no sed/awk/dirname process, so loading the plugin stays fast.
case "${BASH_SOURCE[0]}" in
    */*) CURRENT_DIR="${BASH_SOURCE[0]%/*}" ;;
    *) CURRENT_DIR=. ;;
esac
CURRENT_DIR="$(cd "${CURRENT_DIR:-/}" && pwd)"

option() { # option <name> <default>
    REPLY="$(tmux show-option -gqv "$1")"
    REPLY="${REPLY:-$2}"
}

# replace_all <text> <from> <to>: every <from> becomes <to> (pattern parts quoted, so they match literally).
replace_all() {
    local rest="$1" out=""
    while [[ "$rest" == *"$2"* ]]; do
        out="$out${rest%%"$2"*}$3"
        rest="${rest#*"$2"}"
    done
    REPLY="$out$rest"
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
    replace_all "$1" "'" "'\\''"
    REPLY="'$REPLY'"
}

# A run-shell command is format-expanded by tmux first: '#' must be doubled.
format_escape() {
    replace_all "$1" "#" "##"
}

# Quote a whole string as one tmux double-quoted argument (for a command that tmux parses itself, like a hook).
tmux_quote() {
    replace_all "$1" "\\" "\\\\"
    replace_all "$REPLY" '"' '\"'
    replace_all "$REPLY" '$' '\$'
    REPLY="\"$REPLY\""
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
    shell_quote "$CURRENT_DIR/scripts/toggle.sh"
    format_escape "$REPLY"
    toggle="$REPLY #{pane_id}"
    if [ -n "$user_key" ] || ! tmux list-keys -T prefix "$key" >/dev/null 2>&1; then
        tmux bind-key -T prefix "$key" run-shell -b "$toggle"
    fi

    option @sebside-hook on
    if [ "$REPLY" != "off" ]; then
        # Fixed index: idempotent on reload and never clobbers the user's own hooks at [0].
        # `#{q:socket_path}` is already quoted for the shell by tmux.
        local reconcile
        shell_quote "$sebside"
        format_escape "$REPLY"
        reconcile="$REPLY --socket #{q:socket_path} reconcile"
        tmux_quote "$reconcile"
        tmux set-hook -g 'session-closed[97]' "run-shell -b $REPLY"
    fi

    # Auto-open: fixed indexes, so re-sourcing is idempotent and the user's own hooks are never touched.
    # tmux expands `#{window_id}` in run-shell; in session-created it is the new session's first window.
    option @sebside-auto off
    if [ "$REPLY" = "on" ]; then
        local open
        shell_quote "$sebside"
        format_escape "$REPLY"
        open="$REPLY --socket #{q:socket_path} open --if-missing --target #{window_id}"
        tmux_quote "$open"
        tmux set-hook -g 'session-created[96]' "run-shell -b $REPLY"
        tmux set-hook -g 'after-new-window[98]' "run-shell -b $REPLY"
    else
        tmux set-hook -gu 'session-created[96]' 2>/dev/null || true
        tmux set-hook -gu 'after-new-window[98]' 2>/dev/null || true
    fi
}

main
