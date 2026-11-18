# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - unreleased

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
