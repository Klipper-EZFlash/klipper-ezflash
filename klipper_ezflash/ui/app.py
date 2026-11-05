"""
Main application — EZFlashApp, composed from FlashFlowMixin (flash_flow.py)
plus App itself.
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Optional, Sequence

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, LoadingIndicator

from ..model import BoardDatabase
from ..settings import InterfaceSettings
from ..util import find_katapult, _KATAPULT_CLONE_URL, check_can_host_state, fix_can_host_state, check_uart_host_state, fix_uart_host_state, reboot_host, start_services, update_katapult, clone_katapult as _clone_katapult_repo
from ..session import load_session, clear_session
from .. import detect as detect_mod
from .theme import _BG, _PANEL, _TEXT, _ACCENT
from .dialogs import MessageDialog, ConfirmDialog, ChoiceDialog, ButtonChoiceDialog, InputDialog, HOME_TAG
from .flash_flow import FlashFlowMixin


class EZFlashApp(FlashFlowMixin, App):
    TITLE = "Klipper EZFlash"
    # The command palette (default Ctrl+P) isn't relevant to this app and
    # was cluttering the footer ("^p palette" alongside "q Quit") — off.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Force Quit", show=False),
    ]
    CSS = f"""
    Screen {{
        background: {_BG};
        color: {_TEXT};
    }}
    Header {{
        background: {_PANEL};
        color: {_ACCENT};
    }}
    Footer {{
        background: {_PANEL};
    }}
    LoadingIndicator {{
        color: {_ACCENT};
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._board_db: Optional[BoardDatabase] = None
        self._katapult_checked = False
        # Services WE stopped (see stop_services_for_flash, in
        # FlashFlowMixin) — only ever restarted once, when the app exits
        # (main(), after run() returns), not after each individual flash:
        # the user may want to flash more than one board in a single app
        # session, and bouncing Klipper/Moonraker between each one would
        # be needless churn (and would re-trigger the same disruption
        # we're trying to avoid by stopping them in the first place).
        self._stopped_services: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield LoadingIndicator(id="startup-loading")
        yield Footer()

    async def on_mount(self) -> None:
        self.run_worker(self.launch(), exclusive=True)

    def _clear_startup_loading(self) -> None:
        """Remove the startup LoadingIndicator, if it's still mounted.
        Called right before the first dialog of launch() appears, so the
        user gets visual feedback during whatever setup work (board
        database parsing, etc.) happens before that point instead of a
        blank-looking screen that reads as "frozen"."""
        try:
            self.query_one("#startup-loading", LoadingIndicator).remove()
        except Exception:
            pass

    # ---- thin awaitable wrappers replacing the old dialog calls ----------

    async def msgbox(self, message: str, title: str = "") -> None:
        await self.push_screen_wait(MessageDialog(message, title))

    async def yesno(self, message: str, title: str = "", yes_label: str = "Yes", no_label: str = "No", default_no: bool = False) -> bool:
        return await self.push_screen_wait(ConfirmDialog(message, title, yes_label, no_label, default_no))

    async def menu(self, choices: Sequence[tuple[str, str]], message: str = "", title: str = "", cancel_label: str = "Cancel", home_label: Optional[str] = None) -> Optional[str]:
        return await self.push_screen_wait(ChoiceDialog(choices, message, title, cancel_label, home_label))

    async def button_menu(self, choices: Sequence[tuple[str, str]], message: str = "", title: str = "", cancel_label: Optional[str] = None, home_label: Optional[str] = None) -> Optional[str]:
        return await self.push_screen_wait(ButtonChoiceDialog(choices, message, title, cancel_label, home_label))

    async def inputbox(self, prompt: str, initial: str = "", title: str = "") -> Optional[str]:
        return await self.push_screen_wait(InputDialog(prompt, initial, title))

    # ---- katapult checkout check / clone -----------------------------

    async def check_katapult(self) -> None:
        if self._katapult_checked:
            return

        katapult_path = find_katapult()
        if katapult_path is not None:
            self._katapult_checked = True
            error_msg = await asyncio.to_thread(update_katapult, katapult_path)
            if error_msg:
                await self.msgbox(f"Unable to update katapult from git:\n\n{error_msg}", title="Katapult update failed")
            return

        self._katapult_checked = True
        tag = await self.menu(
            choices=[("clone", "Clone katapult now"), ("quit", "Quit")],
            message=(
                "KATAPULT NOT FOUND\n\n"
                "Klipper-ezflash requires 'katapult' (a bootloader flash helper) to be installed.\n\n"
                f"It can be cloned from:\n  {_KATAPULT_CLONE_URL}"
            ),
            title="Katapult not found",
            cancel_label="Quit",
        )
        if tag == "clone":
            await self.clone_katapult()

    async def clone_katapult(self) -> None:
        clone_target = Path.home() / "katapult"
        error_msg = await asyncio.to_thread(_clone_katapult_repo, clone_target)
        if error_msg:
            await self.msgbox(f"Failed to clone katapult:\n\n{error_msg}", title="Clone failed")
            return
        await self.msgbox(
            f"Katapult has been cloned successfully!\n\nLocation: {clone_target}\n\nThe tool will now check for it again.",
            title="Katapult cloned",
        )
        self._katapult_checked = False
        await self.check_katapult()

    # ---- board selection wizard ---------------------------------------

    async def select_board_and_interface(self):
        """Manufacturer -> model -> (variant) -> board info -> (interface)
        -> confirm wizard behind the main menu's "Select a board" entry.
        Returns (board, chosen_if_type) once the user confirms a flash —
        chosen_if_type is None only if the board genuinely has no
        interfaces declared — or None if the user backed out of the
        wizard entirely (Back/Escape at the manufacturer step, or the
        "Main menu" button on any step).

        Lives here rather than in flash_flow.py: selecting/confirming a
        board and interface is menu navigation with no hardware or build
        involvement, the same category of thing the
        settings/CAN-host menus already handle, and keeping it out of
        flash_flow.py keeps that file scoped to the actual compile/
        detect/flash sequence — relevant to the eventual Phase 2 UI-
        independence split noted in flash_flow.py's module docstring.
        """
        return await self._run_board_wizard()

    async def _run_board_wizard(self):
        """Step machine behind select_board_and_interface(). Escape / the
        dialog's Cancel-slot button ("Back" on every step, including the
        first) steps back one level rather than aborting the whole wizard
        outright; at the very first step there's nothing earlier to return
        to, so it exits the wizard entirely and returns None (every caller
        already treats a None result as "user backed out, do nothing
        further"). The "Main menu" button on every step does the same
        thing from anywhere: returns None straight away.

        The board info step is a midpoint: it shows the board summary and
        offers Flash (continue to interface/confirm), Back, or Main menu,
        so it doubles as a board lookup.

        Steps variant and interface are skipped, in both directions, when
        they don't apply to the board in question (a single variant, or a
        single/no interface).

        This is one flat sequence rather than separate wizards re-entered
        partway through: an earlier version of the flash flow ran its
        own separate interface/confirmation loop, and treating Back on
        those later steps as "start board selection again" silently
        discarded the manufacturer/model/variant already chosen instead
        of stepping back one level — that's the bug this replaced.
        """
        if self._board_db is None:
            self._board_db = BoardDatabase()
        bdb = self._board_db
        boards = list(bdb.get_all())
        manufacturers = tuple(sorted(set(b.manufacturer for b in boards)))

        STEP_MFR, STEP_MODEL, STEP_VARIANT, STEP_INFO, STEP_INTERFACE, STEP_CONFIRM = range(6)
        selected_mfr: Optional[str] = None
        selected_model: Optional[str] = None
        board = None
        chosen_if_type = None
        ifaces: list = []

        def _board_variants():
            return tuple(sorted(set(
                b.variant for b in boards
                if b.manufacturer == selected_mfr and b.model == selected_model
            )))

        step = STEP_MFR
        while True:
            if step == STEP_MFR:
                tag = await self.menu(
                    choices=[(str(i), n) for i, n in enumerate(manufacturers)],
                    message="Select manufacturer",
                    cancel_label="Main menu",
                )
                if tag is None:
                    return None
                selected_mfr = manufacturers[int(tag)]
                step = STEP_MODEL

            elif step == STEP_MODEL:
                mfr_models = tuple(sorted(set(b.model for b in boards if b.manufacturer == selected_mfr)))
                tag = await self.menu(
                    choices=[(str(i), n) for i, n in enumerate(mfr_models)],
                    message="Select model",
                    cancel_label="Back",
                    home_label="Main menu",
                )
                if tag == HOME_TAG:
                    return None
                if tag is None:
                    step = STEP_MFR
                    continue
                selected_model = mfr_models[int(tag)]
                step = STEP_VARIANT

            elif step == STEP_VARIANT:
                board_variants = _board_variants()
                if len(board_variants) > 1:
                    tag = await self.menu(
                        choices=[(str(i), n) for i, n in enumerate(board_variants)],
                        message="Select variant",
                        cancel_label="Back",
                        home_label="Main menu",
                    )
                    if tag == HOME_TAG:
                        return None
                    if tag is None:
                        step = STEP_MODEL
                        continue
                    selected_variant = board_variants[int(tag)]
                else:
                    selected_variant = board_variants[0]
                board = bdb.get_board(selected_mfr, selected_model, selected_variant)
                step = STEP_INFO

            elif step == STEP_INFO:
                # Midpoint: the board summary doubles as a lookup. Flash
                # continues to interface selection / confirmation; Back
                # returns to the variant (or model, when there was no
                # variant choice to make); Main menu leaves the wizard.
                tag = await self.button_menu(
                    choices=[("flash", "Flash")],
                    message=board.pretty(),
                    title="Board Info",
                    cancel_label="Back",
                    home_label="Main menu",
                )
                if tag == HOME_TAG:
                    return None
                if tag == "flash":
                    step = STEP_INTERFACE
                else:
                    step = STEP_VARIANT if len(_board_variants()) > 1 else STEP_MODEL
                continue

            elif step == STEP_INTERFACE:
                ifaces = board.interfaces if hasattr(board, "interfaces") else []
                ifaces = sorted(ifaces, key=lambda i: i.if_type)
                if len(ifaces) > 1:
                    tag = await self.menu(
                        choices=[(str(i), if_.pretty()) for i, if_ in enumerate(ifaces)],
                        message="This board supports multiple interfaces. Which interface do you want to use?",
                        cancel_label="Back",
                        home_label="Main menu",
                    )
                    if tag == HOME_TAG:
                        return None
                    if tag is None:
                        step = STEP_INFO
                        continue
                    chosen_if_type = ifaces[int(tag)].if_type
                else:
                    chosen_if_type = ifaces[0].if_type if ifaces else None

                step = STEP_CONFIRM

            elif step == STEP_CONFIRM:
                # Show the full board summary (including its interface
                # list) plus the interface just chosen, and let the user
                # either proceed or go back and pick again before the
                # caller commits to a session / starts compiling anything.
                # Rendered as buttons (not a scrollable list) since it's
                # just two fixed, equally-weighted choices; no separate
                # Cancel button — Escape still backs all the way out.
                iface_label = chosen_if_type or "(none available)"
                speed_line = ""
                if chosen_if_type and chosen_if_type in ("CAN", "USB to CAN Bridge"):
                    speed_line = f"\nCAN bit rate: {InterfaceSettings().can_speed} bps"
                elif chosen_if_type and chosen_if_type == "UART":
                    speed_line = f"\nUART baud rate: {InterfaceSettings().uart_baudrate} baud"
                summary = f"{board.pretty()}\nSelected interface: {iface_label}{speed_line}\n"
                tag = await self.button_menu(
                    choices=[("begin", "Confirm and start flash")],
                    message=summary,
                    title="Confirm Board & Interface",
                    cancel_label="Back",
                    home_label="Main menu",
                )
                if tag == HOME_TAG:
                    return None
                if tag == "begin":
                    # Host CAN networking check runs only now, after the
                    # user has committed — not while merely browsing a
                    # board's info or picking an interface.
                    if chosen_if_type in ("CAN", "USB to CAN Bridge"):
                        can_settings = InterfaceSettings()
                        problems = await asyncio.to_thread(check_can_host_state, can_settings.can_speed)
                        if problems:
                            problem_list = "\n".join(f"  - {p}" for p in problems)
                            fix_now = await self.yesno(
                                f"Found issues with the host's CAN networking setup:\n\n{problem_list}\n\n"
                                "Fix these now? (sudo password may be required)\n\n"
                                "You can still flash without fixing these — useful if this board "
                                "belongs to a different CAN network/printer than the host is "
                                "currently configured for.",
                                yes_label="Fix",
                                no_label="Flash anyway",
                                default_no=True,
                            )
                            if fix_now:
                                try:
                                    with self.suspend():
                                        print("\nApplying host CAN networking changes — you may be prompted for your sudo password below.\n")
                                        fix_can_host_state(can_settings.can_speed)
                                    await self.msgbox("Host CAN networking has been updated.", title="CAN host state")
                                except Exception as e:
                                    await self.msgbox(f"Failed to fix host CAN networking:\n\n{e}", title="CAN host state")
                    return (board, chosen_if_type)
                else:
                    # Escape/Back (tag is None): step back exactly one
                    # level — to interface reselection if this board
                    # actually has a choice to make there, else to the
                    # board info screen, since a single-interface board
                    # never showed an interface screen to return to.
                    step = STEP_INTERFACE if len(ifaces) > 1 else STEP_INFO
                    continue

    # ---- settings menu --------------------------------------------------

    async def edit_int_setting(self, title: str, current_value: int) -> int:
        result = await self.inputbox(
            f"Enter a custom value for the {title} (default: {current_value})",
            initial=str(current_value),
        )
        if result is None:
            return current_value
        try:
            new_value = int(result)
            if new_value < 0:
                await self.msgbox(f"Value must be non-negative (got {new_value}).\nUsing previous value.")
                return current_value
            return new_value
        except ValueError:
            await self.msgbox(f"Invalid input '{result}'. Must be a positive integer.\nUsing previous value.")
            return current_value

    async def choose_rate(self, title: str, current_value: int, options: list[tuple[str, int]]) -> int:
        preset_values = [v for _, v in options]
        while True:
            choices = [(str(v), f"{'*' if v == current_value else ' '} {label}") for label, v in options]
            choices.append(("__custom__", f"{'*' if current_value not in preset_values else ' '} Enter a custom value"))
            tag = await self.menu(choices=choices, message=title)
            if tag is None:
                return current_value
            if tag == "__custom__":
                new_value = await self.edit_int_setting(title, current_value)
                if new_value != current_value:
                    return new_value
                continue
            return int(tag)

    async def settings_menu(self) -> None:
        settings = InterfaceSettings()
        while True:
            choices = [
                ("can_speed", f"CAN bit rate: {settings.can_speed} bps"),
                ("uart_baudrate", f"UART baud rate: {settings.uart_baudrate} baud"),
            ]
            try:
                can_list = await asyncio.to_thread(detect_mod.list_can_interfaces)
            except Exception:
                can_list = []
            if len(can_list) > 1:
                choices.append(("can_interface", f"CAN interface: {settings.can_interface}"))
            choices.append(("back", "Back to main menu"))

            tag = await self.menu(choices=choices, title="Communication Settings", cancel_label="Back")

            if tag == "can_speed":
                new_value = await self.choose_rate(
                    "CAN bit rate selector (default 1M bps)",
                    settings.can_speed,
                    [("1M bps", 1000000), ("500K bps", 500000)],
                )
                if new_value != settings.can_speed:
                    try:
                        settings.can_speed = new_value
                        settings.save()
                        await self.msgbox(f"CAN Speed updated to {settings.can_speed} bps.")
                        # Offer to update the host's CAN networking config to match the new speed.
                        sync_now = await self.yesno(
                            f"Update the host's system CAN networking config to match "
                            f"({settings.can_speed} bps)? (sudo password may be required)",
                            yes_label="Update",
                            no_label="Not now",
                            default_no=True,
                        )
                        if sync_now:
                            try:
                                with self.suspend():
                                    print("\nApplying host CAN networking changes — you may be prompted for your sudo password below.\n")
                                    fix_can_host_state(settings.can_speed)
                                await self.msgbox("Host CAN networking has been updated.", title="CAN host state")
                            except Exception as e:
                                await self.msgbox(f"Failed to update host CAN networking:\n\n{e}", title="CAN host state")
                    except ValueError as e:
                        await self.msgbox(str(e))
            elif tag == "uart_baudrate":
                new_value = await self.choose_rate(
                    "UART baud rate selector (default 250K)",
                    settings.uart_baudrate,
                    [("250K baud", 250000), ("230.4K baud", 230400), ("115.2K baud", 115200)],
                )
                if new_value != settings.uart_baudrate:
                    try:
                        settings.uart_baudrate = new_value
                        settings.save()
                        await self.msgbox(f"UART Baud Rate updated to {settings.uart_baudrate} baud.")
                    except ValueError as e:
                        await self.msgbox(str(e))
            elif tag == "can_interface":
                try:
                    can_list = await asyncio.to_thread(detect_mod.list_can_interfaces)
                except Exception:
                    can_list = []
                if not can_list:
                    await self.msgbox("No CAN interfaces detected on host.")
                    continue
                tag2 = await self.menu(
                    choices=[(str(i), n) for i, n in enumerate(can_list)],
                    message="Select CAN interface to use:",
                )
                if tag2 is None:
                    continue
                chosen = can_list[int(tag2)]
                try:
                    settings.can_interface = chosen
                    settings.save()
                    await self.msgbox(f"CAN interface set to {chosen}")
                except Exception as e:
                    await self.msgbox(str(e))
            elif tag in ("back", None):
                return

    # ---- CAN host prep menu --------------------------------------------

    async def prepare_can_host_menu(self) -> None:
        settings = InterfaceSettings()
        problems = await asyncio.to_thread(check_can_host_state, settings.can_speed)
        if not problems:
            await self.msgbox(
                f"Host CAN networking looks correctly configured for {settings.can_speed} bps.",
                title="CAN host state",
            )
            return
        problem_list = "\n".join(f"  - {p}" for p in problems)
        proceed = await self.yesno(
            f"Found issues with the host's CAN networking setup:\n\n{problem_list}\n\n"
            "Fix these now? (sudo password may be required)",
            yes_label="Fix",
            no_label="Cancel",
            default_no=True,
        )
        if not proceed:
            return
        try:
            with self.suspend():
                print("\nApplying host CAN networking changes — you may be prompted for your sudo password below.\n")
                fix_can_host_state(settings.can_speed)
            await self.msgbox("Host CAN networking has been updated.", title="CAN host state")
        except Exception as e:
            await self.msgbox(f"Failed to fix host CAN networking:\n\n{e}", title="CAN host state")

    # ---- UART host configuration menu --------------------------------

    async def configure_uart_host_menu(self) -> None:
        problems = await asyncio.to_thread(check_uart_host_state)
        if not problems:
            await self.msgbox(
                "Host looks correctly configured for hardware UART.",
                title="UART host state",
            )
            return
        problem_list = "\n".join(f"  - {p}" for p in problems)
        proceed = await self.yesno(
            f"Found issues with the host's UART setup:\n\n{problem_list}\n\n"
            "Fix what's fixable now? (sudo password may be required)",
            yes_label="Fix",
            no_label="Cancel",
            default_no=True,
        )
        if not proceed:
            return
        try:
            with self.suspend():
                print("\nApplying host UART changes — you may be prompted for your sudo password below.\n")
                skipped, reboot_required = fix_uart_host_state()
            msg = "Host UART configuration has been updated."
            if skipped:
                msg += "\n\nNot checked/fixed (unsupported host):\n" + "\n".join(f"  - {s}" for s in skipped)
            await self.msgbox(msg, title="UART host state")
            if reboot_required:
                do_reboot = await self.yesno(
                    "A reboot is required for these changes to take effect. Reboot now?",
                    yes_label="Reboot",
                    no_label="Later",
                    default_no=True,
                )
                if do_reboot:
                    with self.suspend():
                        print("\nRebooting — you may be prompted for your sudo password below.\n")
                        reboot_host()
        except Exception as e:
            await self.msgbox(f"Failed to fix host UART configuration:\n\n{e}", title="UART host state")

    # ---- top-level menu loop ------------------------------------------

    async def menus(self) -> None:
        while True:
            tag = await self.menu(
                choices=[
                    ("flash", "Select a board"),
                    ("settings", "Edit the default communication settings"),
                    ("preparecan", "Prepare host OS for CAN"),
                    ("configureuart", "Configure host for hardware UART"),
                    ("crash", "Intentionally crash"),
                    ("exit", "Exit"),
                ],
                title="Main Menu",
                message="What would you like to do?",
                cancel_label="Quit",
            )
            if tag is None or tag == "exit":
                self.exit()
                return
            if tag == "settings":
                await self.settings_menu()
            elif tag == "preparecan":
                await self.prepare_can_host_menu()
            elif tag == "configureuart":
                await self.configure_uart_host_menu()
            elif tag == "flash":
                try:
                    await self.start_flash_flow()
                except Exception as e:
                    await self.msgbox(str(e), title="Error")
            elif tag == "crash":
                raise RuntimeError("Intentional Crash")

    async def launch(self) -> None:
        try:
            # WIP warning and board-database load/warnings both happen
            # BEFORE the resume-session check now — previously they were
            # queued to run only after start_flash_flow(resume_session=...)
            # returned, so choosing "Resume previous flash" meant these
            # warnings didn't show until the resumed flash had already
            # finished (or failed), which looked like they'd appeared at
            # the wrong time entirely.
            self._clear_startup_loading()
            proceed = await self.yesno(
                "This is WIP tooling.\n\nIt may eat your cat and/or firmware\nBe warned!",
                default_no=True,
                yes_label="Risk It",
                no_label="Quit",
            )
            if not proceed:
                self.exit()
                return

            try:
                import importlib
                m = importlib.import_module("klipper_ezflash.model")
                importlib.reload(m)
                self._board_db = m.BoardDatabase()
                warnings = self._board_db.get_load_warnings()
                if warnings:
                    max_show = 25
                    msg = "Issues detected while loading user_boards.json:\n\n"
                    msg += "\n".join(warnings[:max_show])
                    if len(warnings) > max_show:
                        msg += f"\n\n...and {len(warnings) - max_show} more"
                    proceed = await self.yesno(
                        msg + "\n\nDo you want to continue anyway?",
                        yes_label="Continue",
                        no_label="Quit",
                        default_no=False,
                    )
                    if not proceed:
                        self.exit()
                        return
                    try:
                        self._board_db.clear_load_warnings()
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                existing = load_session()
            except Exception:
                existing = None
            if existing is not None:
                resume_message = "A previous flash session was detected.\n\nResume or start a new flash?"
                # step_index 4-5 = bootloader flashed, firmware detection
                # not yet done — the window where a board needing a
                # physical interface swap would have been interrupted (e.g.
                # by a full host shutdown to move a cable). Echo the same
                # post_bootloader text shown mid-flash, since a user coming
                # back after a real power-off may not remember it.
                if existing.step_index in (4, 5):
                    try:
                        board = next(
                            (b for b in (self._board_db.get_all() if self._board_db else [])
                             if b.manufacturer == existing.manufacturer
                             and b.model == existing.model
                             and b.variant == existing.variant),
                            None,
                        )
                    except Exception:
                        board = None
                    if board is not None and board.instructions and board.instructions.get("post_bootloader"):
                        resume_message = (
                            "A previous flash session was detected, partway through the "
                            "bootloader-to-firmware transition.\n\n"
                            f"Reminder for this board:\n{board.instructions['post_bootloader']}\n\n"
                            "Resume or start a new flash?"
                        )
                tag = await self.menu(
                    choices=[("resume", "Resume previous flash"), ("new", "Start a new flash")],
                    message=resume_message,
                    cancel_label="Start new",
                )
                if tag == "resume":
                    try:
                        await self.start_flash_flow(resume_session=existing)
                    except Exception as e:
                        await self.msgbox(f"Failed to resume session: {e}")
                else:
                    # User declined to resume so clear the session so it doesn't keep showing up on next launch.
                    try:
                        clear_session()
                    except Exception:
                        pass

            await self.check_katapult()
            await self.menus()
        except Exception as e:
            await self.handle_doom(e)

    @staticmethod
    def describe_error(e: Exception) -> str:
        if type(e) is NotImplementedError:
            return (
                "The functionality you have tried to use has not yet been implemented\n"
                f"The responsible part of the codebase said: {e.args}"
            )
        return f"{type(e)}\n{e.args}\nNo additional details are available"

    async def handle_doom(self, e: Exception) -> None:
        try:
            human_text = self.describe_error(e)
        except Exception:
            human_text = "Unknown error"
        try:
            await self.msgbox(
                f"A fatal error has occurred in EZFlash\n\n{human_text}\n\nThe program will now exit",
                title="Fatal Error",
            )
        except Exception:
            traceback.print_exc()
        self.exit(return_code=1)


def main() -> None:
    app = EZFlashApp()
    # mouse=False: this app is fully keyboard-navigable (arrow keys, Enter,
    # mnemonic letters, type-ahead — see dialogs.py), so nothing is lost by
    # skipping mouse tracking entirely. The real win: without the mouse-
    # tracking-enable escape sequence, Terminal.app never leaves native
    # click-and-drag selection mode, so a plain drag + Cmd+C copies the
    # completion text directly — no Fn+drag workaround needed.
    app.run(mouse=False)
    if app._stopped_services:
        # Runs after Textual has fully torn down and the terminal is back
        # to normal (app.run() is synchronous and blocks until exit) —
        # sudo can prompt directly here without needing app.suspend().
        print(f"\nRestarting services: {', '.join(app._stopped_services)}")
        start_services(app._stopped_services)


if __name__ == "__main__":
    main()
