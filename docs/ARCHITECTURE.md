# sebside-tmux: architecture conventions (binding for v0.1)

Consolidated 2026-11-18 from three agent reviews (Python structure, tmux plugin conventions, skeptic on state).
Sources: libtmux/tmuxp, tmux-plugins (tmux-sidebar, sensible, continuum, resurrect), tmux-agent-sidebar (hiroppy),
workmux (hooks.rs, ~/.local/state/workmux/agents), opensessions. Where reviews conflicted, decision noted.

## Storage and identity (skeptic; overrides earlier spec)
1. **One JSON file per card**: `$XDG_STATE_HOME/sebside/cards/<card_id>.json` (fallback `~/.local/state/sebside`),
   temp + `os.replace`; `fcntl.flock` on `cards/.lock` only for multi-card ops (pruning). Dir 0700, files 0600,
   owner uid checked, `O_NOFOLLOW`.
2. **The file is the single source of truth for card content.** Pane options carry only `@sebside_id` and optionally
   `@sebside_state` (for status lines). No free text in pane options. (Decision: skeptic over "options win".)
3. **Identity**: card id chosen by the integration. Card stores tmux socket path (from `$TMUX`), server pid, pane id.
   Live = same socket + server pid and pane id present in `list-panes`. Reused `%N` after restart cannot claim a card.
4. **Closed/history by reconciliation**: on every poll and at sidebar start, cards whose pane is not alive get
   `closed_at`. `session-closed` hook is only an optional nudge. Nothing keyed by session name.
5. **Outside tmux** (no `$TMUX`/`$TMUX_PANE`): `card set` writes the file with `pane: null`; never call tmux without `-t`.
6. **Resume**: stored only in the file, as an argv list; launched `tmux new-session -d -- <argv…>` (no shell); only
   programs from a config allowlist; command shown before running; no resume option in tmux.
7. Sanitize title/desc (strip control/escape chars); cap desc ~300 chars; prune closed cards > N or > D days.
8. `"schema": 1` per card; ignore unknown fields; newer schema → read-only, never rewritten; migrations pure on read.
9. Times: epoch seconds UTC; ages clamped ≥ 0; stale uses max(card updated_at, pane_activity).

## tmux plugin conventions
10. Prefix everything: config options `@sebside-<name>` (dash), runtime pane options `@sebside_<name>` (underscore).
11. Read options via one helper with defaults (`show-option -gqv`, empty → default); never write defaults into tmux;
    don't override user-changed values; document "set options before the plugin loads".
12. Key: default `prefix + S` (verified free in tmux 3.8 defaults; `s` is choose-tree), configurable `@sebside-key`;
    bind only if unbound.
13. Side pane: `split-window -hb -f -l <size>` (left, full height, size option, percent allowed, clamp < 50%), mark it
    `@sebside_role=sidebar`, find via `list-panes -F`; toggle state lives in tmux, validated against `list-panes`.
    Per-window sidebar; follow switches via hooks only if needed.
14. Hooks at fixed high indices (e.g. `session-closed[97]`, `after-new-window[98]`) → idempotent, never clobber `[0]`;
    run hook commands with `run-shell -b`; quote executable path, pass `#{pane_id}` as argument.
15. Require tmux ≥ 3.0 (pane options, hook arrays); check `tmux -V` correctly, `display-message` if older.
16. `sebside uninstall`: unset hook indices, unbind key, kill sidebar panes, unset `@sebside_*`. Document in README.
17. Events over polling: `card set` nudges the sidebar (signal via pid in `@sebside_pid` or `tmux wait-for`); slow poll
    (2 s) as fallback; re-read only card files whose mtime changed; `\x1f` field separator in `-F`.

## Python structure
18. src layout, package `sebside`, zero runtime deps (stdlib `tomllib`, `argparse`, raw ANSI). `main(argv=None) -> int`.
19. Module map: `__init__` (version via importlib.metadata, fallback `0+unknown`), `__main__`, `cli` (argparse
    subparsers, each `run(args) -> int`, error→exit code), `errors` (SebsideError tree), `config` (frozen dataclass
    defaults, TOML < env < flags, XDG), `model` (frozen slotted dataclasses Pane/Card, State enum; no IO), `tmux`
    (only module with subprocess; `Tmux` Protocol + real impl; field-list-built formats; socket handling
    `-S`/`SEBSIDE_TMUX_SOCKET`), `store` (card files, lock, schema), `cards` (pure merge/sort/reconcile), `render`
    (pure card/frame → ANSI, width helper incl. wide chars), `tui` (alt screen, hidden cursor, SIGWINCH flag, select
    timeout, whole-frame write with `\x1b[H` + `\x1b[K`, skip unchanged frames), `log` (debug to state-dir file only,
    `-v`/`SEBSIDE_DEBUG=1`), `py.typed`.
20. Exit codes: 0 ok, 1 runtime, 2 usage, 3 tmux missing/not running, 4 state unreadable; messages
    `sebside: <what failed>: <hint>`, no traceback unless debug.
21. Typing strict (mypy --strict on src). ruff lint+format, select E,F,I,UP,B,SIM,RUF,PT,C4,PTH,TCH,T20,S,ANN; S603/S607
    ignored only in `tmux.py`; T20 allowed only in cli/render/tui.
22. Tests: `tests/unit` (pure parse/render/merge, render at several widths incl. wide Unicode), `tests/integration`
    (`@pytest.mark.tmux`, private server `tmux -L <random>` / `-S tmpdir`, `-f /dev/null`, fixed TERM/HOME, skipped
    without tmux); `--strict-markers`. Never touch the user's tmux server.
23. Single version source in pyproject; `sebside --version`; tags `vX.Y.Z`; trusted publishing.

## Notes on the v0.1 implementation

Where the code deliberately differs from, or narrows, the rules above:

- Rule 1: the `cards/.lock` flock is also held for single-card read-modify-write (`card set`, closing, seen marks),
  so the sidebar and `card set` never lose each other's updates. Plain reads take no lock.
- Rule 17: instead of signals or `tmux wait-for`, the sidebar checks the mtime of the `cards/` directory on every
  frame tick (cheap `stat`) and reloads at once when a card file changed; tmux itself is polled every
  `refresh_seconds`. Unchanged card files are not parsed again (mtime and size cache).
- Rule 13: no follow-on-switch hook in v0.1; the sidebar is per window, toggled with the key.
- Module map: `card set` orchestration (tmux pane options, then the file) lives in `cli.py`; `cards.py` stays pure.
- Resume program allowlist: config key `resume_programs` (empty by default, so resume is off until you opt in).
- Rule 2 (groups): `@sebside_group` is the one option sebside reads but never writes. You or an integration set it
  (usually on a session); `list-panes -a -F` resolves it per pane through window, session and global options. Its
  value is an id (`[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}`), never free text, and an invalid value counts as no group. The
  group's text lives in `groups/<id>.json`, not in tmux. Cards keep `schema: 1`; `group` is an optional field.
- Rule 17 (groups): the frame-tick check covers the mtime of `groups/` as well, so a `group set` shows at once.
