# Textual themes at runtime, and theming the board's own colors

Research for #186 (map #183, "The board wears herdr's theme").

Investigated 2026-10-28 against Textual **8.2.8** (the version installed in this repo's `.venv`), Textual's
source on GitHub (tags `v0.86.0` and `main`), its CHANGELOG, and the docs at textual.textualize.io. The claims
about runtime behaviour were checked with a headless `App.run_test()` probe on 8.2.8 (see "Probe" at the end).

## Summary

1. **Build and register.** `Theme(name=..., primary=..., secondary=..., accent=..., warning=..., error=...,
   success=..., foreground=..., background=..., surface=..., panel=..., boost=..., dark=..., variables={...})`,
   then `app.register_theme(theme)` and `app.theme = theme.name`. Only `name` and `primary` are required; every
   color is any CSS color string. Setting `App.theme` to an unregistered name raises `InvalidThemeError`.
2. **Runtime switch repaints everything.** Setting `App.theme` regenerates the CSS variables, reparses the
   stylesheet, reapplies it to every node on every screen, and then publishes `theme_changed_signal`. Already
   mounted widgets pick up new `$variables` in CSS **and** in Textual `Content` styles without any code of ours.
3. **Slots → variables.** Each of the 13 base colors gets `$name` plus `-lighten-1..3` / `-darken-1..3`; on
   top come `$text*`, `$*-muted`, `$foreground-muted`, `$block-cursor-*`, `$border`, `$footer-*`, `$scrollbar-*`,
   etc. Table below.
4. **Custom variables: yes.** `Theme.variables` both overrides built-in variables and adds new ones
   (`$tm-blocked`). A custom variable that CSS references must also be declared in
   `App.get_theme_variable_defaults()`, or any theme that lacks it fails the stylesheet with
   `UnresolvedVariableError`.
5. **The board's Rich colors.** Hard-coded Rich `Text` span styles (`"red"`, `"italic cyan"`, `"grey62"`,
   `"bold black on cyan"`, `PR_STYLE`) never follow the theme. Putting `$accent` into a Rich `Text` **span**
   crashes (`MissingStyle`), because Textual parses Rich span styles with Rich's parser. The clean route is to
   build Textual `Content` instead of Rich `Text`: `Content` keeps style strings unparsed until render, resolves
   `$variables` against the live stylesheet then, and repaints by itself on a theme change. The fallback,
   keeping Rich `Text`, is to resolve hex values from `app.theme_variables` and rebuild on
   `theme_changed_signal`.
6. **Version floor.** Themes landed in **0.86.0**, and `$variables` in Content/markup in **2.0.0**. The current
   `textual>=0.80,<9` floor is too low for either. The theming work needs `textual>=2.0,<9`, or `>=0.86` if it
   stays with Rich `Text` and the signal.

## 1. Building and registering a Theme

`textual/theme.py` (8.2.8) defines `Theme` as a dataclass:

```python
@dataclass
class Theme:
    name: str
    primary: str
    secondary: str | None = None
    warning: str | None = None
    error: str | None = None
    success: str | None = None
    accent: str | None = None
    foreground: str | None = None
    background: str | None = None
    surface: str | None = None
    panel: str | None = None
    boost: str | None = None
    dark: bool = True
    luminosity_spread: float = 0.15
    text_alpha: float = 0.95
    variables: dict[str, str] = field(default_factory=dict)
    ansi: bool = False   # added 8.2.5
```

Everything except `name` and `primary` is optional, and values are parsed with Textual's `Color.parse`, so
`"#rrggbb"`, `"rgb(…)"`, `"hsl(…)"` and named colors all work. That is enough to build a theme from herdr's
values at runtime. `to_color_system()` turns it into a `textual.design.ColorSystem`.

