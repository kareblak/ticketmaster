# How a herdr pane learns herdr's active theme and its changes

Research for #185 (map #183, "The board wears herdr's theme").

Investigated 2026-10-28 against the installed herdr **0.9.3** (`/opt/homebrew/bin/herdr`): its live socket API
(`herdr api schema --json`, `herdr api snapshot`), `herdr --default-config`, the pane environment, and the herdr
source at tag [`v0.9.3`](https://github.com/herdrdev/herdr/tree/v0.9.3). All of this was read-only. The user's
config was not changed and the server was not reloaded. Source links below point at `v0.9.3`. Abbreviation:
`H` = `https://github.com/herdrdev/herdr/blob/v0.9.3`.

## Summary

1. **Herdr does not expose its UI theme to panes.** No API method, snapshot field, subscription event, pane
   environment variable or plugin context carries the theme name or palette. The only way to learn it is to read
   `config.toml` and resolve it the same way herdr does.
2. **A pane does get the host terminal's colours and light/dark appearance**, through standard terminal escape
   sequences: OSC 10/11/4 queries and CSI `?996n` / mode 2031. Mode 2031 **pushes** appearance changes to the
   pane. These are the outer terminal's colours, not herdr's theme palette.
3. **Nothing pushes theme changes.** `config.toml` is not watched, and a config reload emits no event, so a
   pane has to poll the file (mtime). The single exception is appearance under `auto_switch = true`: the mode 2031
   push arrives at the same moment herdr switches between `dark_name` and `light_name`.
4. **`auto_switch` is the only case where the effective theme differs from the `name` in `config.toml`.** When
   it is true, the effective theme is `dark_name` or `light_name` (with defaults derived from `name`), chosen by
   the foreground client's host appearance, which can change at runtime.

## Sources, one by one

### 1. `config.toml`: the source of truth (poll)

- **Path.** `$HERDR_CONFIG_PATH` if set. Otherwise `$XDG_CONFIG_HOME/herdr/config.toml`, and otherwise
  `$HOME/.config/herdr/config.toml` on unix. Debug builds use `herdr-dev`.
  ([H/src/config/io.rs#L22-L35](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/io.rs#L22-L35),
  [#L195-L200](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/io.rs#L195-L200)). Herdr does not export
  `HERDR_CONFIG_PATH` into panes; it appears in the source only as an override that herdr reads. A pane process
  therefore resolves the path itself, and is right as long as it inherits the same `XDG_CONFIG_HOME` and `HOME`
  as the server. The server process reads the file; clients also reload it locally.
- **Keys** (`herdr --default-config`):
  - `[theme] name` (default `catppuccin`)
  - `auto_switch` (default `false`)
  - `dark_name` and `light_name`
  - `[theme.custom]` token overrides, plus `[theme.custom.dark]` and `[theme.custom.light]` when auto-switching
  - The legacy `[ui] accent`, which still applies when `theme.custom.accent` is not set
  ([H/src/app/mod.rs#L245-L271](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/mod.rs#L245-L271)).
  Built-in names are listed in
  [H/src/config/theme.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/theme.rs). The palettes,
  with the tokens `accent`, `panel_bg`, `sidebar_bg`, `active_row_bg`, `selection_bg`, `surface0/1`, `surface_dim`,
  `overlay0/1`, `text`, `subtext0`, `mauve`, `green`, `yellow`, `red` and so on, are hard-coded as `Palette::<name>()` in
  [H/src/app/state.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/state.rs) (for example `nord()` at
  L228). The file holds **only the name**, so a consumer has to ship its own copy of each palette table. The
  `terminal` theme is special: it uses `Reset` colours, which means "whatever the terminal has".
- **Effective-theme resolution**
  ([H/src/app/mod.rs#L301-L331](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/mod.rs#L301-L331)):
  - When `auto_switch` is false, the theme is `name`, with `[theme.custom]` applied.
  - When it is true, the theme is `dark_name` or `light_name`, picked by host appearance. Unknown appearance
    counts as **Dark**. The `[theme.custom]` overrides apply first, then `[theme.custom.dark]` or
    `[theme.custom.light]` on top.
  - `dark_name` and `light_name` default to the sibling pair of `name`
    ([#L218-L243](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/mod.rs#L218-L243)). For example,
    `catppuccin` pairs with `catppuccin-latte` and `gruvbox` with `gruvbox-light`. A theme with no sibling, such
    as `nord`, uses itself for both.
- **When it changes.**
  - The user edits the file and runs `herdr server reload-config` (API `server.reload_config`). The reload
    re-reads `[theme]` and calls `refresh_effective_app_theme`
    ([H/src/app/mod.rs#L949-L951](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/mod.rs#L949-L951)).
    The response is `{type: "config_reload", status, diagnostics}` and goes **only to the caller**.
  - Herdr's in-app Settings → Theme picker **writes** `config.toml`. It sets `[theme] name = "<picked>"` and
    forces `auto_switch = false`
    ([H/src/config/write.rs#L20-L25](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/write.rs#L20-L25)).
    It then reloads the client and sends `server.reload_config`
    ([H/src/client/shell/settings.rs#L162-L182](https://github.com/herdrdev/herdr/blob/v0.9.3/src/client/shell/settings.rs#L162-L182)).
    So a runtime theme change made in the UI always lands on disk, and polling the file's mtime sees it.
  - No file watcher exists. A grep of the source finds no `notify` watcher or FSEvents use. Editing the file
    without a reload changes nothing in herdr, though a file poller would still see the new value before herdr
    does.

### 2. Socket API (`$HERDR_SOCKET_PATH`): carries nothing about the theme

- `herdr api schema --json` (protocol 22) has **zero** matches for `theme`, `appearance` or `palette`. The
  methods and events are workspace, tab, pane, agent, layout, worktree, plugin, notification and server
  lifecycle.
- `herdr api snapshot` returns only `agents, focused_*_id, layouts, panes, protocol, tabs, version,
  workspaces`. None of these is about the theme.
- `events.subscribe` / `events.wait` have **no** config-reload or theme-changed event. The full set of event
  constants in the schema contains none. A reload is therefore invisible to subscribers.

### 3. Pane environment variables: nothing about the theme

Inside a pane: `HERDR_ENV=1`, `HERDR_PANE_ID`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID`, `HERDR_SOCKET_PATH`,
`HERDR_BIN_PATH`, `TERM_PROGRAM=herdr` (observed live). An inventory of every `"HERDR_*"` literal in the source
finds no theme variable. Plugin panes also get `HERDR_PLUGIN_CONTEXT_JSON`, and it holds no theme either
([H/src/app/api/plugins/panes.rs#L244-L262](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/api/plugins/panes.rs#L244-L262)).
Environment variables are fixed when the pane spawns, so they could not carry changes anyway.

### 4. Terminal escape sequences: host colours and appearance (query plus push)

Herdr runs each pane on an embedded terminal emulator (ghostty-vt). It feeds that emulator the **host** terminal's
default fg/bg, its 256-colour palette and its light/dark appearance, all taken from the foreground attached
client ([H/src/app/theme_sync.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/theme_sync.rs),
[H/src/server/headless.rs#L2215-L2232](https://github.com/herdrdev/herdr/blob/v0.9.3/src/server/headless.rs#L2215-L2232)).
A pane process can use these sequences:

- **OSC 10 / 11 / 4 queries** (`ESC]10;?ST`, `ESC]11;?ST`, `ESC]4;n;?ST`) return the host terminal's fg, bg and
  palette ([H/src/terminal_theme.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/terminal_theme.rs)). This
  is the host's colour scheme (for example a Ghostty or iTerm theme), **not** herdr's `[theme]` palette. With
  `theme.name = "terminal"` the two coincide.
- **CSI `?996n`** queries appearance. The reply is `CSI ?997;1n` for dark or `CSI ?997;2n` for light, and there is
  no reply when the appearance is unknown.
- **Mode 2031** (`CSI ?2031h`) subscribes the pane. Herdr then **pushes** `CSI ?997;1n` / `?997;2n` to that
  pane whenever host appearance changes
  ([H/src/terminal_theme.rs#L14-L21](https://github.com/herdrdev/herdr/blob/v0.9.3/src/terminal_theme.rs#L14-L21),
  [#L57-L61](https://github.com/herdrdev/herdr/blob/v0.9.3/src/terminal_theme.rs#L57-L61); test
  `color_scheme_queries_and_live_updates_follow_terminal_mode` in
  [H/src/pane/terminal.rs](https://github.com/herdrdev/herdr/blob/v0.9.3/src/pane/terminal.rs)).

This is the **only push channel**. The appearance change is the very input that flips herdr's theme when
`auto_switch = true`, because both paths start from `set_host_terminal_appearance_state` (a pane notification
and `refresh_effective_app_theme`). A TUI that has enabled mode 2031 can therefore re-resolve
`dark_name`/`light_name` from `config.toml` as soon as it receives `?997`. A host fg/bg/palette change (a new
host theme with the same appearance) updates the replies to OSC queries, but nothing is pushed.

## What `auto_switch` changes at runtime

- When it is **false**, which is the user's current setting (`name = "nord"`, `auto_switch = false`), the effective
  theme is exactly `[theme] name` plus customs. It changes only when the file changes and herdr reloads.
- When it is **true**, the effective theme is `dark_name` or `light_name`, chosen per the **foreground client's**
  host appearance. It can change with no file change: the OS switches light/dark, or a client with a different
  appearance takes the foreground. The server tracks one appearance, the foreground shell client's.
- Picking a theme in herdr's Settings sets `auto_switch` back to false in the file.

## Implication for the board (input to #188)

To wear herdr's theme, a pane process has to:

1. Resolve the config path (`HERDR_CONFIG_PATH`, then XDG, then `~/.config/herdr/config.toml`), parse `[theme]`
   and `[ui] accent`, and map the name to a palette table that it copies from herdr's `state.rs`.
2. Poll the file's mtime to catch changes. Every runtime theme change other than `auto_switch` is written to
   disk.
3. If `auto_switch = true`, also query `CSI ?996n` at start and enable mode 2031 to receive pushed `?997`
   appearance flips, then choose `dark_name` or `light_name`.

An alternative that needs no palette tables: render with terminal default and ANSI colours (`Reset`). This
matches herdr only when the user runs the `terminal` theme.
