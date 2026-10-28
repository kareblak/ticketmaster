import re

import pytest

from ticketmaster import herdr_palettes
from ticketmaster.herdr_palettes import ALIASES, HERDR_TAG, PALETTES, SIBLINGS, TOKENS

# The color names herdr's `parse_color` knows, besides hex.
ANSI_NAMES = {
    "reset", "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white", "gray", "darkgray",
    "lightred", "lightgreen", "lightyellow", "lightblue", "lightmagenta", "lightcyan",
}  # fmt: skip


def test_generated_from_a_herdr_tag():
    assert re.fullmatch(r"v\d+\.\d+\.\d+", HERDR_TAG)


def test_every_theme_is_there():
    assert len(PALETTES) == 18
    assert "catppuccin" in PALETTES


def test_nineteen_tokens():
    assert len(TOKENS) == 19 == len(set(TOKENS))


@pytest.mark.parametrize("name", PALETTES)
def test_theme_has_every_token_as_a_color(name):
    palette = PALETTES[name]
    assert tuple(palette) == TOKENS
    for token, value in palette.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", value) or value in ANSI_NAMES, f"{name}.{token} = {value!r}"


def test_aliases_name_known_themes():
    assert ALIASES["catppuccin-mocha"] == "catppuccin"
    assert ALIASES["dawn"] == "rose-pine-dawn"
    assert set(ALIASES.values()) <= set(PALETTES)
    assert not set(ALIASES) & set(PALETTES)


def test_siblings_name_known_themes():
    assert SIBLINGS["kanagawa-lotus"] == ("kanagawa", "kanagawa-lotus")
    for name, (dark, light) in SIBLINGS.items():
        assert {name, dark, light} <= set(PALETTES)
        assert name in (dark, light)
    assert not {"nord", "dracula", "vesper", "terminal"} & set(SIBLINGS)


def test_module_is_data_only():
    assert {n for n in vars(herdr_palettes) if not n.startswith("_")} == {
        "HERDR_TAG", "TOKENS", "PALETTES", "ALIASES", "SIBLINGS",
    }  # fmt: skip