`App.register_theme(theme)` stores it in `_registered_themes`, overwriting any theme with the same name.
`App.theme` is a `Reactive[str]`; `_validate_theme` raises `InvalidThemeError` ("Theme 'x' has not been
registered…") for an unknown name (`textual/app.py`, probe confirmed). `App.get_theme(name)`,
`App.available_themes` and `App.current_theme` read them back; `unregister_theme` removes one. The env var
`TEXTUAL_THEME` (comma list) picks the initial theme (CHANGELOG 0.86.0).

Docs: <https://textual.textualize.io/guide/design/> (Themes, "Creating custom themes").

Gotcha: `Theme.dark` matters beyond the colors themselves. It picks the default background/surface when they
are not given, the `-dark-mode`/`-light-mode` App class, the `ansi_theme_dark`/`ansi_theme_light` truecolor
filter, and whether `$boost` is generated (see below). For herdr palettes it should come from the background's
luminance.

## 2. What happens when `App.theme` changes at runtime

`App._watch_theme` (`textual/app.py`, 8.2.8):

```python
theme = self.current_theme
classes = {name: False for name in self.classes if name.startswith("-theme-")}
classes[f"-theme-{self.current_theme.name}"] = True
classes["-dark-mode"] = dark
classes["-light-mode"] = not dark
self.update_classes(classes, update=False)
self._refresh_truecolor_filter(self.ansi_theme)
self._invalidate_css()
self.call_next(partial(self.refresh_css, animate=False))
self.call_next(self.theme_changed_signal.publish, theme)
```

`App.refresh_css` calls `stylesheet.set_variables(self.get_css_variables())`, `stylesheet.reparse()`,
`stylesheet.update(self.app)` (re-applies rules to every node), refreshes the current screen's layout, and then
updates every other screen in the stack. On each node, `Stylesheet.replace_rules` ends in
`node.notify_style_update()`, which for a `Widget` clears `_rich_style_cache`, `_visual_style_cache` and
`_visual_style`.

So after a switch:

- CSS using `$accent`, `$panel`, … repaints on every mounted widget, modals included (probe: `#css` went
  `#fe0000` → `#00ffff`).
- Textual `Content` whose spans carry `$variable` styles repaints too, with no code of ours (probe: `markup`,
  `assemble`, `stylize`).
- `theme_changed_signal` is published **after** `refresh_css` (both via `call_next`, in that order), so a
  subscriber that reads `app.theme_variables` sees the new values.
- Rich `Text` with literal colors does not change (probe: `richred` stayed `#ff0000`).

`theme_changed_signal` is a `textual.signal.Signal[Theme]`. Subscribe from a mounted widget with
`self.app.theme_changed_signal.subscribe(self, callback)`; the subscription goes away when the widget is
removed.

## 3. Which theme slots feed which CSS variables

From `ColorSystem._generate()` (`textual/design.py`, 8.2.8; this is the non-ANSI path, which the board uses
since `Theme.ansi` defaults to `False`):

| Slot / variable | Derived from |
| --- | --- |
| `$primary` | `primary` (required) |
| `$secondary` | `secondary` or `primary` |
| `$warning` | `warning` or `primary` |
| `$error` | `error` or `secondary` (→ `primary`) |
| `$success` | `success` or `secondary` |
| `$accent` | `accent` or `primary` |
| `$background` | `background`, else `#121212` dark / `#efefef` light (`DEFAULT_*_BACKGROUND`) |
| `$surface` | `surface`, else `#1e1e1e` dark / `#f5f5f5` light |
| `$foreground` | `foreground` or `background.inverse` |
| `$panel` | `panel`, else `surface.blend(primary, 0.1)`, plus `boost` in dark mode |
| `$boost` | `boost` or contrast text at 4% alpha, **but only when `panel` is unset and `dark` is true**; otherwise `transparent` |
| `$primary-background`, `$secondary-background` | primary / secondary, blended into the background in dark mode |
| `$<base>-lighten-{1,2,3}` / `-darken-{1,2,3}` | each of the 13 colors above, stepped by `luminosity_spread / 2` |
| `$text` / `$text-muted` / `$text-disabled` | `auto 87%` / `auto 60%` / `auto 38%`: contrast against whatever background they land on |
| `$text-primary` … `$text-accent` | contrast text tinted 66% with the slot color |
| `$primary-muted` … `$success-muted`, `$accent-muted` | slot blended 70% into `background` |
| `$foreground-muted` / `$foreground-disabled` | `foreground` at 60% / 38% alpha |
| `$border` / `$border-blurred` | `primary` / `surface.darken(0.025)` |
| `$surface-active` | `surface.lighten(spread / 2.5)` |
| `$block-cursor-*`, `$block-hover-background` | text / `primary` / `primary` 30% / `boost` 10% |
| `$scrollbar*` | `background-darken-1` + `primary` at 40/50%; active = `primary` |
| `$footer-*` | `foreground`, `panel`, key = `accent` |
| `$input-*`, `$link-*`, `$markdown-h1..6-*`, `$button-*`, `$screen-selection-*` | primary / foreground / text |

About `$boost`: the `boost` slot is only read when `background` is not an ANSI color, `panel` is unset, and
`dark` is true. Otherwise `$boost` is `transparent`, even if the theme sets `boost`. The board's `.card:focus { background:
$boost }` is therefore invisible under a light theme or a theme that sets `panel`. That matters for #183.

The board's current CSS uses `$panel`, `$panel-lighten-2`, `$accent`, `$accent-darken-2`, `$boost`,
`$text-muted` and `$surface`, all generated by every theme.

Docs: <https://textual.textualize.io/guide/design/> ("Theme variables", "Base colors", "Shades").

## 4. Custom variables in a theme

Two mechanisms, both present since 0.86.0:

- **`Theme.variables`**: `App.get_css_variables()` does
  `variables = {**theme.to_color_system().generate(), **theme.variables}`, so a theme can override any built-in
  (`"block-cursor-background"`, `"footer-key-foreground"`, … as the built-in `nord`/`gruvbox` themes do) and
  add new names. (`ColorSystem._generate` itself only consults `variables` for keys it knows; the merge in
  `get_css_variables` is what brings new names in.)
- **`App.get_theme_variable_defaults()`**: returns app-level fallbacks; the final map is
  `{**app_defaults, **theme_variables}`. Its docstring: "If a variable is referenced in CSS but does not appear
  either here or in the theme, the CSS will fail to parse on startup." The probe confirmed this: a stylesheet
  referencing an undeclared `$tm-undeclared` fails with `UnresolvedVariableError`.

So the board can declare e.g. `tm-blocked`, `tm-frontier`, `tm-claimed`, `tm-decided`, `tm-map`, `tm-spec`,
`tm-loose`, `tm-pr`, `tm-hitl`, `tm-afk` in `get_theme_variable_defaults()`, defaulting to today's colors or to
built-ins (`$error`, `$success`, `$warning`, `$text-muted`, `$accent`, …), and a herdr-derived `Theme` can set
them in `variables`. Values may be any CSS color, including alpha (`"#81a1c1 35%"`, as built-in `nord` does)
and `auto N%`. Giving each a concrete color is the safe choice; whether one variable can reference another
inside `variables` was not tested.

The probe: `#custom { color: $tm-blocked }` rendered `#00ff00` under theme A and `#ff00ff` after switching to
theme B.

