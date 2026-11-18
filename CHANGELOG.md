# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Mouse support in the sidebar: a left click selects and activates a card (like Enter), the wheel moves the selection.
  Uses SGR mouse reporting (enabled on start, disabled on exit). Needs tmux `mouse on`. Clicks are ignored in the
  resume confirmation and explain views.
- A bold `●` on the second line of the card whose session contains the sidebar, so you can tell where you are apart
  from where the cursor is. The current card is never dimmed.
- A running sidebar restarts itself (same pane, same arguments) a few seconds after sebside is upgraded, so old
  sidebars no longer stay keyboard-only. Mouse mode is re-sent on every repaint; mouse modifier bits are ignored.

## [0.2.0] - 2026-11-18

### Added

- `sebside open --if-missing [--target WINDOW_ID]`: opens the sidebar pane on the left of a window, full height,
  without taking focus, only when the window has no sidebar yet. Idempotent and quiet; exit 0 when already present.
- tmux option `@sebside-auto on` (default `off`): opens the sidebar automatically in every new session and window,
  through `session-created[96]` and `after-new-window[98]` hooks at fixed indexes (re-sourcing is idempotent and your
  own hooks are never touched). With the option off the two hooks are removed. `sebside uninstall` removes them too.
- tmux option `@sebside-focus main`: after Enter switches to another session, the cursor lands in a main (non-sidebar)
  pane of the target window instead of the sidebar. Unset keeps the 0.1 behaviour.
- The sidebar closes itself when it is the last pane left in its window (every remaining pane is a sidebar and a main
  pane shared the window with it before), so the window and session end normally and tmux's own `detach-on-destroy`
  decides what your client does. On by default; `set -g @sebside-close-alone off` disables it (read at runtime, no
  reload needed). The sidebar only exits its own process: it never kills a pane, and it never closes while a main pane
  is in the window. It notices on its next refresh, so closing takes up to `refresh_seconds` (1.5 s by default).
- Integration tests for the new options on a private tmux server.

### Changed

- The tmux allowlist gains `split-window`, available only to `sebside open` (new pane, one command, no other flags).

## [0.1.0] - 2026-11-18

### Added

- Sidebar TUI (`sebside`): three-line cards with a colored bar, state icons and an animated spinner, cards that
  need you on top, a counter and key hints at the bottom, recently closed cards greyed at the bottom.
- Keyboard navigation: `↑`/`↓`/`j`/`k`, `Enter` to switch to a session, `?` to explain a card's state, `q` to quit.
- Card contract: one JSON file per card in `$XDG_STATE_HOME/sebside/cards/`, plus `@sebside_id` and
  `@sebside_state` pane options; `sebside card set|clear|list`.
- Reconciliation of closed cards by tmux identity (socket, server pid, pane) on every refresh; `sebside reconcile`.
- Resume of closed cards as an argv list in a new detached session, limited to `resume_programs`, after confirmation.
- Config file `~/.config/sebside/config.toml` with environment overrides.
- TPM plugin `sebside.tmux`: `prefix + S` toggles the sidebar, `session-closed[97]` nudge hook; `sebside uninstall`.
- Allowlist of tmux commands, enforced and tested.
