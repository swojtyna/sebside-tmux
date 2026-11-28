#!/usr/bin/env bash
# Toggle the sebside sidebar in the window of pane $1: close it if the window has one, else open it on the left,
# full height. Called by the key binding that sebside.tmux installs. Only panes marked @sebside_role=sidebar are closed.
set -u

pane="${1:?usage: toggle.sh <pane_id>}"
sebside="$(tmux show-environment -g SEBSIDE_BIN 2>/dev/null || true)"
case "$sebside" in
    SEBSIDE_BIN=*) sebside="${sebside#SEBSIDE_BIN=}" ;;
    *) sebside="" ;;   # unset, or "-SEBSIDE_BIN" (marked for removal)
esac
sebside="${sebside:-$(command -v sebside || true)}"
if [ -z "$sebside" ]; then
    tmux display-message "sebside-tmux: 'sebside' not found. Install: uv tool install sebside-tmux"
    exit 0
fi

window="$(tmux display-message -p -t "$pane" '#{window_id}')"
# Built-ins only (no awk/sed process), so the key answers fast.
existing=""
while read -r id role _; do
    if [ "$role" = "sidebar" ]; then existing="$id"; break; fi
done <<PANES
$(tmux list-panes -t "$window" -F '#{pane_id} #{@sebside_role}')
PANES
if [ -n "$existing" ]; then
    tmux kill-pane -t "$existing"
    exit 0
fi

width="$(tmux show-option -gqv @sebside_shared_width)"   # the width the open sidebars share
width="${width:-$(tmux show-option -gqv @sebside-width)}"
width="${width:-$("$sebside" config get width 2>/dev/null || echo 36)}"
case "$width" in
    ''|*[!0-9%]*|*%*%*|%*) width=36 ;;   # only "36" or "25%" are valid
    *%) ;;
    *[!0-9]*) width=36 ;;
esac
window_width="$(tmux display-message -p -t "$pane" '#{window_width}')"
case "$width" in
    *%) percent="${width%\%}"; [ "$percent" -gt 49 ] 2>/dev/null && percent=49; width="${percent}%" ;;
    *) half=$(( window_width / 2 - 1 )); [ "$width" -gt "$half" ] 2>/dev/null && width="$half"
       [ "$width" -lt 10 ] && width=10 ;;
esac

rest="$sebside" quoted=""
while [[ "$rest" == *"'"* ]]; do
    quoted="$quoted${rest%%"'"*}'\\''"
    rest="${rest#*"'"}"
done
quoted="'$quoted$rest'"
new="$(tmux split-window -hb -f -l "$width" -t "$pane" -P -F '#{pane_id}' "$quoted sidebar")"
[ -n "$new" ] && tmux set-option -p -t "$new" @sebside_role sidebar