`app.theme_variables` holds the merged map after each `get_css_variables()` call. It reads reliably in code
from 0.86.2 ("Fixed theme variables being unavailable in code until refresh_css was called", #5254).

## 5. Theming the board's Rich `Text` colors

Today `board.py` builds every card and header as `rich.text.Text` with literal styles: `COLUMN_COLORS`
(`red`/`green`/`yellow`/`grey50`), `MAP_COLOR`/`SPEC_COLOR`/`LOOSE_COLOR`, `STATUS_STYLES`, `MODE_STYLES`
(`black on yellow`/`black on cyan`), `PR_STYLE` (`black on magenta`), the lane chip `bold black on <color>`,
`italic cyan` assignees, `grey62` notes and `bold red` errors. None of these move with the theme.

How Textual turns a widget's renderable into pixels: `Widget._render()` calls `visualize(widget,
widget.render())`; a Rich `Text` becomes `Content.from_rich_text(text)` (`textual/content.py`). There:

- **span** styles that are strings are parsed eagerly with `app.console.get_style`, Rich's parser. `"$accent"`
  is not a Rich color, so it raises `rich.errors.MissingStyle: unable to parse '$accent' as color` (probe).
- the **whole-Text** `text.style`, when it is a string, is kept as a string via `content.stylize_before(...)`,
  so `Text("x", style="$accent")` does resolve and repaint. That covers only a single-style Text, not the
  board's multi-span cards.

So there are three ways to theme the cards:

### A. Build Textual `Content` instead of Rich `Text` (recommended, Textual ≥ 2.0)

`Content` keeps span styles as strings until `Content.render(parse_style=...)`, where `textual.style.Style.parse`
→ `textual.markup.parse_style(style, variables=None)` substitutes from `app.stylesheet._variable_tokens`, which
`refresh_css` has just replaced. Style strings use Textual's style grammar: `"bold $text on $accent"`,
`"italic $text-muted"`, `"$tm-blocked"`, `"$accent 50%"`, `"dim"`. The rendered visual is cached in
`_layout_cache["_render.visual"]`, but it is a `Content` that still holds the `$` strings, and theme changes
clear the style caches, so it re-resolves on the next paint. The probe confirmed repaint after a switch for
`Content.assemble(("chip", "bold $background on $accent"), …)`, `Content("x").stylize("$accent")` and
`Content.from_markup("[$accent]markup[/] [bold $text on $tm-chip]chip[/]")`.

Mapping for board.py: `Text()` → `Content()`; `text.append(s, style=...)` → `content = content.append(...)` or
collect parts into `Content.assemble(...)` (`Content` is immutable, so its methods return new instances);
`Text.assemble` → `Content.assemble`; `Text("\n").join` → `Content("\n").join`. `clamp_lines`, which
`Card.render` uses for Decided cards, wraps a Rich `Text`; it would need a `Content` port
(`Content.wrap`/`split`/`divide` exist) or a check that it still works. `Static`/`Label` accept `Content`
(typed since 3.0, rendered since 2.0). Prefer `Content.assemble` / `Content.styled` with styles from constants
over `from_markup`, because ticket titles may contain `[`.

For chips, `"bold $text on $tm-pr"` gives automatic contrast: `$text` is `auto 87%`, so the foreground flips
black/white against the chip's background (probe: the `chip` foreground went `#ffffff` → `#000000` when
`$tm-chip` went blue → yellow). That replaces the hard-coded `black on …`.

