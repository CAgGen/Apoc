"""Tests for editable-deck normalization and assembly.

Regression coverage for the blank-slide bug: when the model emits a slide with
extra class tokens (e.g. ``class="slide title"``), normalization must NOT wrap it
in a second ``.slide`` (which would hide the real content), and assembly must
still reliably activate the first slide.
"""

from __future__ import annotations

import re

from app import deck
from app.generation import _deck_user, _normalize_slides


def _section_tags(html: str) -> list[str]:
    return re.findall(r"<section\b[^>]*>", html)


def test_normalize_keeps_single_slide_when_class_has_extra_tokens():
    raw = '<section class="slide title"><div class="slide-content">Hello</div></section>'
    out = _normalize_slides([raw])
    # Must not double-wrap into a nested .slide (which would be display:none).
    assert len(_section_tags(out)) == 1
    assert out.count("slide-content") == 1


def test_normalize_wraps_bare_content_once():
    out = _normalize_slides(["<h1>Title</h1><p>Body</p>"])
    assert len(_section_tags(out)) == 1
    assert "slide-content" in out
    assert "Title" in out and "Body" in out


def test_normalize_adds_slide_content_without_extra_section():
    raw = '<section class="slide hero"><h1>Hi</h1></section>'
    out = _normalize_slides([raw])
    assert len(_section_tags(out)) == 1
    assert "slide-content" in out


def test_assemble_activates_first_slide_even_with_extra_class():
    deck_html = (
        '<section class="slide hero"><div class="slide-content">A</div></section>'
        '<section class="slide"><div class="slide-content">B</div></section>'
    )
    out = deck.assemble_deck(
        deck_html=deck_html, deck_css="", poc_id="poc_x",
        api_base="http://t", editable=False,
    )
    first = _section_tags(out)[0]
    assert "active" in first, f"first slide not activated: {first}"


def test_deck_user_includes_client_and_org_signature_instruction():
    prompt = _deck_user(
        {"client_name": "Acme Bank", "consulting_org": "ArcD Studio"},
        "Payments Modernization",
        "Document body",
    )
    assert "POC title: Payments Modernization" in prompt
    assert "Client company for title slide: Acme Bank" in prompt
    assert "Consulting team for title slide: ArcD Studio" in prompt
    assert "Title slide signature: Prepared for Acme Bank · Produced by ArcD Studio" in prompt
    assert "Document body" in prompt


def test_deck_user_includes_client_only_signature_instruction():
    prompt = _deck_user({"client_name": "Acme Bank"}, "T", "Body")
    assert "Client company for title slide: Acme Bank" in prompt
    assert "Consulting team for title slide" not in prompt
    assert "Title slide signature: Prepared for Acme Bank" in prompt
    assert "Produced by " not in prompt


def test_deck_user_includes_org_only_signature_instruction():
    prompt = _deck_user({"consulting_org": "ArcD Studio"}, "T", "Body")
    assert "Client company for title slide" not in prompt
    assert "Consulting team for title slide: ArcD Studio" in prompt
    assert "Title slide signature: Produced by ArcD Studio" in prompt
    assert "Prepared for " not in prompt


def test_deck_user_omits_signature_when_names_missing():
    prompt = _deck_user({"client_name": "", "consulting_org": None}, "T", "Body")
    assert "Title slide signature" not in prompt
    assert "Client company for title slide" not in prompt
    assert "Consulting team for title slide" not in prompt
    assert "POC title: T" in prompt
    assert "Body" in prompt


def test_runtime_edits_text_leaves_not_whole_slide_content():
    out = deck.assemble_deck(
        deck_html='<section class="slide"><div class="slide-content"><details class="card"><summary>Box</summary><p>Body</p></details></div></section>',
        deck_css="",
        poc_id="poc_x",
        api_base="http://t",
        editable=True,
    )
    assert "EDITABLE_TEXT_SELECTOR" in out
    assert "el.setAttribute('contenteditable', on ? 'true' : 'false')" in out
    assert "t.setAttribute('contenteditable', on ? 'true' : 'false')" not in out


def test_runtime_toggles_details_from_any_card_interior_when_not_editing():
    out = deck.assemble_deck(
        deck_html='<section class="slide"><div class="slide-content"><details class="diagram-box"><summary>Box</summary><p>Body</p></details></div></section>',
        deck_css="",
        poc_id="poc_x",
        api_base="http://t",
        editable=True,
    )
    assert "details.open = !details.open" in out
    assert "if (editing && summary)" in out
    assert ".slide .diagram-box details > summary" in out


def _assemble_css() -> str:
    """The full assembled <style> payload (viewport base + presenter + theme)."""
    return deck.assemble_deck(
        deck_html='<section class="slide"><div class="slide-content">x</div></section>',
        deck_css="", poc_id="poc_x", api_base="http://t", editable=True,
    )


def test_open_summary_collapses_so_revealed_text_stays_inside_box():
    """Bug: an open drill-down's summary kept `min-height:100%`, claiming the whole
    box and pushing the revealed paragraph OUTSIDE the box border (text escaping
    the ticket / overlapping neighbours). The fill-and-centre must be scoped to the
    CLOSED state; the open summary must collapse (`min-height:0`)."""
    css = _assemble_css()
    assert ".slide .diagram-box details:not([open]) > summary" in css
    assert ".slide .diagram-box details[open] > summary" in css
    assert "min-height: 0" in css


def test_tiles_stretch_when_closed_and_top_align_only_when_open():
    """Bug: a blanket `align-self:start` on every tile meant closed tickets never
    equalised height (ragged rows). Stretch by default; only top-align the row/grid
    that actually has an open drill-down so just that card grows."""
    css = _assemble_css()
    assert ".slide .diagram-row:has(details[open])" in css
    assert ".slide .grid:has(details[open]) > *" in css
    # The old unconditional pin must be gone.
    assert ".slide .card { align-self: start; }" not in css


def test_slide_content_gets_definite_width_for_grids():
    """Bug: themes centre content with `.slide-content{margin:0 auto;max-width}`,
    whose auto margins suppress flex stretch and collapse the content to its
    shrink-to-fit width — dropping `auto-fit` grids to one column. A forced
    `width:100%` restores a real width (still capped by the theme max-width)."""
    css = _assemble_css()
    assert "width: 100% !important" in css
