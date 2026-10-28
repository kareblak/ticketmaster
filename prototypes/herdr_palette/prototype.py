"""PROTOTYPE, throwaway (#187): the board in herdr's palette, three ways of mapping herdr's tokens onto it.

Question: which herdr color drives each Textual slot and each of the board's own colors, so the board looks like part
of herdr in every herdr theme?

    PYTHONPATH=.:tests <venv>/bin/python prototypes/herdr_palette/prototype.py              # SVGs + gallery.html
    PYTHONPATH=.:tests <venv>/bin/python prototypes/herdr_palette/prototype.py nord B       # live, interactive

Recolors the real board by patching its color constants and translating the color names it hard-codes (grey50, dim,
cyan, ...) at Rich's Style.parse. The real implementation will use a Textual theme plus `$vars` (#186), not this.
"""

from __future__ import annotations

import asyncio
import functools
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import rich.console
import rich.style
from textual.theme import Theme

from fakes import a_map, on, spec_of
from fake_backend import FakeBackend, workbench
from ticketmaster import board
from ticketmaster.model import Column, LaneKind, Ticket
from ticketmaster.providers import Query
from ticketmaster.session import Session

HERE = Path(__file__).parent
TOKENS = ("accent panel_bg sidebar_bg active_row_bg selection_bg surface0 surface1 surface_dim overlay0 overlay1 "
          "text subtext0 mauve green yellow red blue teal peach").split()


def palettes() -> dict[str, dict[str, str]]:
    """herdr's built-in palettes (minus `terminal`), from the research table on #184."""
    out = {}
    for line in (HERE / "palettes.md").read_text().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        out[cells[0].strip("`")] = dict(zip(TOKENS, cells[1:]))
    return out