Docs: <https://textual.textualize.io/guide/content/> ("Content markup … You can also use CSS variables in
markup").

### B. Keep Rich `Text`, resolve hex from the theme, rebuild on the signal (Textual ≥ 0.86.2)

Read concrete values from `app.theme_variables["accent"]` (or `app.get_css_variables()`), build Rich styles
with them, and subscribe `app.theme_changed_signal` to rebuild and `update()` the affected widgets. The probe's
`Repainter` did this and repainted correctly. Downsides: every card needs a rebuild path (the board already
rebuilds cards on refresh, so a full re-render on the signal is plausible), and values like `auto 60%` /
`#fff 4%` are not Rich colors, so only concrete hex variables can be used.

### C. Whole-Text `style="$var"`

This works, but only for single-style text. It does not fit cards.

### A note on named ANSI colors

When `App.ansi_color` is off (the default), Textual converts Rich's named ANSI colors to truecolor through
`App.ansi_theme_dark` / `ansi_theme_light` (Rich `TerminalTheme`s, default MONOKAI / ALABASTER) and does not
pass them to the terminal palette. `"red"` on the board is therefore not herdr's red either. Since 8.2.5 a
`Theme(ansi=True)` (built-ins `ansi-dark`/`ansi-light`) switches the whole color system to `ansi_*` values that
the terminal itself resolves. That is an alternative way to "wear the host terminal's palette", but it is only
available at ≥ 8.2.5 and flattens shades (`-lighten-N` = base, `$panel`/`$surface`/`$boost` = transparent).

## 6. Version range

