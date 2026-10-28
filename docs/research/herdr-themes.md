# Herdr's themes and their palettes

Research for #184 (map #183, "The board wears herdr's theme").

Investigated 2026-10-28 against the installed herdr **0.9.3** (`/opt/homebrew/bin/herdr`) and herdr's public source
at tags [`v0.9.3`](https://github.com/herdrdev/herdr/tree/v0.9.3) and `v0.9.5` (current stable). The theme code
(`src/config/theme.rs`, the `Palette` impl in `src/app/state.rs`, `status_color` in `src/client/shell.rs`) is
identical between the two tags.

## Summary

1. **A theme is a fixed set of 19 named color tokens** (`Palette`). Herdr ships **18 built-in themes**, all compiled
   into the binary as Rust constructors. There are no theme files on disk.
2. **No user-defined named themes.** "Custom" means overriding individual tokens on top of a built-in theme under
   `[theme.custom]` (plus `[theme.custom.light]` / `[theme.custom.dark]` when auto-switching).
3. **`auto_switch`** (default `false`) picks between a dark and a light theme name from the **host terminal's**
   light/dark appearance. It reads that from the terminal's color-scheme report (DECRQM-style `CSI ? 997 ; 1|2 n`),
   or, without one, infers it from the terminal background's luminance (OSC 11).
4. **Sidebar agent-state colors are tokens, not fixed values:** working = `yellow`, blocked = `red`, done = `teal`,
   idle = `green`, unknown = `overlay0`.
5. **Herdr does not expose the theme or palette over its socket API** (`herdr api schema --json` has no theme, palette
   or color fields). Another program that wants herdr's colors has to read `~/.config/herdr/config.toml`, resolve
   the name the way herdr does, and carry its own copy of the built-in table below.

## Theme model

`Palette` ([`src/app/state.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/state.rs#L35)) has these
tokens, with herdr's own doc comments:

| token | meaning (from source) |
|---|---|
| `accent` | Primary accent (highlight, active borders) |
| `panel_bg` | Background for the tab bar, floating panels, overlays, and modals |
| `sidebar_bg` | Optional desktop sidebar background. `Reset` keeps the terminal background |
| `active_row_bg` | Background for the active workspace and focused agent rows |
| `selection_bg` | Background for the Navigate-mode cursor row in the sidebar |
| `surface0` | Subtle surface background for selected/focused items |
| `surface1` | Slightly lighter surface for hover/active states |
| `surface_dim` | Very dim surface for separators |
| `overlay0` | Muted text (secondary info, numbers) |
| `overlay1` | Slightly brighter overlay text |
| `text` | Main text color |
| `subtext0` | Subdued text (workspace numbers, dim labels) |
| `mauve` | Branch name / special label color |
| `green` | Done / idle states |
| `yellow` | Working / running states |
| `red` | Needs attention / blocked states |
| `blue` | Unseen / done notification accent |
| `teal` | Notification accent / unseen markers |
| `peach` | Interrupted / warning states |

(The `green` and `blue` comments say "done", but the sidebar code maps done to `teal`; see below.)

## Built-in themes

`THEME_NAMES` in [`src/config/theme.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/theme.rs#L4):
`catppuccin` (the default), `catppuccin-latte`, `terminal`, `tokyo-night`, `tokyo-night-day`, `dracula`, `nord`,
`gruvbox`, `gruvbox-light`, `one-dark`, `one-light`, `solarized`, `solarized-light`, `kanagawa`, `kanagawa-lotus`,
`rose-pine`, `rose-pine-dawn`, `vesper`.

Name matching (`canonical_theme_name`) lowercases the name and turns spaces and underscores into hyphens. It also
accepts aliases: `catppuccin-mocha`; `latte` and `light` for catppuccin-latte; `tokyonight`; `tokyo-day` and
`tokyonight-day`; `gruvbox-dark`; `onedark` and `onelight`; `solarized-dark`; `lotus`; `rosepine`; `rosepine-dawn`
and `dawn`. An unknown name logs a warning, shows a startup diagnostic, and falls back to `catppuccin`
(`catppuccin-latte` for `light_name`).

`herdr --default-config` lists only the 11 dark/base names in its comment. The 7 light variants are valid all the
same.

### Palettes

Generated from the `impl Palette` constructors in
[`src/app/state.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/state.rs#L76-L525) (`Color::Rgb(r, g, b)`
written as hex). `ansi:X` is a ratatui named color that the host terminal's palette resolves. `ansi:Reset` means
the terminal's default color. Every built-in theme leaves `sidebar_bg` at `Reset`; a herdr test pins this.

| theme | `accent` | `panel_bg` | `sidebar_bg` | `active_row_bg` | `selection_bg` | `surface0` | `surface1` | `surface_dim` | `overlay0` | `overlay1` | `text` | `subtext0` | `mauve` | `green` | `yellow` | `red` | `blue` | `teal` | `peach` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `catppuccin` | #89b4fa | #181825 | ansi:Reset | #1e1e2e | #313244 | #313244 | #45475a | #1e1e2e | #6c7086 | #7f849c | #cdd6f4 | #a6adc8 | #cba6f7 | #a6e3a1 | #f9e2af | #f38ba8 | #89b4fa | #94e2d5 | #fab387 |
| `catppuccin-latte` | #1e66f5 | #eff1f5 | ansi:Reset | #e6e9ef | #bdd0f5 | #ccd0da | #bcc0cc | #e6e9ef | #9ca0b0 | #8c8fa1 | #4c4f69 | #6c6f85 | #8839ef | #40a02b | #df8e1d | #d20f39 | #1e66f5 | #179299 | #fe640b |
| `terminal` | ansi:Blue | ansi:Reset | ansi:Reset | ansi:DarkGray | ansi:Reset | ansi:Reset | ansi:DarkGray | ansi:DarkGray | ansi:Gray | ansi:White | ansi:Reset | ansi:Gray | ansi:Gray | ansi:Green | ansi:Yellow | ansi:LightRed | ansi:Blue | ansi:Cyan | ansi:Yellow |
| `tokyo-night` | #7aa2f7 | #1a1b26 | ansi:Reset | #232636 | #2d3650 | #24283b | #414868 | #1a1b26 | #565f89 | #697196 | #c0caf5 | #a9b1d6 | #bb9af7 | #9ece6a | #e0af68 | #f7768e | #7aa2f7 | #7dcfff | #ff9e64 |
| `tokyo-night-day` | #2e7de9 | #e1e2e7 | ansi:Reset | #d2d3da | #b6cae7 | #c4c8da | #a8aecb | #d2d3da | #8990b3 | #68709a | #3760bf | #6172b0 | #7847bd | #587539 | #8c6c3e | #f52a65 | #2e7de9 | #118c74 | #b15c00 |
| `dracula` | #bd93f9 | #282a36 | ansi:Reset | #373c52 | #463f5d | #44475a | #6272a4 | #282a36 | #6272a4 | #828cb4 | #f8f8f2 | #d2d2dc | #ff79c6 | #50fa7b | #f1fa8c | #ff5555 | #8be9fd | #8be9fd | #ffb86c |
| `nord` | #88c0d0 | #2e3440 | ansi:Reset | #434c5e | #40505d | #3b4252 | #434c5e | #2e3440 | #4c566a | #646e82 | #eceff4 | #d8dee9 | #b48ead | #a3be8c | #ebcb8b | #bf616a | #81a1c1 | #8fbcbb | #d08770 |
| `gruvbox` | #d79921 | #282828 | ansi:Reset | #323130 | #4b3f27 | #3c3836 | #504945 | #282828 | #928374 | #a89984 | #ebdbb2 | #d5c4a1 | #d3869b | #b8bb26 | #fabd2f | #fb4934 | #83a598 | #8ec07c | #fe8019 |
| `gruvbox-light` | #076678 | #fbf1c7 | ansi:Reset | #f2e5bc | #ebdbb2 | #ebdbb2 | #d5c4a1 | #f2e5bc | #928374 | #7c6f64 | #3c3836 | #504945 | #8f3f71 | #79740e | #b57614 | #9d0006 | #076678 | #427b58 | #af3a03 |
| `one-dark` | #61afef | #282c34 | ansi:Reset | #313640 | #334659 | #2c313a | #3e4451 | #282c34 | #5c6370 | #737a87 | #abb2bf | #969ca8 | #c678dd | #98c379 | #e5c07b | #e06c75 | #61afef | #56b6c2 | #d19a66 |
| `one-light` | #4078f2 | #fafafa | ansi:Reset | #d8dbe2 | #cddbf8 | #f0f0f1 | #e5e5e6 | #f5f5f6 | #a0a1a7 | #686b77 | #383a42 | #686b77 | #a626a4 | #50a14f | #c18401 | #e45649 | #4078f2 | #0184bc | #986801 |
| `solarized` | #268bd2 | #002b36 | ansi:Reset | #164b57 | #083e55 | #073642 | #586e75 | #002b36 | #586e75 | #657b83 | #93a1a1 | #839496 | #d33682 | #859900 | #b58900 | #dc322f | #268bd2 | #2aa198 | #cb4b16 |
| `solarized-light` | #268bd2 | #fdf6e3 | ansi:Reset | #eee8d5 | #c9dcdf | #eee8d5 | #93a1a1 | #eee8d5 | #93a1a1 | #586e75 | #657b83 | #839496 | #d33682 | #859900 | #b58900 | #dc322f | #268bd2 | #2aa198 | #cb4b16 |
| `kanagawa` | #7e9cd8 | #1f1f28 | ansi:Reset | #363646 | #32384b | #2a2a37 | #363646 | #1f1f28 | #727169 | #87867d | #dcd7ba | #c8c3aa | #957fb8 | #76946a | #c0a36e | #c34043 | #7e9cd8 | #7fb4ca | #ffa066 |
| `kanagawa-lotus` | #4d699b | #f2ecbc | ansi:Reset | #d5cea3 | #dcd5ac | #dcd5ac | #c9cbd1 | #d5cea3 | #a09cac | #8a8980 | #545464 | #43436c | #624c83 | #6f894e | #77713f | #c84053 | #4d699b | #4e8ca2 | #cc6d00 |
| `rose-pine` | #c4a7e7 | #191724 | ansi:Reset | #26233a | #3b344b | #1f1d2e | #26233a | #26233a | #6e6a86 | #908caa | #e0def4 | #c8c5dc | #c4a7e7 | #31748f | #f6c177 | #eb6f92 | #31748f | #9ccfd8 | #ea9a97 |
| `rose-pine-dawn` | #907aa9 | #faf4ed | ansi:Reset | #e3d9cf | #f2e9e1 | #f2e9e1 | #fffaf3 | #f2e9e1 | #9893a5 | #797593 | #464261 | #797593 | #907aa9 | #286983 | #ea9d34 | #b4637a | #286983 | #56949f | #d7827e |
| `vesper` | #ffc799 | #1a1a1a | ansi:Reset | #101010 | #232323 | #232323 | #282828 | #101010 | #5c5c5c | #7e7e7e | #ffffff | #a0a0a0 | #ffd1a8 | #99ffe4 | #ffc799 | #ff8080 | #b0b0b0 | #66ddcc | #ffc799 |

The `terminal` theme only uses ANSI named colors, so it takes on the host terminal's palette.

## Custom colors

There is no way to declare a new named theme. The config takes token overrides
([`CustomThemeColors` / `ModeThemeColors`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/theme.rs#L99-L151);
[configuration docs](https://raw.githubusercontent.com/herdrdev/herdr/v0.9.3/docs/next/website/src/content/docs/configuration.mdx)):

```toml
[theme]
name = "nord"

[theme.custom]          # any of the 19 tokens
accent = "#a6e3a1"
sidebar_bg = "#181825"

[theme.custom.light]    # applied last, only when auto_switch picks light
panel_bg = "#eff1f5"

[theme.custom.dark]     # applied last, only when auto_switch picks dark
panel_bg = "#1e1e2e"
```

- Values ([`parse_color`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/config/theme.rs#L155)): `#rrggbb`,
  `#rgb`, `rgb(r,g,b)`, the 16 ANSI names (`red`, `lightblue`, `darkgray`, `magenta`/`purple`, …), or
  `reset`/`default`/`none`/`transparent`. Anything else becomes **cyan**, with a warning in the log.
- Resolution order (`resolve_palette_for_theme_name`,
  [`src/app/mod.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/app/mod.rs#L273-L299)): built-in palette, then
  `[theme.custom]`, then the legacy `[ui] accent` (only when it is not `"cyan"` and `theme.custom.accent` is unset),
  then the active mode's `[theme.custom.light|dark]`. The mode subtables apply **only** when `auto_switch = true`.
- Themes and colors come from the **client's** local config, including when viewing an SSH machine (configuration
  docs, "Reload config"). `herdr server reload-config` or the UI's reload action picks up edits.

## `auto_switch`

Fields of `ThemeConfig`: `name`, `auto_switch` (bool, default `false`), `dark_name`, `light_name`, `custom`.

- **Off** (the user's setting): herdr uses `name` (default `catppuccin`) and ignores `dark_name`, `light_name` and the
  mode subtables.
- **On**: herdr uses `dark_name` or `light_name` for the host terminal's current appearance. If no appearance is
  known yet, it uses dark (`appearance.unwrap_or(Dark)` in `resolve_effective_theme`).
- **Defaults for the pair** (`sibling_theme_names`, `src/app/mod.rs#L218`): when `dark_name`/`light_name` are not
  set, herdr derives them from `name`: catppuccin↔catppuccin-latte, tokyo-night↔tokyo-night-day,
  gruvbox↔gruvbox-light, one-dark↔one-light, solarized↔solarized-light, kanagawa↔kanagawa-lotus,
  rose-pine↔rose-pine-dawn. **`nord`, `dracula`, `vesper` and `terminal` have no sibling**, so both halves of the
  pair become the same theme (for `nord` with `auto_switch = true`, the light theme is also `nord`).
- **What sets the appearance** ([`src/terminal_theme.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/terminal_theme.rs),
  `src/client/shell/input.rs`, `src/server/clients.rs`): herdr enables the terminal's color-scheme reports
  (`CSI ? 2031 h`) and asks for the current scheme (`CSI ? 996 n`). A `CSI ? 997 ; 1 n` reply means dark and
  `CSI ? 997 ; 2 n` means light; these count as *explicit*. Without an explicit report, herdr infers the appearance
  from the OSC 11 background color: Rec. 601 luma `0.299 R + 0.587 G + 0.114 B ≥ 128` gives light, otherwise dark.
  On Unix, herdr re-reads the terminal colors after a resize or `SIGWINCH`.
- Picking a theme by hand in herdr's Settings turns `auto_switch` off (configuration docs).

## Sidebar agent-state colors

`status_color` in [`src/client/shell.rs`](https://github.com/herdrdev/herdr/blob/v0.9.3/src/client/shell.rs#L225-L237)
(the same in v0.9.5) styles the state glyph and the state text for workspace rows, agent rows, and the collapsed
sidebar:

| `AgentStatus` | sidebar word | token | `nord` | `catppuccin` | glyph (`dots` / `symbols`) | priority |
|---|---|---|---|---|---|---|
| `Blocked` (needs you) | blocked | `red` | `#bf616a` | `#f38ba8` | `●` / `×` | 4 (highest) |
| `Done` | done | `teal` | `#8fbcbb` | `#94e2d5` | `●` / `✓` | 3 |
| `Working` | working | `yellow` | `#ebcb8b` | `#f9e2af` | `●` / `◐` | 2 |
| `Idle` | idle | `green` | `#a3be8c` | `#a6e3a1` | `○` / `○` | 1 |
| `Unknown` | idle (agent panel) | `overlay0` | `#4c566a` | `#6c7086` | `·` / `·` | 0 |

The glyph set comes from the sidebar's status-indicator setting (`StatusIndicatorStyle::Dots | Symbols`). A
collapsed workspace group takes the highest-priority status of its members (`displayed_workspace_status`). Other
sidebar colors: muted labels use `overlay0`/`overlay1`, the branch label uses `mauve`, the active row background is
`active_row_bg`, and the Navigate cursor row is `selection_bg` (or `active_row_bg` when `selection_bg` is `Reset`).

## For the user's setup

`~/.config/herdr/config.toml` has `[theme] name = "nord"`, `auto_switch = false` and no `[theme.custom]`. The
effective palette is exactly the `nord` row above. The board would show working `#ebcb8b`, blocked `#bf616a`,
done `#8fbcbb`, idle `#a3be8c`, accent `#88c0d0`, text `#eceff4` and panel `#2e3440`.

## Sources

- herdr source, tag `v0.9.3` (matches the installed binary): `src/config/theme.rs`, `src/app/state.rs`,
  `src/app/mod.rs`, `src/app/theme_sync.rs`, `src/terminal_theme.rs`, `src/client/shell.rs`,
  `src/client/shell/sidebar.rs`, `src/client/shell/agent_sidebar.rs`, `src/client/shell/input.rs`,
  `src/server/clients.rs`. I compared against `v0.9.5` and found no theme changes.
- herdr docs: https://herdr.dev/llms.txt → `configuration.mdx` (Theme section).
- Installed binary: `herdr --default-config` (`[theme]` block) and `herdr api schema --json` (no theme fields).
