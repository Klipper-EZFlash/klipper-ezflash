"""
Modal dialogs — equivalents of dialog.msgbox / yesno / menu / inputbox.

Every former `dialog` call has an equivalent ModalScreen here, and a thin
`self.msgbox(...)` / `self.yesno(...)` / `self.menu(...)` /
`self.inputbox(...)` helper on EZFlashApp (in app.py) that awaits it.
"""

from __future__ import annotations

from typing import Optional, Sequence

from textual.app import ComposeResult
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static
from rich.text import Text

from .theme import DIALOG_CSS

# Returned by ChoiceDialog / ButtonChoiceDialog when the optional "home"
# button (e.g. "Main menu") is pressed — distinct from None, which means
# Back/Escape (step back one level).
HOME_TAG = "__home__"


def _mnemonic_label(label: str) -> Text:
    """Return `label` as Rich Text with its first alphanumeric character
    underlined — a visual cue for the hotkey/type-ahead character,
    mirroring the classic `dialog` TUI's underlined access-key
    convention. Used for both ListView choice rows and Button labels."""
    text = Text(label)
    for i, ch in enumerate(label):
        if ch.isalnum():
            text.stylize("underline", i, i + 1)
            break
    return text


class _DialogActions:
    """Shared action implementations for dialog screens.

    Deliberately NOT a class that carries its own `BINDINGS` for other
    classes to inherit — every dialog screen below declares its own
    `BINDINGS` list directly (see the note on each class). This mixin only
    supplies the method bodies those bindings call, which is ordinary
    Python method resolution and unaffected by how Textual's binding
    collection walks a class's MRO.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Belt-and-suspenders alongside the `ModalScreen { align: center
        # middle; }` CSS rule: setting it imperatively too guarantees the
        # dialog is actually centered regardless of any ambiguity in how
        # a *scoped* stylesheet's type-selector (`ModalScreen`, the base
        # class) resolves against each subclass's own runtime type.
        self.styles.align_horizontal = "center"
        self.styles.align_vertical = "middle"

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_app_quit(self) -> None:
        # Calling this directly on the screen (rather than relying on the
        # key bubbling up to the App's own `q` binding) means quitting
        # works the same way regardless of whatever screen/modal is on
        # top — no dependency on bubbling semantics we can't fully see
        # from here.
        self.app.exit()

    def _activate_button_by_letter(self, char: str) -> bool:
        """Look for a Button whose label's first alphanumeric character
        matches `char` (case-insensitive); if found, press it directly —
        this is the "hotkey" half of the mnemonic-letter feature (see
        `_mnemonic_label`), so pressing e.g. "Y" activates a "Yes" button
        immediately rather than only moving focus to it."""
        char = char.lower()
        for button in self.query(Button):
            label = button.label
            label_text = getattr(label, "plain", None) or str(label)
            for ch in label_text:
                if ch.isalnum():
                    if ch.lower() == char:
                        button.press()
                        return True
                    break
        return False


# Shared binding set for arrow-key focus navigation and quit keys. Each
# dialog class below assigns its own `BINDINGS = list(_NAV_BINDINGS) +
# [...]` — copied in directly rather than inherited via a separate mixin,
# since that's what actually guaranteed Textual picks them up (whereas
# pulling BINDINGS in only through a bare mixin's inheritance was
# unreliable).
#
# NOTE: vim-style h/j/k/l navigation was dropped here — it collided with
# the ChoiceDialog type-ahead feature below (pressing "h" to jump to a
# choice starting with "h" vs. pressing "h" to move focus left are
# mutually exclusive). Plain arrow keys still work everywhere.
_NAV_BINDINGS = [
    Binding("left,up", "focus_previous", "", show=False),
    Binding("right,down", "focus_next", "", show=False),
    Binding("q", "app_quit", "Quit", show=True),
    Binding("ctrl+c", "app_quit", "Force Quit", show=False),
]


class MessageDialog(_DialogActions, ModalScreen[None]):
    """Equivalent of dialog.msgbox / infobox."""

    CSS = DIALOG_CSS
    BINDINGS = list(_NAV_BINDINGS) + [
        Binding("escape,enter", "dismiss_dialog", "OK", show=False),
    ]

    def __init__(self, message: str, title: str = "") -> None:
        super().__init__()
        self._message = message
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            if self._title:
                yield Label(self._title, id="dialog-title")
            yield Static(self._message, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button(_mnemonic_label("OK"), id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if char and len(char) == 1 and char.isalnum() and self._activate_button_by_letter(char):
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)


class ConfirmDialog(_DialogActions, ModalScreen[bool]):
    """Equivalent of dialog.yesno."""

    CSS = DIALOG_CSS
    BINDINGS = list(_NAV_BINDINGS)

    def __init__(
        self,
        message: str,
        title: str = "",
        yes_label: str = "Yes",
        no_label: str = "No",
        default_no: bool = False,
    ) -> None:
        super().__init__()
        self._message = message
        self._title = title
        self._yes_label = yes_label
        self._no_label = no_label
        self._default_no = default_no

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            if self._title:
                yield Label(self._title, id="dialog-title")
            yield Static(self._message, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button(
                    _mnemonic_label(self._no_label),
                    id="no",
                    variant="error" if self._default_no else "default",
                )
                yield Button(
                    _mnemonic_label(self._yes_label),
                    id="yes",
                    variant="default" if self._default_no else "primary",
                )

    def on_mount(self) -> None:
        self.query_one("#no" if self._default_no else "#yes", Button).focus()

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if char and len(char) == 1 and char.isalnum() and self._activate_button_by_letter(char):
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ChoiceDialog(_DialogActions, ModalScreen[Optional[str]]):
    """Equivalent of dialog.menu. `choices` is a list of (tag, label)."""

    CSS = DIALOG_CSS
    BINDINGS = list(_NAV_BINDINGS) + [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, choices: Sequence[tuple[str, str]], message: str = "", title: str = "", cancel_label: str = "Cancel", home_label: Optional[str] = None) -> None:
        super().__init__()
        self._choices = list(choices)
        self._message = message
        self._title = title
        self._cancel_label = cancel_label
        self._home_label = home_label
        # Type-ahead state: pressing a letter jumps to the next choice
        # whose label starts with it; pressing the same letter again
        # cycles to the next match after that, mirroring the classic
        # `dialog` menu's first-letter navigation.
        self._typeahead_char: Optional[str] = None
        self._typeahead_index: int = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            if self._title:
                yield Label(self._title, id="dialog-title")
            if self._message:
                yield Static(self._message, id="dialog-body")
            yield ListView(
                *[ListItem(Label(_mnemonic_label(label)), name=tag) for tag, label in self._choices]
            )
            with Horizontal(id="dialog-buttons"):
                yield Button(_mnemonic_label(self._cancel_label), id="cancel")
                if self._home_label:
                    yield Button(_mnemonic_label(self._home_label), id="home")

    def on_mount(self) -> None:
        # Without this, the ListView never gains focus on open, so
        # Textual never applies the `-highlight` class to any row (see
        # the `ListView > ListItem.-highlight` rule in theme.py) — the
        # menu renders with no highlighted row at all until the user
        # first presses an arrow key.
        list_view = self.query_one(ListView)
        if self._choices:
            list_view.index = 0
        list_view.focus()

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if not char or len(char) != 1 or not char.isalnum():
            return
        if self._jump_to_choice(char):
            event.stop()
            return
        if self._activate_button_by_letter(char):
            event.stop()

    def _jump_to_choice(self, char: str) -> bool:
        char = char.lower()
        labels = [label for _, label in self._choices]
        count = len(labels)
        if count == 0:
            return False
        start = 0
        if char == self._typeahead_char:
            start = (self._typeahead_index + 1) % count
        for offset in range(count):
            idx = (start + offset) % count
            if labels[idx].strip().lower().startswith(char):
                list_view = self.query_one(ListView)
                list_view.index = idx
                list_view.focus()
                self._typeahead_char = char
                self._typeahead_index = idx
                return True
        return False

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "home":
            self.dismiss(HOME_TAG)
            return
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ButtonChoiceDialog(_DialogActions, ModalScreen[Optional[str]]):
    """Like ChoiceDialog, but each choice renders as its own Button
    instead of a ListView row — laid out side by side in the same
    `#dialog-buttons` row style used everywhere else (ConfirmDialog,
    InputDialog, etc.), rather than stacked full-width. Better fit for a
    small, fixed set of equally-weighted choices (e.g. "Begin Flash" /
    "Reselect") where a scrollable list is overkill. The cancel button is
    optional — pass `cancel_label=None` (the default) to omit it
    entirely; Escape still dismisses with None either way, so backing out
    doesn't require a dedicated button."""

    CSS = DIALOG_CSS
    BINDINGS = list(_NAV_BINDINGS) + [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        choices: Sequence[tuple[str, str]],
        message: str = "",
        title: str = "",
        cancel_label: Optional[str] = None,
        home_label: Optional[str] = None,
        warning: str = "",
    ) -> None:
        super().__init__()
        self._choices = list(choices)
        self._message = message
        self._title = title
        self._cancel_label = cancel_label
        self._home_label = home_label
        self._warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            if self._title:
                yield Label(self._title, id="dialog-title")
            if self._message:
                yield Static(self._message, id="dialog-body")
            if self._warning:
                yield Static(self._warning, id="dialog-warning", markup=False)
            with Horizontal(id="dialog-buttons"):
                for i, (tag, label) in enumerate(self._choices):
                    yield Button(_mnemonic_label(label), id=f"choice-{i}", variant="primary" if i == 0 else "default")
                if self._cancel_label:
                    yield Button(_mnemonic_label(self._cancel_label), id="cancel")
                if self._home_label:
                    yield Button(_mnemonic_label(self._home_label), id="home")

    def on_mount(self) -> None:
        first = self.query("#dialog-buttons Button")
        if first:
            first.first().focus()

    def on_key(self, event: events.Key) -> None:
        char = event.character
        if char and len(char) == 1 and char.isalnum() and self._activate_button_by_letter(char):
            event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "home":
            self.dismiss(HOME_TAG)
            return
        index = int(event.button.id.removeprefix("choice-"))
        self.dismiss(self._choices[index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputDialog(_DialogActions, ModalScreen[Optional[str]]):
    """Equivalent of dialog.inputbox."""

    CSS = DIALOG_CSS
    BINDINGS = list(_NAV_BINDINGS) + [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, prompt: str, initial: str = "", title: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            if self._title:
                yield Label(self._title, id="dialog-title")
            yield Static(self._prompt, id="dialog-body")
            yield Input(value=self._initial, id="input-field")
            with Horizontal(id="dialog-buttons"):
                yield Button(_mnemonic_label("Cancel"), id="cancel")
                yield Button(_mnemonic_label("OK"), id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_key(self, event: events.Key) -> None:
        # Unlike the other dialogs, this one has a text field — letter
        # keys must type into it, not trigger Cancel/OK by mnemonic.
        if self.query_one(Input).has_focus:
            return
        char = event.character
        if char and len(char) == 1 and char.isalnum() and self._activate_button_by_letter(char):
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one(Input).value)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
