# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Groups: a sidebar in a session whose `@sebside_group` is set (by you or an integration, e.g.
  `tmux set-option -t api @sebside_group backend`; sebside never writes it) shows only that group's live,
  default and recently closed cards. Sidebars in sessions without the option show everything, as before. `card set`
  stores the pane's group in the card file (optional field `group`, files without it still read) unless the card
  already has one, or takes `--group ID` (`""` clears it). A card without a group of its own follows its pane's group
  and, once closed, stays in the group its pane last had (optional field `pane_group`). `card list` prints each
  card's `group` and filters with `--group ID`.
- `sebside group set --id ID --title TEXT [--note TEXT]` / `sebside group clear --id ID`: a footer for one group's
  sidebars, above the usage meter: the title word-wrapped over up to `group_lines` lines (new config key, default 3,
  also `SEBSIDE_GROUP_LINES`), the note dimmed below. Stored in `groups/<id>.json` (atomic, 0600; a hashed name for an
  id with upper-case letters, so ids that differ only in case never share a file); a change shows at once.
- `sebside card update --id ID [--title] [--subtitle] [--desc] [--extra]`: changes only the given text of an existing
  card. State, pane, revision, age, seen mark and tmux options stay as they are, and tmux is not called. A missing
  card is a usage error (exit 2).
- One sidebar width everywhere: resizing one sidebar resizes the others (and new ones open at that width),
  kept in the global option `@sebside_shared_width`.
- `sebside meter set --label L --percent N [--note TEXT]` / `sebside meter clear`: one usage meter under the
  counter (`label ████░░ N%` and the note below), e.g. a weekly limit. Hidden once older than `stale_hours`.

- The `x` / Delete key forgets the selected recently closed card (its own
  card file only, after the same ownership checks as resume). Live cards are never removed.
- A click on a recently closed card resumes it at once (still only allowlisted programs, no shell); Enter asks first,
  and in that confirmation any click cancels.
- A resumed session closes when its program exits, also on a server with `remain-on-exit on`, and the viewer returns
  to the session it came from. The sidebar's own pane never stays behind dead.

### Changed

- Live cards never have dimmed text: the state shows only in the bar colour and the icon. Only recently closed cards
  are dimmed.
- No more "showing <title>" message after switching (it went stale); errors are still shown.

### Added

- Mouse support in the sidebar: a left click selects and activates a card (like Enter), the wheel moves the selection.
  Uses SGR mouse reporting (enabled on start, disabled on exit). Needs tmux `mouse on`. Clicks are ignored in the
  resume confirmation and explain views.
- A bold `●` on the second line of the card whose session contains the sidebar, so you can tell where you are apart
  from where the cursor is. 
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