def is_light(p: dict[str, str]) -> bool:
    r, g, b = (int(p["panel_bg"][i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b >= 128


# --- the three mappings ------------------------------------------------------------------------------------------
# Every variant shares the decided bits: session status = herdr's sidebar (working yellow, blocked red, done teal,
# idle green, unknown overlay0). They differ in how loud the rest of the board is.

@dataclass
class Variant:
    key: str
    name: str
    describe: str


VARIANTS = {
    "A": Variant("A", "Herdr sidebar, chips filled",
                 "Colored chips filled (reverse video, text in panel_bg). Lane kinds: MAP accent, SPEC mauve, "
                 "ORPHANED blue. HITL peach, AFK teal, PR mauve. Columns red/green/yellow/overlay0. Background "
                 "panel_bg, cards on it, focus active_row_bg."),
    "B": Variant("B", "Quiet, text-first",
                 "No filled chips: color is text on a surface0 pill. Lane borders surface1, card bars surface1. "
                 "Card refs in subtext0; only column heads, status and blockers carry hue. Background panel_bg."),
    "C": Variant("C", "Accent-led, two hues",
                 "Structure in accent only (MAP, focus, lane borders, assignees); SPEC mauve; ORPHANED overlay1. "
                 "HITL filled accent, AFK filled surface1. Columns: blocked red, frontier accent, claimed yellow. "
                 "Background: the pane's own (sidebar_bg = terminal default), cards on surface0."),
}


def mapping(variant: str, p: dict[str, str]) -> dict:
    """What every board color becomes: the board's constants, the names it hard-codes, and the Textual theme."""
    on_bg = p["panel_bg"]
    status = {"working": p["yellow"], "blocked": p["red"], "done": p["teal"], "idle": p["green"],
              "unknown": p["overlay0"]}
    common_names = {"grey50": p["overlay0"], "grey62": p["overlay1"], "red": p["red"], "green": p["green"],
                    "yellow": p["yellow"], "black": on_bg}
    if variant == "A":
        return dict(
            columns={Column.BLOCKED: p["red"], Column.FRONTIER: p["green"], Column.CLAIMED: p["yellow"],
                     Column.DECIDED: p["overlay0"]},
            map=p["accent"], spec=p["mauve"], loose=p["blue"], status=status,
            mode={"HITL": f"{on_bg} on {p['peach']}", "AFK": f"{on_bg} on {p['teal']}"},
            pr=f"{on_bg} on {p['mauve']}", lane_chip=lambda c: f"bold {on_bg} on {c}",
            names={**common_names, "cyan": p["blue"], "dim": p["subtext0"]},
            theme=dict(primary=p["accent"], secondary=p["mauve"], background=p["panel_bg"],
                       surface=p["surface0"], panel=p["surface0"], foreground=p["text"],
                       boost=p["active_row_bg"], border=p["surface1"], card_bar=p["surface1"]),
        )
    if variant == "B":
        pill = p["surface0"]
        return dict(
            columns={Column.BLOCKED: p["red"], Column.FRONTIER: p["green"], Column.CLAIMED: p["yellow"],
                     Column.DECIDED: p["overlay0"]},
            ref=p["subtext0"],
            map=p["accent"], spec=p["mauve"], loose=p["blue"], status=status,
            mode={"HITL": f"{p['peach']} on {pill}", "AFK": f"{p['teal']} on {pill}"},
            pr=f"{p['mauve']} on {pill}", lane_chip=lambda c: f"bold {c} on {pill}",
            names={**common_names, "cyan": p["overlay1"], "dim": p["overlay1"]},
            theme=dict(primary=p["accent"], secondary=p["mauve"], background=p["panel_bg"],
                       surface=p["surface0"], panel=p["surface_dim"], foreground=p["text"],
                       boost=p["selection_bg"], border=p["surface1"], card_bar=p["surface1"]),
        )
    return dict(  # C
        columns={Column.BLOCKED: p["red"], Column.FRONTIER: p["accent"], Column.CLAIMED: p["yellow"],
                 Column.DECIDED: p["overlay0"]},
        map=p["accent"], spec=p["mauve"], loose=p["overlay1"], status=status,
        mode={"HITL": f"{on_bg} on {p['accent']}", "AFK": f"{p['text']} on {p['surface1']}"},
        pr=f"{p['mauve']} on {p['surface0']}", lane_chip=lambda c: f"bold {on_bg} on {c}",
        names={**common_names, "cyan": p["accent"], "dim": p["overlay1"]},
        theme=dict(primary=p["accent"], secondary=p["mauve"], background=None,  # the pane: terminal default
                   surface=p["surface0"], panel=p["surface_dim"], foreground=p["text"],
                   boost=p["selection_bg"], border=p["accent"], card_bar=p["surface0"]),
    )


# --- applying a mapping to the real board --------------------------------------------------------------------------

_parse = rich.style.Style.parse.__wrapped__
NAMES: dict[str, str] = {}
_get_style = rich.console.Console.get_style


@classmethod
@functools.lru_cache(maxsize=None)
def _translated_parse(cls, definition: str):
    words = definition.split()
    if "dim" in words:  # dim becomes a muted color, not Rich's half-intensity
        words = [w for w in words if w != "dim"] + [NAMES["dim"]]
    return _parse(cls, " ".join(NAMES.get(w, w) for w in words))


def apply(theme_name: str, variant: str) -> tuple[Theme, dict]:
    p = palettes()[theme_name]
    m = mapping(variant, p)
    NAMES.clear()
    NAMES.update(m["names"])
    _translated_parse.__func__.cache_clear()
    rich.style.Style.parse = _translated_parse
    # single words like "red" or "dim" hit Rich's default theme before Style.parse; send those through it too
    rich.console.Console.get_style = lambda self, name, default=None: (
        rich.style.Style.parse(name) if isinstance(name, str) and set(name.split()) & set(NAMES)
        else _get_style(self, name, default=default))

    board.COLUMN_COLORS.update(m["columns"])
    board.MAP_COLOR, board.SPEC_COLOR, board.LOOSE_COLOR = m["map"], m["spec"], m["loose"]
    board.LANE_LABELS.update({LaneKind.MAP: ("MAP", m["map"]), LaneKind.SPEC: ("SPEC", m["spec"]),
                              LaneKind.LOOSE: ("ORPHANED", m["loose"])})
    s = m["status"]
    board.STATUS_STYLES.update({"working": ("● working", f"bold {s['working']}"),
                                "blocked": ("⚠ needs you", f"bold {s['blocked']}"),
                                "idle": ("◌ idle", s["idle"]), "done": ("✓ done", s["done"]),
                                "unknown": ("? unknown", s["unknown"]),
                                "unavailable": ("· status unavailable", s["unknown"]),
                                None: ("○ no agent", s["unknown"])})
    board.MODE_STYLES.update(m["mode"])
    board.PR_STYLE = m["pr"]
    board.SEGMENT_STYLES.update({"warn": f"bold {p['yellow']}", "filter": p["yellow"], "error": f"bold {p['red']}"})
    lane_chip = m["lane_chip"]
    board.kind_label = lambda kind: board.Text.assemble(
        (f" {board.LANE_LABELS[kind][0]} ", lane_chip(board.LANE_LABELS[kind][1])), " ")
    # B: refs in a muted color, the column's hue only on its head
    board.render_card = _with_ref_color(m["ref"]) if "ref" in m else _render_card

    t = m["theme"]
    theme = Theme(
        name=f"herdr-{theme_name}-{variant}", primary=t["primary"], secondary=t["secondary"], accent=t["primary"],
        foreground=t["foreground"], background=t["background"], surface=t["surface"], panel=t["panel"],
        boost=t["boost"], warning=p["yellow"], error=p["red"], success=p["green"], dark=not is_light(p),
        variables={"border": t["border"], "footer-background": p["surface0"], "footer-key-foreground": p["accent"],
                   "input-selection-background": p["selection_bg"], "text-muted": p["overlay1"],
                   "block-cursor-background": p["accent"], "block-cursor-foreground": p["panel_bg"]},
    )
    return theme, t


_render_card = board.render_card


def _with_ref_color(color):
    def render(view):
        text = _render_card(view)
        ref = f"{view.ticket.ref} "
        start = text.plain.find(ref)
        if start >= 0:
            text.stylize(f"bold {color}", start, start + len(ref))
        return text
    return render


class PrototypeBoard(board.BoardApp):
    def __init__(self, *args, herdr_theme: Theme, slots: dict, **kw):
        super().__init__(*args, **kw)
        self.herdr_theme, self.slots = herdr_theme, slots

    def on_mount(self) -> None:
        self.register_theme(self.herdr_theme)
        self.theme = self.herdr_theme.name
        s = self.slots
        self.stylesheet.add_source(f"""
        .lane {{ border: round {s['border']}; }}
        .card {{ border-left: wide {s['card_bar']}; }}
        .map-card {{ border-left: wide {s['primary']}; }}
        .card:focus {{ background: {s['boost']}; border-left: wide {s['primary']}; }}
        {"Screen { background: ansi_default; }" if s['background'] is None else ""}
        """, read_from=("prototype", "herdr"))
        self.refresh_css()
        super_mount = getattr(super(), "on_mount", None)
        if super_mount:
            return super_mount()


# --- sample board ---------------------------------------------------------------------------------------------------

def sample():
    now = datetime.now(timezone.utc)
    spec_body = "## Problem Statement\n\nCards vanish.\n\n## Solution\n\nKeep them.\n"
    alone = Ticket(number=70, title="Board shows standalone work", url="u70", is_open=True, body=spec_body,
                   labels=("ready-for-agent",), assignees=("alice",), has_tickets=True)
    tickets = [
        a_map(183, "The board wears herdr's theme"),
        on(183, 184, "Herdr's themes and their palettes", "research", is_open=False, closed_at=now),
        on(183, 187, "The board in herdr's palette", "prototype", assignees=("kareblak",),
           body="## Question\n\nHow should herdr's palette map onto the board?"),
        on(183, 188, "Keeping palettes in step with herdr", "grilling",
           body="## Question\n\nHow is the copy kept current?"),
        on(183, 189, "Slice the theme work", "task", open_blockers=1, blocked_by=("#187",),
           body="## Question\n\nAFK: split the work into /implement tickets."),
        spec_of(183, 190, "Spec: herdr theme on the board", assignees=("kareblak",), has_open_change_request=True,
                body="Part of #183\n\n## Problem Statement\n\nThe board ignores herdr's theme."),
        a_map(150, "Board runs without herdr"),
        on(150, 151, "Plain backend terminal", "grilling", assignees=("bob",),
           body="## Question\n\nWhich terminal does plain open?"),
        on(150, 152, "Which agent states plain knows", "research", body="## Question\n\nWhat can plain see?"),
        on(150, 153, "Decide the session model", "grilling", is_open=False, closed_at=now),
        alone,
        Ticket(number=71, title="Show the lane", url="u71", is_open=True, parent_number=70, parent_url="u70",
               lane_spec=70, body="## Parent\n\n#70\n\n## What to build\n\nShow it.\n\n## Acceptance criteria\n\n- [ ] x\n",
               assignees=("alice",)),
        Ticket(number=72, title="Keep closed cards", url="u72", is_open=True, parent_number=70, parent_url="u70",
               lane_spec=70, open_blockers=1, blocked_by=("#71",),
               body="## Parent\n\n#70\n\n## What to build\n\nKeep.\n\n## Acceptance criteria\n\n- [ ] x\n"),
        Ticket(number=95, title="Stray ticket", url="u95", is_open=True, loose=True,
               body="## Parent\n\n#999\n\n## What to build\n\nOrphan.\n\n## Acceptance criteria\n\n- [ ] x\n"),
    ]
    sessions = {
        187: [Session(187, "/w/issue-187", status="working", handle=1)],
        188: [Session(188, "/w/issue-188", status="blocked", reason="permission", handle=2)],
        190: [Session(190, "/w/issue-190", status="done", handle=3)],
        151: [Session(151, "/w/issue-151", status="idle", handle=4)],
        71: [Session(71, "/w/issue-71", status="unknown", handle=5)],
    }
    return tickets, sessions


def make_app(theme_name: str, variant: str):
    from fakes import FakeProvider

    theme, slots = apply(theme_name, variant)
    slots = {**slots, "primary": slots["primary"]}
    tickets, sessions = sample()
    return PrototypeBoard(FakeProvider(tickets, "kareblak"), Query(), interval=None,
                          workbench=workbench(FakeBackend(sessions)), herdr_theme=theme, slots=slots)


THEMES = ["nord", "catppuccin", "catppuccin-latte", "gruvbox-light", "tokyo-night", "rose-pine-dawn"]


async def shoot(theme_name: str, variant: str, out: Path) -> Path:
    app = make_app(theme_name, variant)
    async with app.run_test(size=(170, 46)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down", "down")  # focus a card with a working session
        await pilot.pause()
        path = out / f"{theme_name}-{variant}.svg"
        app.save_screenshot(path.name, str(out))
    return path


def gallery(out: Path, pals: dict) -> None:
    def svg(t, v):
        return re.sub(r"<\?xml[^>]*>", "", (out / f"{t}-{v}.svg").read_text())
    sections = []
    for v, var in VARIANTS.items():
        shots = "".join(f'<figure data-theme="{t}"><figcaption>{t}</figcaption>{svg(t, v)}</figure>' for t in THEMES)
        sections.append(f'<section data-variant="{v}"><h2>{v}: {var.name}</h2><p>{var.describe}</p>{shots}</section>')
    (out / "gallery.html").write_text(f"""<!doctype html><meta charset="utf-8"><title>Herdr palette prototype</title>
<style>body{{font:14px system-ui;margin:16px;background:#111;color:#ddd}} section{{display:none}}
section.on{{display:block}} figure{{margin:0 0 24px}} svg{{width:100%;height:auto}} figure{{display:none}}
figure.on{{display:block}} nav{{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);background:#fff;
color:#000;border-radius:99px;padding:6px 14px;box-shadow:0 2px 10px #0008;z-index:9}} nav button{{margin:0 4px}}</style>
{''.join(sections)}
<nav><button id=pv>‹</button><b id=lv></b><button id=nv>›</button> &nbsp;
<button id=pt>‹</button><b id=lt></b><button id=nt>›</button></nav>
<script>
const V={list(VARIANTS)!r},T={THEMES!r};let q=new URLSearchParams(location.search);
let v=Math.max(0,V.indexOf(q.get('variant'))),t=Math.max(0,T.indexOf(q.get('theme')));
function show(){{document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.dataset.variant==V[v]));
document.querySelectorAll('figure').forEach(f=>f.classList.toggle('on',f.dataset.theme==T[t]));
lv.textContent='variant '+V[v];lt.textContent=T[t];history.replaceState(0,'','?variant='+V[v]+'&theme='+T[t]);}}
pv.onclick=()=>{{v=(v+V.length-1)%V.length;show()}};nv.onclick=()=>{{v=(v+1)%V.length;show()}};
pt.onclick=()=>{{t=(t+T.length-1)%T.length;show()}};nt.onclick=()=>{{t=(t+1)%T.length;show()}};
addEventListener('keydown',e=>{{if(e.key=='ArrowLeft')pv.onclick();if(e.key=='ArrowRight')nv.onclick();
if(e.key=='ArrowUp'){{e.preventDefault();pt.onclick()}}if(e.key=='ArrowDown'){{e.preventDefault();nt.onclick()}}}});show();
</script>""")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        make_app(sys.argv[1], sys.argv[2]).run()
    else:
        out = HERE / "shots"
        out.mkdir(exist_ok=True)
        for t in THEMES:
            for v in VARIANTS:
                asyncio.run(shoot(t, v, out))
                print("wrote", out / f"{t}-{v}.svg")
        gallery(out, palettes())
        print("gallery:", out / "gallery.html")
