from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .fault import EZFException, AmbiguousCanUuidException
from .util import find_tool, find_katapult, find_klippy_env_python, run_captured

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


def detect_usb_serial(predicate: Callable[[Path], bool]) -> List[Path]:
    """Scan /dev/serial/by-id for entries matching `predicate`."""
    base = Path("/dev/serial/by-id")
    if not base.exists():
        return []
    return [p for p in base.iterdir() if predicate(p)]


def extract_serial(device_path: Path) -> Optional[str]:
    """Extract the unique device serial from a /dev/serial/by-id entry.

    e.g. usb-katapult_rp2040_E6611CB697123A29-if00 -> "E6611CB697123A29"
    Works the same for Klipper_ entries, since the serial sits in the same
    position regardless of the katapult_/Klipper_ prefix or chip name.

    Not used to *match* devices across the katapult -> klipper transition
    (a board's klipper config may set a custom USB serial number that
    differs from what katapult reports) — kept around for display/logging
    purposes, e.g. showing the user which board's serial was detected.
    """
    match = re.search(r"_([0-9A-Fa-f]+)-if\d+$", device_path.name)
    return match.group(1) if match else None


class _DirectoryChangeWaiter(FileSystemEventHandler):
    """Wakes on any create/delete/move event in the watched directory.
    Used purely as a signal to re-poll — the actual answer always comes
    from re-checking the directory directly, never from this handler's
    own state, so a missed inotify event costs one extra wait cycle
    instead of a false "not found" result.
    """
    def __init__(self):
        self.changed = threading.Event()

    def on_created(self, event):
        self.changed.set()

    def on_deleted(self, event):
        self.changed.set()

    def on_moved(self, event):
        self.changed.set()


def _wait_until(
    condition_fn: Callable[[], bool],
    timeout: float,
    cancel: Optional[threading.Event] = None,
) -> bool:
    """Block until `condition_fn()` returns True, or `timeout` elapses.
    Re-checks immediately, then again on every /dev/serial/by-id change
    (via inotify) or timeout, always by calling `condition_fn()` directly
    rather than trusting the watcher's own bookkeeping.

    If `cancel` is given and gets set (the user quit), raises
    EZFException("Cancelled") within ~0.25s. The inotify wait is sliced
    into short waits because a thread can only block on one Event at a
    time (the watcher's own).
    """
    base = Path("/dev/serial/by-id")
    if condition_fn():
        return True

    deadline = time.monotonic() + timeout

    if not base.exists():
        while time.monotonic() < deadline and not base.exists():
            if cancel is not None and cancel.is_set():
                raise EZFException("Cancelled")
            time.sleep(0.05)
        if not base.exists():
            return False

    handler = _DirectoryChangeWaiter()
    observer = Observer()
    observer.schedule(handler, str(base), recursive=False)
    observer.start()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise EZFException("Cancelled")
            if condition_fn():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return condition_fn()
            handler.changed.wait(min(remaining, 0.25) if cancel is not None else remaining)
            handler.changed.clear()
    finally:
        observer.stop()
        observer.join()


