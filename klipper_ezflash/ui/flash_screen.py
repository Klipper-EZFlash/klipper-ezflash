"""
Flash flow screen — two-pane layout: steps on the left, live output right.

Replaces `_run_with_progressbox` + the `multiprocessing`(fork) trick
entirely. Command streaming uses `asyncio.create_subprocess_exec` and
`async for line in proc.stdout` — this runs on Textual's own asyncio event
loop, no `threading` import and no manual fork/pipe plumbing.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import logging
import shlex
import tempfile
import threading
from enum import Enum
from pathlib import Path
from typing import Optional

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, RichLog, Static

from .theme import _BG, _PANEL, _SURFACE, _CALLOUT, _TEXT, _TEXT_DIM, _ACCENT
from ..fault import EZFException


@contextlib.contextmanager
def _capture_output_to_log(screen: "FlashFlowScreen"):
    """Redirect stdout/stderr AND klipper_ezflash's own logger.debug()
    output into the flash screen's log for the duration of the block.

    Some synchronous library code we call into — notably kconfiglib, via
    Configurator/KConfig — prints warnings directly to stdout/stderr
    during parsing rather than raising or logging through the `logging`
    module. Since that's a direct write to the real terminal, it bypasses
    Textual's rendering entirely and corrupts the screen. This can't be
    streamed live (the call is synchronous, not a subprocess we're
    reading from), but capturing and flushing it into the log after the
    fact at least keeps it inside the app instead of on the raw
    terminal, and it's still visible to the user.

    Separately, Configurator._log_selection() logs every kconfig
    choice/symbol it sets (arch, MCU, interface, etc.) via
    logger.debug() on the 'klipper_ezflash' logger tree — normally
    invisible here, since __main__.py's basicConfig sets the root logger
    to INFO. For the duration of this block only, this attaches a
    handler directly to the 'klipper_ezflash' logger and drops its level
    to DEBUG, so that config-building detail lands in this same
    transcript (on-screen and in log_path) right before the compile
    steps that follow it, instead of being silently discarded.
    """
    buf = io.StringIO()

    class _ScreenLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                screen.write(self.format(record))
            except Exception:
                pass

    pkg_logger = logging.getLogger("klipper_ezflash")
    handler = _ScreenLogHandler(level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    prev_level = pkg_logger.level
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.DEBUG)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield
    finally:
        pkg_logger.removeHandler(handler)
        pkg_logger.setLevel(prev_level)
        captured = buf.getvalue()
        if captured.strip():
            for line in captured.splitlines():
                screen.write(line)


def _summarize_failure(argv: list[str], rc: int, output_lines: list[str]) -> str:
    """Build a human-readable failure message for a failed command,
    surfacing the actual error content instead of just the bare exit code.

    Without this, a compile failure (e.g. a linker "region 'rom'
    overflowed" error) surfaced only as "'make' failed (exit 2)" -- no
    hint of *why*, leaving the retry dialog's only options as an identical
    retry (which fails again for the same reason) or abort, with no way
    to diagnose the problem from inside the app.

    Prefers lines that look like the actual error (linker/compiler
    errors), falling back to the last few lines of output if nothing
    recognizable is found.
    """
    header = f"'{argv[0]}' failed (exit {rc})"
    markers = ("overflowed", "error:", "Error:", "ERROR", "undefined reference", "No such file")
    interesting = [line for line in output_lines if any(m in line for m in markers)]
    tail = interesting[-6:] if interesting else output_lines[-10:]
    if not tail:
        return header
    return header + "\n\n" + "\n".join(tail)


class StepStatus(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    ERROR = "error"


_STATUS_GLYPH = {
    StepStatus.PENDING: ("○", f"{_TEXT_DIM}"),
    StepStatus.ACTIVE: ("▶", "bold #f5c518"),
    StepStatus.DONE: ("✔", f"bold {_ACCENT}"),
    StepStatus.ERROR: ("✘", "bold #ef5350"),
}


class StepList(Static):
    """Left-hand pane: ordered list of flash steps with live status."""

    def __init__(self, steps: list[str]) -> None:
        super().__init__()
        self._steps = steps
        self._status = [StepStatus.PENDING] * len(steps)

    def set_status(self, index: int, status: StepStatus) -> None:
        self._status[index] = status
        self.refresh()

    def status(self, index: int) -> StepStatus:
        return self._status[index]

    def __len__(self) -> int:
        return len(self._steps)

    def render(self) -> str:
        lines = []
        for i, name in enumerate(self._steps):
            glyph, style = _STATUS_GLYPH[self._status[i]]
            lines.append(f"[{style}]{glyph}  {name}[/]")
        return "\n".join(lines)


class CompletionScreen(Screen[None]):
    """Full-screen view for the final flash summary — has a Header for
    visual consistency with FlashFlowScreen, but deliberately no Footer,
    step-list pane, or CSS border (unlike the modal dialogs in
    dialogs.py). On macOS Terminal.app, getting native terminal text
    selection at all inside a Textual app requires holding Fn while
    dragging (Option is iTerm2's equivalent), and even then a
    drag-selection captures every visible character, border glyphs
    included. Keeping this screen down to just Header + the completion
    text (plus a one-line hint) means a Fn-drag selection of the body
    captures exactly the completion message and nothing else — no footer
    keybinding hints, no border-right from a step-list pane, no
    box-drawing dialog border.

    When `config_snippet` is given, it's rendered in its own zero-
    horizontal-padding Static rather than folded into the same padded
    block as the surrounding prose — leaving the intro/outro at their
    original padding would mean every line of the copyable [mcu] section
    starts several blank columns in from the left edge, which a
    drag-select easily includes by accident. That's not just cosmetic:
    printer.cfg's config format treats a leading-whitespace line as a
    continuation of the previous line's value, not a new key, so pasting
    an indented `[mcu]`/`serial:` line can silently break parsing. A
    bordered popup would reintroduce the exact problem the Fn-drag fix
    above solved, so this stays on the same borderless screen — just
    split into separate widgets so the snippet's own box has no left
    padding to accidentally select along with it.

    The intro/outro Statics also carry zero horizontal padding (matching
    the snippet), rather than the screen-wide padding this used to have
    — an indented snippet sitting flush against non-indented prose read
    as a layout bug, not an intentional callout. With everything flush
    left, the snippet's distinct `_CALLOUT` background (a dedicated
    token, more contrasty against `_BG` than `_SURFACE` — see theme.py —
    since `_SURFACE` alone was too close to `_BG` to read as a
    deliberate card rather than a rendering glitch) is what marks it as
    a callout, not indentation. No border on it either, same reasoning
    as above.
    """

    BINDINGS = [
        Binding("enter,escape", "dismiss_screen", "Continue", show=False),
    ]

    CSS = f"""
    CompletionScreen {{
        background: {_BG};
        color: {_TEXT};
    }}
    CompletionScreen #completion-intro {{
        padding: 2 0 0 0;
    }}
    CompletionScreen #completion-snippet {{
        padding: 1 0;
        margin: 0;
        background: {_CALLOUT};
    }}
    CompletionScreen #completion-outro {{
        padding: 0 0 2 0;
    }}
    """

    def __init__(self, text: str, config_snippet: Optional[str] = None) -> None:
        super().__init__()
        self._text = text
        self._config_snippet = config_snippet

    def compose(self) -> ComposeResult:
        # Header only — no Footer (its keybinding hints were exactly the
        # kind of extra text that got swept into a drag-selection once
        # the modal border was removed) and no step-list pane. Header
        # itself is just a solid title bar with no border glyphs, so it's
        # safe to bring back for visual consistency with FlashFlowScreen
        # without reintroducing the copy/paste problem this screen exists
        # to avoid.
        yield Header(icon="")
        # markup=False on every Static below: the completion text contains
        # literal square brackets ("[mcu]" in the printer.cfg snippet)
        # which, under Static's default markup=True, Rich's markup parser
        # reads as an opening style tag rather than literal text —
        # confirmed by testing: it silently consumes the entire "[mcu]"
        # line as an (invalid) style span, which is why that line went
        # missing. Resolving "mcu" as an actual Style object at render
        # time (rather than the lenient plain-print path) is also the
        # likely cause of the theme dropping out / slow-to-render symptoms
        # seen alongside it — an internal render error/recovery, not
        # something wrong with the CSS itself.
        if self._config_snippet:
            # partition() on the raw snippet text (not a reconstructed
            # slice) so this stays correct regardless of exactly how much
            # blank-line spacing flash_flow.py puts around it — the
            # blank lines themselves are kept in `before`/`after` (not
            # stripped) since blank lines have no visible content to
            # garble and dropping them would collapse the intentional
            # spacing around the block.
            before, _, after = self._text.partition(self._config_snippet)
            yield Static(before, id="completion-intro", markup=False)
            yield Static(self._config_snippet, id="completion-snippet", markup=False)
            yield Static(after + "\n\nPress Enter to continue...", id="completion-outro", markup=False)
        else:
            yield Static(f"{self._text}\n\nPress Enter to continue...", id="completion-intro", markup=False)

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


class FlashFlowScreen(Screen):
    """Persistent screen for the whole flash flow: steps left, output right."""

    BINDINGS = [
        Binding("q", "request_quit", "Quit", show=True),
        Binding("ctrl+c", "request_quit", "Force Quit", show=False),
    ]

    CSS = f"""
    FlashFlowScreen {{
        background: {_BG};
    }}
    FlashFlowScreen #body {{
        height: 1fr;
    }}
    FlashFlowScreen #steps {{
        /* percentage width so this pane scales with the terminal instead of
           staying a fixed cell count; clamped so it stays usable on very
           narrow or very wide terminals. */
        width: 30%;
        min-width: 24;
        max-width: 56;
        height: 100%;
        border-right: solid {_ACCENT};
        background: {_PANEL};
        color: {_TEXT};
        padding: 2 3;
    }}
    FlashFlowScreen #output {{
        /* 1fr = "take whatever width is left after #steps" — this is what
           actually makes the right pane grow/shrink with the terminal. */
        width: 1fr;
        height: 100%;
    }}
    FlashFlowScreen RichLog {{
        /* Bounding the height (instead of leaving it "auto", which would
           just grow to fit all content forever) is what makes this an
           actual scrollable region — RichLog's own auto_scroll then keeps
           new lines pinned in view rather than there being nothing to
           scroll within. */
        height: 2fr;
        background: {_SURFACE};
        color: {_TEXT};
    }}
    """

    def __init__(self, step_names: list[str]) -> None:
        super().__init__()
        self.step_list = StepList(step_names)
        # Set by action_request_quit() so blocking waits running in worker
        # threads (asyncio.to_thread) can bail out immediately. A thread
        # can't be cancelled from outside, and the default executor is
        # joined at shutdown, so without this, quitting mid-wait leaves the
        # shell prompt blocked until that wait's own timeout expires.
        self.cancel_event = threading.Event()
        # NOTE: can't call this `self.log` — Textual's Widget/DOMNode base
        # class already defines a read-only `log` property (the per-widget
        # logger, e.g. `self.log.info(...)`), so assigning to it raises
        # "property 'log' has no setter".
        self.output_log = RichLog(
            highlight=False, markup=False, wrap=True, max_lines=5000, auto_scroll=True
        )
        # Every line passed to write() is mirrored to this file immediately,
        # regardless of whether the on-screen log gets cleared between
        # steps — this is what makes the full transcript recoverable after
        # the fact (success, error, or abort) even though the visible
        # RichLog only ever shows the current step.
        self.log_path = Path(tempfile.gettempdir()) / f"ezflash-log-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            self._log_file = self.log_path.open("a", encoding="utf-8")
        except OSError:
            self._log_file = None

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        with Horizontal(id="body"):
            with VerticalScroll(id="steps"):
                yield self.step_list
            with Vertical(id="output"):
                yield self.output_log
        yield Footer()

    def action_request_quit(self) -> None:
        # No confirmation here on purpose: push_screen_wait requires an
        # active Textual worker (key-binding actions run directly on the
        # message pump, not inside one — that mismatch is exactly what
        # raised NoActiveWorker and crashed this on a real attempt to
        # abort mid-compile). The flash/compile steps are fast enough
        # that a confirmation isn't worth the risk of crashing the exit
        # path itself — just quit immediately.
        self.cancel_event.set()
        self.app.exit()

    def on_unmount(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass

    def set_step(self, index: int, status: StepStatus) -> None:
        self.step_list.set_status(index, status)

    def start_step(self, index: int, header: Optional[str] = None) -> None:
        """Begin a new top-level flash-flow step: mark it ACTIVE, clear the
        on-screen log, and write a banner into it.

        The on-screen RichLog only shows the current step — cleared here on
        every transition. Nothing is actually lost: write() mirrors every
        line to self.log_path as it's produced, so the complete transcript
        for the whole flow is always recoverable from disk regardless of
        whether the flow finishes, errors, or is aborted.
        """
        self.set_step(index, StepStatus.ACTIVE)
        self.output_log.clear()
        if 0 <= index < len(self.step_list._steps):
            step_name = self.step_list._steps[index]
        else:
            step_name = f"Step {index}"
        self.write(f"\n{'─' * 8} {step_name} {'─' * 8}")
        if header:
            self.write(header)

    def write(self, line: str) -> None:
        self.output_log.write(line)
        # Belt-and-suspenders: auto_scroll=True already does this, but if
        # the user has manually scrolled up (which pauses auto-scroll)
        # we still want each new line during an active flash to pull the
        # view back down rather than silently piling up out of sight.
        self.output_log.scroll_end(animate=False)
        if self._log_file is not None:
            try:
                self._log_file.write(line + "\n")
                self._log_file.flush()
            except OSError:
                pass

    async def _stream_subprocess(
        self, argv: list[str], cwd: Optional[Path] = None, capture: Optional[list[str]] = None
    ) -> int:
        """Run argv, streaming stdout+stderr into the log.

        Reads raw chunks (not line-by-line) and splits on both `\\r` and
        `\\n` ourselves. This matters for tools like dfu-util and make
        that redraw an in-place progress line via bare `\\r` with no
        real newline in between ticks — `async for line in proc.stdout`
        only splits on `\\n`, so an entire burst of `\\r`-separated
        percentage updates arrives as one lump the moment a real newline
        finally shows up, which is what rendered as everything jammed
        onto one visual line. Here, intermediate `\\r`-terminated
        updates are held and only the final one before the next real
        line (or end of stream) is actually written — collapsing a
        rapid progress burst into one tidy line instead of dumping every
        tick.

        If `capture` is given, every line actually written is also
        appended to it (e.g. for success-pattern checks that need the
        full text afterward).
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        buf = ""
        pending_progress: Optional[str] = None

        def flush_progress() -> None:
            nonlocal pending_progress
            if pending_progress is not None:
                self.write(pending_progress)
                if capture is not None:
                    capture.append(pending_progress)
                pending_progress = None

        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            while True:
                cut_n = buf.find("\n")
                cut_r = buf.find("\r")
                if cut_n == -1 and cut_r == -1:
                    break
                if cut_r != -1 and (cut_n == -1 or cut_r < cut_n):
                    segment, buf = buf[:cut_r], buf[cut_r + 1:]
                    if segment:
                        pending_progress = segment
                else:
                    segment, buf = buf[:cut_n], buf[cut_n + 1:]
                    if segment.endswith("\r"):
                        segment = segment[:-1]
                    flush_progress()
                    if segment:
                        self.write(segment)
                        if capture is not None:
                            capture.append(segment)
        flush_progress()
        if buf:
            self.write(buf)
            if capture is not None:
                capture.append(buf)
        return await proc.wait()

    async def run_command(self, *args: str, cwd: Optional[Path] = None) -> int:
        """Run a subprocess, streaming stdout/stderr into the log.

        Uses asyncio's native subprocess support, so output is pumped on
        the existing event loop as it's produced — no `threading` import,
        no polling loop, no fork.
        """
        self.write(f"$ {' '.join(shlex.quote(a) for a in args)}")
        return await self._stream_subprocess(list(args), cwd=cwd)

    async def run_make(self, title: str, cwd: Path, *make_args: str) -> None:
        """Run `make` in cwd, raising EZFException on non-zero exit."""
        self.write(f"\n[ {title} ]")
        argv = ["make", *make_args]
        self.write(f"$ {' '.join(shlex.quote(a) for a in argv)}")
        output_lines: list[str] = []
        rc = await self._stream_subprocess(argv, cwd=cwd, capture=output_lines)
        if rc != 0:
            raise EZFException(_summarize_failure(argv, rc, output_lines) + f" in {cwd}")

    async def run_argv(self, title: str, argv: list[str], cwd: Optional[Path] = None) -> None:
        """Run an arbitrary argv (e.g. from flash.py's command builders),
        raising EZFException on non-zero exit.

        Captures output (like run_argv_check_patterns already does) so a
        failure's exception message can include the actual error content
        -- e.g. a linker "region overflowed" line -- rather than just the
        bare exit code, which otherwise leaves a retry dialog with nothing
        actionable to go on.
        """
        self.write(f"\n[ {title} ]")
        self.write(f"$ {' '.join(shlex.quote(a) for a in argv)}")
        output_lines: list[str] = []
        rc = await self._stream_subprocess(argv, cwd=cwd, capture=output_lines)
        if rc != 0:
            raise EZFException(_summarize_failure(argv, rc, output_lines))

    async def run_argv_check_patterns(self, title: str, argv: list[str], success_patterns: list[str], cwd: Optional[Path] = None) -> None:
        """Run an argv where a non-zero exit can still mean success (e.g.
        dfu-util), by checking buffered output against `success_patterns`.
        Mirrors flash.py's `_run_with_success_check`, but streaming into the
        log as it runs rather than only at the end.
        """
        self.write(f"\n[ {title} ]")
        self.write(f"$ {' '.join(shlex.quote(a) for a in argv)}")
        output_lines: list[str] = []
        rc = await self._stream_subprocess(argv, cwd=cwd, capture=output_lines)
        if rc == 0:
            return
        output = "\n".join(output_lines)
        if any(pattern in output for pattern in success_patterns):
            return
        raise EZFException(_summarize_failure(argv, rc, output_lines))