| Feature | First version | Source |
| --- | --- | --- |
| `Theme`, `App.theme`, `register_theme`, `get_theme`, `available_themes`, `current_theme`, `theme_changed_signal`, `get_theme_variable_defaults`, `Theme.variables`, `TEXTUAL_THEME` | 0.86.0 (2024-11-16) | CHANGELOG 0.86.0, PR #5087/#5238; `theme.py`/`app.py` at tag `v0.86.0` |
| `app.theme_variables` readable before first `refresh_css` | 0.86.2 | CHANGELOG, PR #5254 |
| Textual markup + `Content` with `$variables` in styles | 2.0.0 (2025-02-16) | CHANGELOG 2.0.0, PR #5485 |
| `Static`/`Label` typed to accept `Content` | 3.0.0 | CHANGELOG 3.0.0, PR #5618 |
| `Theme.ansi`, `ansi-dark`/`ansi-light` themes | 8.2.5 | CHANGELOG 8.2.5, PR #6513 |

0.80–0.85 had no `Theme` at all (`App.dark` plus a fixed design), so **the current floor `textual>=0.80` cannot
support #183.** Option A needs `textual>=2.0,<9`; option B needs `textual>=0.86.2,<9`. Everything above was
verified on 8.2.8. The code paths quoted (`_watch_theme`, `refresh_css`, `get_css_variables`,
`ColorSystem._generate`) have the same shape at `v0.86.0`, and every variable the board's CSS uses today
(`$panel`, `$panel-lighten-2`, `$accent`, `$accent-darken-2`, `$boost`, `$text-muted`, `$surface`), plus `$text`,
`$foreground-muted` and the `$text-*`/`$*-muted` families, is generated at `v0.86.0` too. Later releases added
some (`$screen-selection-*`, `$ansi-*`), so any new variable should be checked against the chosen floor or
declared in `get_theme_variable_defaults()`.

## Probe

Scripts were run with the repo's venv (`.venv/bin/python -I`, Textual 8.2.8) in `App.run_test()`. Two themes, A
(`accent #ff0000`, `tm-blocked #00ff00`) and B (`accent #00ffff`, `tm-blocked #ff00ff`). Each widget's first
rendered line was read with `render_line(0)` before and after `app.theme = "probe-b"; await pilot.pause()`:

| Widget | A | B |
| --- | --- | --- |
| CSS `color: $accent` | `#fe0000` | `#00ffff` |
| CSS `color: $tm-blocked` (custom) | `#00ff00` | `#ff00ff` |
| `Content.from_markup("[$accent]…")` | `#fe0000` | `#00ffff` |
| `Content.assemble(("chip", "bold $background on $accent"))` bg | `#fe0000` | `#00ffff` |
| Rich `Text("…", style="red")` | `#ff0000` | `#ff0000` (unchanged) |
| Rich `Text("…", style="$accent")` (whole-Text style) | `#fe0000` | `#00ffff` |
| Rich `Text().append("…", style="$accent")` (span) | `MissingStyle` crash | n/a |
| Rich `Text` rebuilt from `theme_variables` on `theme_changed_signal` | `#fe0000` | `#00ffff` |
| `app.theme = "nope"` | `InvalidThemeError` | n/a |
| CSS referencing undeclared `$tm-undeclared` | `UnresolvedVariableError` | n/a |

## Sources

- Textual 8.2.8 source: `textual/theme.py`, `textual/design.py` (`ColorSystem._generate`), `textual/app.py`
  (`get_theme_variable_defaults`, `get_css_variables`, `register_theme`, `_validate_theme`, `_watch_theme`,
  `refresh_css`), `textual/css/stylesheet.py` (`replace_rules`, `update_nodes`), `textual/widget.py`
  (`_render`, `notify_style_update`), `textual/content.py` (`from_rich_text`, `render`), `textual/markup.py`
  (`parse_style`).
- Textual on GitHub, `src/textual/theme.py` and `src/textual/app.py` at tag `v0.86.0`.
- Textual CHANGELOG.md (`main`): 0.86.0, 0.86.2, 2.0.0, 3.0.0, 8.2.5 entries.
- Docs: <https://textual.textualize.io/guide/design/>, <https://textual.textualize.io/guide/content/>.