def wait_for_serial_device(
    predicate: Callable[[Path], bool],
    timeout: float = 5.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[Path]:
    """Block until exactly one /dev/serial/by-id entry matching
    `predicate` appears (or already exists), or until `timeout` elapses.
    Returns the matching Path, or None on timeout.

    Raises EZFException if more than one matching device is present at
    once — the same "can't safely pick one" reasoning as device.py's
    bootloader-mode detection (verify_bootloader_connected): silently
    taking the first match risked acting on the wrong board whenever two
    matching devices were connected simultaneously.
    """
    result: dict = {}

    def condition():
        match = detect_usb_serial(predicate)
        if len(match) > 1:
            raw_list = "\n".join(f"  - {p}" for p in match)
            raise EZFException(
                f"Multiple matching serial devices detected at once:\n{raw_list}\n\n"
                "Remove the extra device(s) so only the board you want "
                "remains connected, then retry."
            )
        if match:
            result["value"] = match[0]
            return True
        return False

    return result.get("value") if _wait_until(condition, timeout=timeout, cancel=cancel) else None


def wait_for_new_serial_device(
    predicate: Callable[[Path], bool],
    known: Iterable[Path],
    timeout: float = 5.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[Path]:
    """Wait for a device matching `predicate` that is NOT already present
    in `known` (a snapshot taken before the flash was triggered).

    Use this instead of matching by serial number whenever the firmware may
    assign the device a different USB serial than the bootloader did (e.g.
    a board with a custom CONFIG_USB_SERIAL_NUMBER set in its klipper
    config) — only cares that the device is newly-appeared, not what its
    identity turns out to be.
    """
    known_set = set(known)
    return wait_for_serial_device(
        lambda p: predicate(p) and p not in known_set,
        timeout=timeout,
        cancel=cancel,
    )


def wait_for_katapult_device(
    known: Optional[Iterable[Path]] = None,
    timeout: float = 5.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[Path]:
    """Wait for the board to re-enumerate into katapult mode after
    flashing the bootloader.

    If `known` is given (a snapshot of katapult_ devices taken before the
    bootloader flash), waits for a device that wasn't in that snapshot —
    this is what actually matters when another board's katapult device
    is already sitting connected from an earlier session: without this,
    that leftover device and the newly-flashed one both match the same
    predicate and get reported as an ambiguous "multiple devices"
    situation, even though only the new one is relevant to this flash. If
    omitted, matches any katapult_ device (only safe when at most one
    could plausibly be connected at a time).
    """
    predicate = lambda p: "katapult_" in p.name
    if known is not None:
        return wait_for_new_serial_device(predicate, known, timeout=timeout, cancel=cancel)
    return wait_for_serial_device(predicate, timeout=timeout, cancel=cancel)


def wait_for_klipper_device(
    known: Optional[Iterable[Path]] = None,
    timeout: float = 5.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[Path]:
    """Wait for a Klipper USB serial device.

    If `known` is given (a snapshot of Klipper_ devices taken before the
    flash), waits for a device that wasn't in that snapshot — safe even if
    the firmware assigns a different serial than the bootloader had. If
    omitted, matches any Klipper_ device (only safe when at most one
    Klipper device could plausibly be connected at a time).
    """
    predicate = lambda p: "Klipper_" in p.name
    if known is not None:
        return wait_for_new_serial_device(predicate, known, timeout=timeout, cancel=cancel)
    return wait_for_serial_device(predicate, timeout=timeout, cancel=cancel)

def list_serial_ports() -> List[Path]:
    """Return every serial device currently present, for the user to
    pick from manually.

    Used for UART interfaces: the connection there goes through a
    generic USB-serial adapter (FTDI, CH340, etc.) wired to the board's
    UART pins, which has no board-specific "katapult_"/"Klipper_" name
    to auto-match against the way a board's own native USB port does —
    there's no way to tell which serial adapter is the right one without
    asking.

    Prefers /dev/serial/by-path/ over /dev/serial/by-id/ as the primary
    source: by-id names are derived from the device's own reported
    vendor/product/serial, which collide when two identical adapters
    share the same (often blank) serial number — udev then only creates
    one by-id symlink for both, silently hiding the second device from
    this list entirely. by-path names are derived from USB bus/port
    topology instead, which is inherently unique per physical connection
    point regardless of what the device itself reports (see
    describe_serial_port() for a friendlier display label built from
    by-id when it's actually unique).

    by-path itself can have its own duplication problem, just a different
    one: some USB controller/kernel combinations (seen on Pi-style SoC
    xHCI controllers) generate two by-path aliases for the exact same
    physical port — one from a legacy-style enumeration path
    ("...usb-0:..."), one from the newer xHCI-derived naming
    ("...usbv2-0:...") — both resolving to the same real device. These
    are deduplicated by resolved target below, keeping one entry per
    unique underlying device rather than surfacing every alias as if it
    were a separate port.

    Also always includes the SBC's own hardware UART device(s)
    (/dev/ttyS*, /dev/ttyAMA*) alongside whatever USB-adapter ports were
    found — e.g. a Raspberry Pi or BTT CB1/CB2's GPIO-header UART, once
    the user has configured it themselves (the dtoverlay/armbianEnv.txt
    setup needed to actually enable it is a separate, platform-specific
    concern this function doesn't attempt; it only needs to be
    selectable here once it exists as a device). Both ttyS* (generic
    8250/16550-style naming) and ttyAMA* (ARM PL011 naming, what a
    Raspberry Pi's GPIO UART is typically called once Bluetooth is freed
    from it) are included since which name applies depends on the
    platform/config. These are appended after the USB-adapter list
    rather than being an either/or fallback, since both by-path and
    by-id only ever contain USB-connected devices — a hardware UART
    would otherwise be invisible any time a USB adapter was also plugged
    in.
    """
    def _dedup_by_target(paths: List[Path]) -> List[Path]:
        seen_targets: dict = {}
        for p in paths:
            try:
                real = p.resolve()
            except OSError:
                real = p
            seen_targets.setdefault(real, p)
        return sorted(seen_targets.values())

    usb_ports: List[Path] = []
    by_path = Path("/dev/serial/by-path")
    if by_path.exists():
        usb_ports = _dedup_by_target(sorted(by_path.iterdir()))
    if not usb_ports:
        by_id = Path("/dev/serial/by-id")
        if by_id.exists():
            usb_ports = _dedup_by_target(sorted(by_id.iterdir()))
    if not usb_ports:
        dev = Path("/dev")
        if dev.exists():
            usb_ports = sorted(
                p for p in dev.glob("tty*") if p.name.startswith(("ttyUSB", "ttyACM"))
            )

    hw_uart_ports: List[Path] = []
    dev = Path("/dev")
    if dev.exists():
        hw_uart_ports = sorted(
            p for p in dev.glob("tty*")
            if p.name.startswith(("ttyS", "ttyAMA")) and p.is_char_device()
        )

    return usb_ports + hw_uart_ports


def describe_serial_port(port: Path) -> str:
    """Build a human-friendly label for a serial port entry, for display
    in a selection menu.

    list_serial_ports() prefers /dev/serial/by-path/ for uniqueness (see
    its docstring), but those names are long, topology-derived strings
    that aren't very readable on their own (e.g.
    "platform-3f980000.usb-usb-0:1.2:1.0-port0"). If a corresponding
    /dev/serial/by-id/ entry exists AND it's the only one resolving to
    this same underlying device, use that instead — it's far more
    readable (e.g. "usb-FTDI_FT232R_USB_UART-if00-port0") and only
    unreliable in exactly the collision case list_serial_ports() already
    routes around by preferring by-path in the first place.
    """
    try:
        real = port.resolve()
    except OSError:
        return str(port)
    by_id_dir = Path("/dev/serial/by-id")
    if by_id_dir.exists():
        matches = []
        try:
            for item in by_id_dir.iterdir():
                try:
                    if item.resolve() == real:
                        matches.append(item)
                except OSError:
                    continue
        except OSError:
            pass
        if len(matches) == 1:
            return f"{matches[0].name} ({real})"
    return str(port)


def probe_katapult_status(
    device: Path,
    firmware_bin: Path,
    baud: Optional[int] = None,
    katapult_path: Optional[Path] = None,
    timeout: float = 10.0,
) -> bool:
    """Probe a serial device to see if a Katapult bootloader is actually
    listening there, using katapult's own flashtool.py in --status mode
    (connect and report status, no flashing) — confirmed from Katapult's
    actual scripts/flashtool.py source: -s/--status runs the same
    connect_btl() path as an actual flash (proper frame/header/trailer/CRC
    validation, 5 retries at 2s each by default), just exits before
    send_file().

    `baud` MUST match whatever rate the actual flash will use (session's
    configured UART baud rate) — flashtool.py falls back to its own
    default otherwise, which silently talks at the wrong speed and
    produces a false "no response" on every candidate port regardless of
    which one is actually correct, indistinguishable from a genuinely
    absent device.

    `firmware_bin` is passed as flashtool's required --firmware argument
    even though status mode never reads it (BaseSocket._check_firmware()
    only validates it for an actual flash request) — using the real
    firmware path already being flashed is simplest, no placeholder
    needed.

    Only meaningful for a board expected to be sitting in Katapult's
    bootloader steady-state (e.g. right after its bootloader was just
    flashed, application area still empty, per Katapult's own "auto-enter
    bootloader if application area is empty" behavior) — NOT for catching
    a brief post-reset bootloader window on an already-running board,
    which needs a different approach entirely (flooding CONNECT frames
    against a narrow timing window, not implemented here since it's not
    this function's use case).

    Returns True if flashtool.py reports a successful connection, False
    on any failure (wrong device, nothing listening, timeout, katapult
    checkout not found, etc.) — never raises, since this is meant to be
    tried across several candidate ports where a failure on any one of
    them is an entirely expected, unremarkable outcome, not an error
    condition.
    """
    kp = Path(katapult_path) if katapult_path else None
    if kp is None:
        try:
            kp = find_katapult()
        except Exception:
            kp = None
    if kp is None:
        return False
    flashtool = kp / "scripts" / "flashtool.py"
    if not flashtool.exists():
        return False
    py = os.environ.get("KBOARD_KLIPPY_PYTHON") or sys.executable or "/usr/bin/python3"
    cmd = [py, str(flashtool), "-d", str(device), "-f", str(firmware_bin)]
    if baud:
        cmd.extend(["-b", str(baud)])
    cmd.append("-s")
    try:
        out = run_captured(cmd, timeout=timeout)
    except Exception:
        return False
    return "Katapult Connected" in out


def detect_can_uuid(katapult_path: Optional[Path] = None, interface: str = "can0", timeout: int = 10) -> List[Tuple[str, str]]:
    """Detect device UUIDs on a CAN interface using katapult's `flashtool.py`.

    Parses lines like ``Detected UUID: 66f03f24c715, Application: Katapult``.
    Returns a list of ``(uuid, firmware_type)`` tuples where *firmware_type* is
    one of: ``"katapult"``, ``"klipper"``, or ``"unknown"``.

    Firmware names are matched case-insensitively and ``CanBoot`` is treated as
    equivalent to ``Katapult``.
    """
    kp = Path(katapult_path) if katapult_path else None
    if kp is None:
        try:
            kp = find_katapult()
        except Exception:
            kp = None

    if kp is None:
        raise EZFException("Could not locate katapult checkout for CAN UUID detection")

    flashtool = kp / "scripts" / "flashtool.py"
    if not flashtool.exists():
        raise EZFException(f"flashtool.py not found at {flashtool}")

    py = os.environ.get("KBOARD_KLIPPY_PYTHON") or sys.executable or "/usr/bin/python3"

    # Regex to parse lines like "Detected UUID: 66f03f24c715, Application: Katapult"
    detected_uuid_re = re.compile(
        r"Detected\s+UUID:\s*([0-9a-fA-F]+)(?:,\s*Application:\s*(\w+))?",
        re.IGNORECASE,
    )

    found: List[Tuple[str, str]] = []  # list of (uuid, firmware_type)
    seen_uuids: set[str] = set()
    
    cmd = [py, str(flashtool), "-i", interface, "-q"]
    out = run_captured(cmd, timeout=timeout)

    for m in detected_uuid_re.finditer(out):
        uuid_val = m.group(1).lower()
        app_name = (m.group(2) or "").lower() if m.lastindex >= 2 else ""
        fw_type = _resolve_firmware_type(app_name)

        if uuid_val not in seen_uuids:
            seen_uuids.add(uuid_val)
            found.append((uuid_val, fw_type))

    if not found:
        raise EZFException(f"No CAN UUIDs detected with command: {' '.join(cmd)}\nOutput:\n{out}")

    return found


def _resolve_firmware_type(text: str) -> str:
    """Resolve a firmware type from lowercase text.

    Matches *katapult* or *canboot* (case-insensitive) → ``"katapult"``,
    *klipper* → ``"klipper"``, anything else → ``"unknown"``.
    """
    if "katapult" in text or "canboot" in text:
        return "katapult"
    if "klipper" in text:
        return "klipper"
    return "unknown"


def list_can_interfaces() -> List[str]:
    """Return a list of candidate CAN network interface names present on the host.

    This implementation checks /sys/class/net for names starting with 'can' or 'vcan'.
    """
    netdir = Path("/sys/class/net")
    if not netdir.exists():
        return []
    out = []
    for p in netdir.iterdir():
        name = p.name
        if name.startswith("can") or name.startswith("vcan") or name.startswith("slcan"):
            out.append(name)
    return sorted(out)


def wait_for_new_can_interface(
    known: Iterable[str],
    timeout: float = 15.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[str]:
    """Poll for a CAN network interface that is not already present in
    `known` (a snapshot taken before the flash was triggered).

    Used to identify a CAN-bridge board's own interface once it reboots
    into USB to CAN Bridge-mode firmware and enumerates as a new USB-CAN adapter
    — there's no serial-device equivalent to match against here, since
    the board no longer exposes a USB-serial device at all in this mode.

    Unlike wait_for_serial_device, this polls list_can_interfaces()
    directly on a short interval rather than watching via inotify —
    /sys/class/net doesn't get the same treatment as /dev/serial/by-id
    elsewhere in this file, and a new CAN interface appearing is a
    slower, rarer event where a plain poll loop is simple and sufficient.

    Raises EZFException if more than one new interface appears at once,
    for the same reason as wait_for_serial_device: silently picking one
    risks acting on the wrong device.
    """
    known_set = set(known)
    deadline = time.monotonic() + timeout
    sleeper = cancel if cancel is not None else threading.Event()
    while True:
        if cancel is not None and cancel.is_set():
            raise EZFException("Cancelled")
        current = list_can_interfaces()
        new = [c for c in current if c not in known_set]
        if len(new) > 1:
            raw_list = "\n".join(f"  - {c}" for c in new)
            raise EZFException(
                f"Multiple new CAN interfaces appeared at once:\n{raw_list}\n\n"
                "Remove any other newly-connected CAN-bridge devices so only "
                "the board you're flashing is present, then retry."
            )
        if new:
            return new[0]
        if time.monotonic() >= deadline:
            return None
        sleeper.wait(0.5)


def wait_for_can_uuid(
    interface: str,
    want_type: str = "katapult",
    katapult_path: Optional[Path] = None,
    timeout: float = 30.0,
    query_timeout: float = 8.0,
    cancel: Optional[threading.Event] = None,
) -> Optional[Tuple[str, str]]:
    """Poll a CAN interface for a device reporting the given firmware type
    ("katapult" or "klipper"), ignoring any other-typed entries entirely.

    This is what stops a board of the wrong type — an already-in-service
    klipper-mode board still sharing the same physical CAN bus while
    waiting for a bootloader, or vice versa, a device that hasn't
    finished rebooting into its newly-flashed firmware yet while waiting
    to confirm the flash — from ever being mistaken for the device
    actually being waited on right now. detect_can_uuid() itself returns
    every UUID it sees regardless of firmware type, so a caller that just
    took the first result could end up targeting/confirming the wrong
    device or the wrong lifecycle stage of the right device.

    Each individual flashtool.py query gets `query_timeout` seconds to
    actually scan the bus and respond — CAN node discovery isn't
    instant. An earlier version of this function tied the per-query
    timeout to a short poll interval (2s), which meant flashtool.py was
    being killed before it could ever report a UUID even though the
    device was genuinely there and a longer manual query found it fine.
    `timeout` is the overall budget across however many such queries fit
    within it; there's no separate sleep between attempts since each
    attempt's own runtime already paces the loop.

    Returns None on timeout (the caller should report "not found", not
    silently fall back to a wrong-typed entry). Raises EZFException if
    more than one matching-typed device is present at once — the same
    "can't safely pick one" reasoning as wait_for_serial_device.
    """
    deadline = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise EZFException("Cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            found = detect_can_uuid(
                katapult_path=katapult_path, interface=interface,
                timeout=max(1, int(min(query_timeout, remaining))),
            )
        except EZFException:
            found = []
        matching = [(u, fw) for u, fw in found if fw == want_type]
        if len(matching) > 1:
            raw_list = "\n".join(f"  - {u}" for u, _ in matching)
            raise AmbiguousCanUuidException(
                f"Multiple {want_type}-mode CAN devices detected at once:\n{raw_list}\n\n"
                "Remove the extra device(s) so only the board you want is present, then retry.",
                candidates=matching,
            )
        if matching:
            return matching[0]


def identify_can_device(
    uuid: str,
    interface: str,
    klipper_path: Path,
    timeout: float = 10.0,
) -> dict:
    """Connect to a CAN device (native CAN or CANBRIDGE-mode) using
    klippy's own host-side connection code, and return its reported MCU
    config constants (e.g. MCU, CLOCK_FREQ) as a dict.

    Used to disambiguate multiple klipper-mode CAN devices found at once
    (see wait_for_can_uuid's ambiguity error) by comparing each one's
    reported MCU/clock against the board actually being flashed —
    boards.json's mcu.mcu field is documented as matching Klipper's own
    MCU constant format exactly (see boards/readme.md), so this is a
    direct string comparison, not a fuzzy one.

    Connecting to a CAN device assigns it a runtime node ID via the CAN
    admin protocol's SET_NODEID command, which is why it stops responding
    to canbus_query.py-style scans afterward — there is no "release"
    command in that protocol (confirmed against Klipper's own
    CANBUS_protocol.md). To avoid leaving the device in that claimed
    state, this sends Klipper's own "reset" MCU command (falling back to
    "config_reset") right after reading the constants, over the same
    connection — EXCEPT for a device reporting CANBUS_BRIDGE, which is
    skipped entirely: klippy's own mcu.py deliberately refuses to restart
    bridge MCUs through the normal path (`_firmware_restart` returns
    immediately if `self._is_mcu_bridge and not force`), because a bridge
    MCU *is* the can0 interface itself — resetting it doesn't just drop
    one node, it disables the whole USB-CAN adapter (per Klipper's own
    CANBUS docs: "Whenever the bridge mcu is reset, Linux will disable
    the corresponding can0 interface"), which would disrupt every other
    device sharing that bus, not just the one being probed.

    This bypasses mcu.py's CommandWrapper/MCU wrapper classes entirely
    (they're tightly coupled to a full MCUConnectHelper we don't have
    here) and instead replicates what CommandWrapper.send() actually does
    underneath, directly against the bare SerialReader: look up the
    command via msgparser.lookup_command(name), encode it, and hand the
    bytes to serial.raw_send() with serial.get_default_command_queue() —
    both confirmed as real SerialReader methods (not MCU-only) from
    klippy's actual mcu.py source. This is best-effort: a failure to
    find/send the reset command is swallowed, since it should never mask
    the identify data already successfully read.

    Runs as a subprocess under klippy-env's own Python with the connect
    logic passed inline via `-c`, rather than importing klippy's reactor
    directly into this process: klippy's reactor is a full event-loop
    implementation of its own, and running it inside this app's existing
    Textual/asyncio event loop risks real deadlocks, not just a style
    clash. It also depends on a compiled C extension built specifically
    for klippy-env, and its own tooling (e.g. console.py) is
    written/tested against that environment, not this app's own venv.

    Raises EZFException if klippy-env can't be found, the subprocess
    fails, or it doesn't report anything within `timeout` seconds.
    """
    klippy_python = find_klippy_env_python()
    if not klippy_python:
        raise EZFException(
            "Could not find klippy-env's Python interpreter "
            "(expected ~/klippy-env/bin/python)"
        )

    klippy_dir = str(Path(klipper_path) / "klippy")
    marker = "KBOARD_IDENTIFY_JSON:"
    inline_script = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {klippy_dir!r})\n"
        "import reactor, serialhdl\n"
        "r = reactor.Reactor()\n"
        "def _connect(eventtime):\n"
        "    try:\n"
        "        ser = serialhdl.SerialReader(r)\n"
        f"        ser.connect_canbus({uuid!r}, 64, {interface!r})\n"
        "        constants = ser.get_msgparser().get_constants()\n"
        "        if constants.get('CANBUS_BRIDGE'):\n"
        "            reset_status = 'skipped (bridge device — resetting it would disrupt the whole CAN interface for other devices, same as klippy itself does)'\n"
        "        else:\n"
        "            reset_status = 'no reset/config_reset command found'\n"
        "            try:\n"
        "                msgparser = ser.get_msgparser()\n"
        "                cmd_fmt = None\n"
        "                for name in ('reset', 'config_reset'):\n"
        "                    try:\n"
        "                        cmd_fmt = msgparser.lookup_command(name)\n"
        "                        break\n"
        "                    except Exception:\n"
        "                        continue\n"
        "                if cmd_fmt is not None:\n"
        "                    cq = ser.get_default_command_queue()\n"
        "                    ser.raw_send(cmd_fmt.encode(()), 0, 0, cq)\n"
        "                    reset_status = 'sent'\n"
        "            except Exception as e:\n"
        "                reset_status = 'failed: ' + repr(e)\n"
        "        constants['_reset_status'] = reset_status\n"
        f"        sys.stdout.write({marker!r} + json.dumps(constants) + chr(10))\n"
        "        sys.stdout.flush()\n"
        "    finally:\n"
        "        os._exit(0)\n"
        "r.register_callback(_connect)\n"
        "r.run()\n"
    )

    try:
        result = subprocess.run(
            [str(klippy_python), "-c", inline_script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise EZFException(f"Timed out identifying CAN device {uuid} on {interface} after {timeout}s")

    for line in result.stdout.splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError as e:
                raise EZFException(f"Could not parse identify response for {uuid}: {e}")

    raise EZFException(
        f"Could not identify CAN device {uuid} on {interface}.\n\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )

