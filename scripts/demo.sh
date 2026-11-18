#!/usr/bin/env bash
# Try sebside on a private tmux server with a few demo cards. Your own tmux server and sessions are not touched.
#   scripts/demo.sh        start and attach (Ctrl-b d to detach)
#   scripts/demo.sh stop   kill the demo server and remove its files
set -eu
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POINTER="${TMPDIR:-/tmp}/sebside-demo-$(id -u).path"   # remembers the private directory for `stop`

if [ "${1:-}" = "stop" ]; then
    if [ -f "$POINTER" ] && [ ! -L "$POINTER" ]; then
        DIR="$(cat "$POINTER")"
        case "$DIR" in */sebside-demo.*) tmux -S "$DIR/tmux.sock" kill-server 2>/dev/null || true; rm -rf "$DIR" ;; esac
        rm -f "$POINTER"
    fi
    echo "demo stopped"
    exit 0
fi

DIR="$(mktemp -d "${TMPDIR:-/tmp}/sebside-demo.XXXXXX")"   # private (0700), unpredictable name
printf '%s\n' "$DIR" > "$POINTER"
SOCK="$DIR/tmux.sock"
T=(tmux -S "$SOCK" -f /dev/null)
SEBSIDE="$(command -v sebside || echo "$REPO/.venv/bin/sebside")"
[ -x "$SEBSIDE" ] || { echo "sebside not found: run 'uv sync' first"; exit 1; }
export XDG_STATE_HOME="$DIR/state"

for name in deploy tests agent docs; do
    "${T[@]}" has-session -t "=$name" 2>/dev/null || "${T[@]}" new-session -d -s "$name" -x 160 -y 45 \
        "printf '\n  demo session: $name\n\n'; exec sleep 86400"
done
"${T[@]}" set-environment -g XDG_STATE_HOME "$XDG_STATE_HOME"
pid="$("${T[@]}" display-message -p '#{pid}')"
card() { # card <session> <args...>
    local pane
    pane="$("${T[@]}" display-message -p -t "=$1:" '#{pane_id}')"
    shift
    TMUX="$SOCK,$pid,0" TMUX_PANE="$pane" "$SEBSIDE" card set "$@" >/dev/null
}
card deploy --id demo-deploy --state waiting --title "deploy-api" --desc "Approve the production migration?"
card tests --id demo-tests --state error --title "nightly tests" --desc "3 failed in test_payments.py"
card agent --id demo-agent --state working --title "agent: refactor" --desc "Editing src/session.ts" --extra "12/40"
card docs --id demo-docs --state done --title "docs build" --desc "Built 214 pages"

TMUX="$SOCK,$pid,0" bash "$REPO/sebside.tmux"
"${T[@]}" set-environment -g PATH "$(dirname "$SEBSIDE"):$PATH"
TMUX="$SOCK,$pid,0" bash "$REPO/scripts/toggle.sh" "$("${T[@]}" display-message -p -t '=deploy:' '#{pane_id}')"
echo "attaching to the demo server (Ctrl-b d to detach, scripts/demo.sh stop to clean up)"
exec "${T[@]}" attach -t "=deploy"
