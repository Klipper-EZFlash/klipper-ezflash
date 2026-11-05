"""
Theme palette (dark gray + green) and the shared dialog stylesheet.

Hardcoded literals rather than redefining Textual's built-in
$accent/$panel/etc. tokens — safer across Textual versions than depending
on exactly how the active theme's design tokens are meant to be
overridden.
"""

from __future__ import annotations

_BG = "#121212"        # app/screen background
_PANEL = "#1c1c1c"      # dialog box / panel background
_SURFACE = "#1a1a1a"    # nested surface (log, list) background
_CALLOUT = "#242424"   # standalone callout/card background (completion-screen)
_ACCENT = "#3ddc84"     # green accent (borders, titles, primary buttons)
_ACCENT_HOVER = "#2fc774"
_ACCENT_TEXT = "#08130c"  # dark text on top of the green accent
_TEXT = "#e6e6e6"       # primary text
_TEXT_DIM = "#8a9199"   # secondary/pending text
_BTN_BG = "#2a2a2a"
_BTN_HOVER = "#353535"
_ERROR_BG = "#5c1f1f"
_ERROR_TEXT = "#f4b4b4"
_WARN = "#f5b83d"       # amber for must-read warnings (distinct from green accent / red error)
_WARN_BG = "#3a2f12"    # dark amber tint behind warnings

DIALOG_CSS = f"""
ModalScreen {{
    align: center middle;
    /* Explicit opaque background — Textual's default ModalScreen
       background is a translucent scrim over whatever screen is
       mounted below it, which is how a previous screen's leftover
       content (e.g. amber step-list text from an earlier flash) could
       show through around the edges of a dialog that doesn't fully
       cover the terminal. Making this fully opaque means nothing from
       underneath can bleed through, regardless of dialog width. */
    background: {_BG};
}}
#dialog-box {{
    /* Percentage width (clamped) instead of width:auto sized to content —
       this is what actually fills the terminal instead of leaving a big
       gap on one side; align:center middle above keeps it centered. */
    width: 80%;
    min-width: 50;
    max-width: 120;
    height: auto;
    max-height: 90%;
    border: solid {_ACCENT};
    background: {_PANEL};
    color: {_TEXT};
    padding: 1 2;
}}
#dialog-title {{
    text-style: bold;
    color: {_ACCENT};
    margin-bottom: 1;
}}
#dialog-body {{
    margin-bottom: 1;
}}
#dialog-warning {{
    margin-bottom: 1;
    padding: 0 1;
    background: {_WARN_BG};
    color: {_WARN};
    border-left: thick {_WARN};
    text-style: bold;
}}
#dialog-buttons {{
    height: auto;
    align: right middle;
}}
#dialog-buttons Button {{
    margin-left: 1;
}}
ListView {{
    height: auto;
    max-height: 20;
    background: {_SURFACE};
}}
ListItem {{
    background: {_SURFACE};
}}
/* The selected/highlighted row in a ListView — Textual's own
   ListItem.watch_highlighted() (see _list_item.py in the installed
   package) applies a SINGLE-dash `-highlight` class, not `--highlight`.
   Both previous attempts here used `--highlight`, which never matched
   anything — that's the entire reason no green ever appeared; it had
   nothing to do with descendant vs. direct-child combinators (`>` is
   in fact correct — ListView's own DEFAULT_CSS in _list_view.py
   confirms ListItem is a direct child of ListView).

   Two rules, mirroring Textual's own DEFAULT_CSS structure: one for
   the focused ListView (full accent bar) and one for the
   blurred/unfocused state (dimmer tint), so the bar doesn't fully
   vanish just because the terminal window lost OS focus. */
ListView > ListItem.-highlight {{
    background: {_ACCENT} 50%;
    color: {_TEXT};
    text-style: bold;
}}
ListView:focus > ListItem.-highlight {{
    background: {_ACCENT};
    color: {_ACCENT_TEXT};
    text-style: bold;
}}
Button {{
    background: {_BTN_BG};
    color: {_TEXT};
}}
Button:hover {{
    background: {_BTN_HOVER};
}}
Button.-primary {{
    background: {_ACCENT};
    color: {_ACCENT_TEXT};
    text-style: bold;
}}
Button.-primary:hover {{
    background: {_ACCENT_HOVER};
}}
Button.-error {{
    background: {_ERROR_BG};
    color: {_ERROR_TEXT};
}}
"""
