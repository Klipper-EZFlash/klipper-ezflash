import re
import logging
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Optional, Iterable
from enum import Enum

from .kconfig import KConfig, KConfigChoice
from .model import BoardDefinition, BoardInterfaceDefinition
from .util import table_munge

logger = logging.getLogger(__name__)
FREQ_IN_RE = re.compile("([0-9]+)([MK]hz)", flags=re.IGNORECASE)

_ARCH_MUNGES = (
    ("rp2040", "rp235x", "Raspberry Pi RP2040", "Raspberry Pi RP2040/RP235x"),
)


_COMMS_MUNGES = (
    "Communication interface",
    "Communications interface",
    "Communication Interface",
    "Communications Interface",
)

# Every symbol under Klipper's "Optional features (to reduce code size)"
# menu in src/Kconfig, as of the current upstream source. These all
# default to enabled ('y') regardless of chip size -- the menu only makes
# them *visible* for toggling on small-flash chips (HAVE_LIMITED_CODE_SIZE),
# it doesn't disable anything by itself. See Configurator.set_optional_features().
#
# This is a snapshot of Klipper's Kconfig and can drift if upstream
# renames/adds/removes symbols -- set_optional_features() validates against
# it so a stale/misspelled boards.json entry fails loudly at configure time
# instead of silently building with defaults.
OPTIONAL_FEATURE_SYMBOLS = (
    "WANT_ADC",
    "WANT_SPI",
    "WANT_SOFTWARE_SPI",
    "WANT_I2C",
    "WANT_SOFTWARE_I2C",
    "WANT_HARD_PWM",
    "WANT_BUTTONS",
    "WANT_TMCUART",
    "WANT_NEOPIXEL",
    "WANT_PULSE_COUNTER",
    "WANT_ST7920",
    "WANT_HD44780",
    "WANT_ADXL345",
    "WANT_LIS2DW",
    "WANT_BMI160",
    "WANT_MPU9250",
    "WANT_ICM20948",
    "WANT_THERMOCOUPLE",
    "WANT_HX71X",
    "WANT_ADS131M0X",
    "WANT_ADS1220",
    "WANT_LDC1612",
    "WANT_SENSOR_ANGLE",
    "WANT_TRIGGER_ANALOG",
)


class ConfiguratorTarget(Enum):
    KLIPPER = 1
    KATAPULT = 2


class Configurator(object):
    def __init__(self, source_path: PathLike, board: BoardDefinition, target: Optional[ConfiguratorTarget] = None):
        self._target: Optional[ConfiguratorTarget] = target
        self.source_path = source_path
        self.kconfig = KConfig(source_path)
        self._board = board
        self._active_interface: Optional[BoardInterfaceDefinition] = None
        # Enable extra low-level options (required for many configs to work)
        if extra_config := self.kconfig.symbol(
            prompt="Enable extra low-level configuration options"
        ):
            extra_config.set(True)
        self._load_from_board(board)

    def _log_selection(self, label: str, value) -> None:
        """Log the kconfig choice/symbol that was set, including the board and target for context.
        """
        target_name = self._target.name if self._target else "?"
        logger.debug(f"[{self._board}/{target_name}] {label}: {value}")

    @classmethod
    def katapult(cls, source_path: PathLike, board: BoardDefinition) -> "Configurator":
        return cls(source_path, board, target=ConfiguratorTarget.KATAPULT)

    @classmethod
    def klipper(cls, source_path: PathLike, board: BoardDefinition) -> "Configurator":
        return cls(source_path, board, target=ConfiguratorTarget.KLIPPER)

    def _load_from_board(self, board):
        logger.debug(
            f"[{board}/{self._target.name if self._target else '?'}] "
            f"Building config: arch={board.mcu.arch} mcu={board.mcu.mcu}"
        )
        self.set_arch(board.mcu.arch)
        self.set_mcu(board.mcu.mcu)
        if clock := board.mcu.clock:
            self.set_freq(clock)
        # Don't set flash type when there's a bootloader (no "stage 2" firmware)
        if flash := board.mcu.flash:
            self.set_flash(flash)
        if self._target == ConfiguratorTarget.KATAPULT:
            if board.status is not None:
                self.set_status_leds(board.status)
            if board.bootloader_pins is not None:
                self.set_bootloader_pins(board.bootloader_pins)
        if self._target == ConfiguratorTarget.KLIPPER:
            if board.startup_pins is not None:
                self.set_startup_pins(board.startup_pins)
            if "rp2040" in board.mcu.arch.lower():
                self.set_bootloader_offset()
            if board.optional_features is not None:
                self.set_optional_features(board.optional_features)

    def set_status_leds(self, gpio_spec: str):
        if self._target != ConfiguratorTarget.KATAPULT:
            raise ValueError("Status LEDs are only supported for Katapult")
        self.kconfig.symbol(prompt="Enable Status LED").set(True)
        self.kconfig.symbol(prompt="Status LED GPIO Pin").set(gpio_spec)
        self._log_selection("Status LED GPIO Pin", gpio_spec)

    def set_startup_pins(self, gpio_spec: str):
        if self._target != ConfiguratorTarget.KLIPPER:
            raise ValueError("Startup pins are only supported for Klipper")
        self.kconfig.symbol(prompt="GPIO pins to set at micro-controller startup").set(gpio_spec)
        self._log_selection("Startup pins", gpio_spec)

    def set_bootloader_pins(self, gpio_spec: str):
        if self._target != ConfiguratorTarget.KATAPULT:
            raise ValueError("Bootloader pins are only supported for Katapult")
        sym = self.kconfig.symbol(prompt="GPIO pins to set on bootloader entry")
        if sym is None:
            raise RuntimeError(
                f"{self._board} needs bootloader pins ({gpio_spec}), but this Katapult "
                "checkout has no 'GPIO pins to set on bootloader entry' option. "
                "Update Katapult and try again."
            )
        sym.set(gpio_spec)
        self._log_selection("Bootloader pins", gpio_spec)

    def set_arch(self, arch):
        # Despite what the below says, we have to deal with differing titles for some arches
        arch = table_munge(arch, _ARCH_MUNGES)
        # Arch is pretty easy to deal with, since it is always the same prompt, but the choice is unnamed
        self.kconfig.choice(prompt="Micro-controller Architecture").select(prompt=arch)
        self._log_selection("Micro-controller Architecture", arch)

    def set_mcu(self, mcu):
        if proc_choice := self.kconfig.choice(prompt="Processor model"):
            for possible_mcu in proc_choice.choices():
                mcu_prompt = possible_mcu.nodes[0].prompt[0].split(" ")[0]
                if mcu_prompt.upper() == mcu.upper():
                    self._log_selection("Processor model", mcu)
                    possible_mcu.set_value(2)
                    return
            raise RuntimeError(f"Unable to find matching MCU {mcu}")
        else:
            # This arch doesn't support setting the MCU
            if not mcu:
                # We don't care
                return
            # If the kconfig sets an 'MCU', and it is already correct, don't mess with it
            if mcu_sym := self.kconfig.symbol(name="MCU", allow_invisible=True):
                if mcu_sym.get() == mcu:
                    self._log_selection("MCU (static)", mcu)
                    return
            raise RuntimeError(
                f"Could not set MCU type to {mcu}. Is it supported by this version of klipper?"
            )

    def set_freq(self, freq):
        freq_choice = None
        for choice in self.kconfig.choices:
            if choice.prompt in ["Processor Speed", "Clock Reference"]:
                freq_choice = choice
                break
        if not freq_choice and freq:
            raise ValueError("Frequency cannot be set for this MCU")
        # Special case "INTERNAL"
        if freq == "INTERNAL":
            freq_choice.select(prompt="Internal clock")
            self._log_selection(freq_choice.prompt, "Internal clock")
            return
        # Frequency should be in the form XXMhz or XXKhz
        if matches := FREQ_IN_RE.match(freq):
            rate, units = matches.groups()
        else:
            raise ValueError(f'Frequency "{freq}" not recognized')
        target_re = re.compile(
            f"^{re.escape(rate)} ?{re.escape(units)}", flags=re.IGNORECASE
        )
        for sym in freq_choice.choices():
            if target_re.match(sym.nodes[0].prompt[0]):
                self._log_selection(freq_choice.prompt, freq)
                sym.set_value(2)
                return
        raise ValueError(f"Could not set frequency to {freq}")

    def set_flash(self, flash):
        if flash_choice := self.kconfig.choice(prompt="Flash chip"):
            for possible_flash in flash_choice.choices():
                if possible_flash.nodes[0].prompt[0].lower().startswith(flash.lower()):
                    self._log_selection("Flash chip", flash)
                    possible_flash.set_value(2)
                    return
            raise ValueError(
                f"Could not select flash {flash}, is it supported by this version of klipper?"
            )
        raise RuntimeError("This MCU does not support setting the flash type")

    def set_bootloader_offset(self):
        """Select the flash offset reserved for Katapult on RP2040, so Klipper's
        firmware links after the bootloader rather than at offset 0.
        """
        offset_choice = self.kconfig.choice(
            prompt=("Bootloader offset", "Application start offset")
        )
        if not offset_choice:
            return
        for opt in offset_choice.choices():
            if "no bootloader" not in opt.nodes[0].prompt[0].lower():
                self._log_selection(offset_choice.prompt, opt.nodes[0].prompt[0])
                opt.set_value(2)
                return
        raise ValueError("Could not find a bootloader offset option to select")

    def set_optional_features(self, features: Iterable[str]):
        """Restrict Klipper's optional code-size-reducing features (the
        WANT_* symbols under "Optional features (to reduce code size)" in
        src/Kconfig) to exactly the given set, disabling every other known
        optional feature.

        These symbols all default to enabled ('y') in Klipper's own
        Kconfig regardless of chip size -- the "Optional features" menu
        only makes them visible/toggleable on small-flash chips, it
        doesn't disable anything by itself. Left alone, a small-flash MCU
        (e.g. STM32F042) tries to build with everything on and overflows
        its .text region. This turns every known optional feature off
        first, then re-enables exactly what the board declares it needs.

        `features` must be exact Klipper Kconfig symbol names (e.g.
        "WANT_NEOPIXEL"), not free-text tags -- validated against
        OPTIONAL_FEATURE_SYMBOLS so a typo or a symbol renamed by a
        Klipper update fails loudly at configure time instead of quietly
        building with defaults.
        """
        wanted = set(features)
        unknown = wanted - set(OPTIONAL_FEATURE_SYMBOLS)
        if unknown:
            raise ValueError(
                f"Unknown optional_features symbol(s) {sorted(unknown)} for {self._board}; "
                f"expected some subset of {OPTIONAL_FEATURE_SYMBOLS}"
            )
        self._log_selection("Optional features (WANT_*)", sorted(wanted))
        for name in OPTIONAL_FEATURE_SYMBOLS:
            sym = self.kconfig.symbol(name=name, allow_invisible=True)
            if sym is None:
                # Not a real symbol for this arch/build at all (e.g. gated
                # behind a HAVE_* this MCU doesn't select) -- nothing to
                # toggle, and not an error: OPTIONAL_FEATURE_SYMBOLS is a
                # global list across all architectures, not every arch has
                # every symbol.
                continue
            sym.set(name in wanted)

    def get_interfaces(self):
        return self._board.interfaces

    # TODO: add get_interface() to return the currently active interface

    def _get_comms_choice(self):
        return self.kconfig.choice(prompt=_COMMS_MUNGES)

    def _get_can_interface_choice(self):
        """Some MCU families expose CAN pin selection as their own separate
        top-level 'CAN bus interface' choice, independent of the
        'Communication interface' choice.

        Confirmed against Klipper's actual src/stm32/Kconfig (and
        src/atsamd/Kconfig, src/atsam/Kconfig follow the same pattern):
        this choice (prompt "CAN bus interface") is only visible `if
        USBCANBUS` — it exists specifically for boards in USB-to-CAN-
        bridge mode, since the bridge still needs a real physical CAN
        peripheral/pins underneath (the USB side is just how the host
        reaches it). Native CAN mode's pin options on these arches
        (STM32_MMENU_CANBUS_* etc.) live directly in the "Communication
        interface" choice instead — see _select_can_bus, which tries that
        first and only falls back to this for arches that don't work that
        way (RP2040, see _set_can_pins_by_symbol).

        Returns None if this MCU has no such choice.
        """
        try:
            return self.kconfig.choice(prompt="CAN bus interface")
        except KeyError:
            return None

    def _set_can_pins_by_symbol(self, interface: BoardInterfaceDefinition) -> bool:
        """RP2040-style: plain 'CAN RX/TX gpio number' int symbols, visible
        whenever CAN hardware is in use — native or bridge alike, confirmed
        against Klipper's actual src/rp2040/Kconfig:

            config RPXXXX_CANBUS_GPIO_RX
                int "CAN RX gpio number" if CANBUS
                default 4
            config RPXXXX_CANBUS_GPIO_TX
                int "CAN TX gpio number" if CANBUS
                default 5

        Unlike STM32, RP2040 has no separate 'CAN bus interface' choice at
        all — these symbols are the only mechanism, for both native CAN
        and USB-to-CAN-bridge modes.

        Returns True if set, False if these symbols don't exist/aren't
        visible on this MCU (caller should try another mechanism).
        """
        rx_sym = self.kconfig.symbol(prompt="CAN RX gpio number")
        tx_sym = self.kconfig.symbol(prompt="CAN TX gpio number")
        if not (rx_sym and tx_sym):
            return False
        rx_sym.set(interface.pins["rx"].removeprefix("gpio"))
        tx_sym.set(interface.pins["tx"].removeprefix("gpio"))
        self._log_selection(
            "CAN RX/TX gpio number", f"{interface.pins['rx']}/{interface.pins['tx']}"
        )
        return True

    def _set_can_interface_pins(self, interface: BoardInterfaceDefinition):
        """Set this interface's CAN pins by whichever mechanism this MCU's
        kconfig actually exposes, trying in order:
          1. A separate 'CAN bus interface' choice (STM32/ATSAM/ATSAMD
             bridge mode) — select its matching 'CAN bus (on RX/TX)' entry.
          2. Plain 'CAN RX/TX gpio number' int symbols (RP2040, both
             native and bridge modes).
        Raises RuntimeError if neither mechanism is available/matches —
        this should only happen for a comms choice that already encoded
        the pins directly in its own prompt (STM32 native CAN's fixed-pin
        variants), which _select_can_bus selects directly instead of
        calling this at all.
        """
        can_choice = self._get_can_interface_choice()
        if can_choice is not None:
            prompts = can_choice.prompts()
            rx, tx = interface.pins["rx"], interface.pins["tx"]
            target = f"CAN bus (on {rx}/{tx})"
            target_rev = f"CAN bus (on {tx}/{rx})"
            if target in prompts:
                can_choice.select(prompt=target)
                self._log_selection("CAN bus interface", target)
                return
            if target_rev in prompts:
                can_choice.select(prompt=target_rev)
                self._log_selection("CAN bus interface", target_rev)
                return
            raise RuntimeError(
                f"'CAN bus interface' choice exists but has no option matching pins {rx}/{tx}"
            )
        if self._set_can_pins_by_symbol(interface):
            return
        raise RuntimeError(
            f"{interface.if_type} comms specified, but no CAN pin configuration mechanism could be found"
        )

    def set_usb_default(self):
        """Select plain/default USB comms, with no board-specific pins.

        Fallback for boards that don't declare their own USB interface in
        boards.json (e.g. CAN-bridge-only boards like the Birds Nest CAN,
        which are never meant to be used as a USB-serial node) — Katapult
        still needs *some* comms mode to build for regardless of what
        interface the firmware itself ends up using, since the device must
        still be reachable as a normal USB DFU/bootloader target to
        receive the firmware flash in the first place. Raises ValueError
        (from KConfigChoice.select) only if this arch's kconfig genuinely
        has no USB comms option at all.
        """
        self._get_comms_choice().select(prompt=("USB", "USBSERIAL"))

    def set_canbridge(
        self,
        can_interface: BoardInterfaceDefinition,
        usb_interface: BoardInterfaceDefinition,
    ):
        """Select a CAN bridge interface, and set the CAN RX/TX pins for the given interface."""
        # Enable USBCANBUS first — makes "CAN bus interface" choice visible on STM32/ATSAMD
        # and RPXXXX_CANBUS_GPIO_RX/TX symbols visible on RP2040.
        usb_canbus = self.kconfig.symbol(prompt="USBCANBUS")
        if usb_canbus:
            usb_canbus.set(True)

        comms = self._get_comms_choice()

        # Find candidates matching "USB to CAN bus bridge" (with or without USB pin suffix).
        candidates = [
            c for c in comms.choices()
            if c.nodes[0].prompt[0].lower().startswith("usb to can bus bridge")
        ]
        if not candidates:
            raise ValueError(
                f"No 'USB to CAN bus bridge' option found in {comms.prompt}"
            )

        if len(candidates) == 1:
            candidates[0].set_value(2)
            self._log_selection(comms.prompt, candidates[0].nodes[0].prompt[0])
        else:
            # Multiple candidates — disambiguate using USB pin specs from boards.json.
            dm = usb_interface.pins.get("dm", "") if usb_interface else ""
            dp = usb_interface.pins.get("dp", "") if usb_interface else ""
            if not (dm and dp):
                raise ValueError(
                    "Multiple 'USB to CAN bus bridge' options found, but no USB pins "
                    "are available to disambiguate — this board needs a 'usb' entry in boards.json."
                )
            pin_re = re.compile(
                f"usb on ({re.escape(dm)}/{re.escape(dp)}|{re.escape(dp)}/{re.escape(dm)})",
                flags=re.IGNORECASE,
            )
            for choice in candidates:
                if pin_re.search(choice.nodes[0].prompt[0]):
                    choice.set_value(2)
                    self._log_selection(comms.prompt, choice.nodes[0].prompt[0])
                    break
            else:
                raise ValueError(
                    f"Could not find 'USB to CAN bus bridge' option matching USB pins {dm}/{dp}"
                )

        self._set_can_interface_pins(can_interface)

    def _select_can_bus(self, interface: BoardInterfaceDefinition):
        """Select CAN bus, preferring a fixed-pin comms choice entry if one
        exists (STM32/ATSAM/ATSAMD native CAN's actual mechanism — the
        selection itself IS the pin configuration, nothing more to set),
        falling back to a bare 'CAN bus' comms entry plus a separate
        pin-setting step otherwise (RP2040's mechanism).
        """
        comms = self._get_comms_choice()
        prompts = comms.prompts()
        rx, tx = interface.pins["rx"], interface.pins["tx"]
        fixed = f"CAN bus (on {rx}/{tx})"
        fixed_rev = f"CAN bus (on {tx}/{rx})"
        if fixed in prompts:
            comms.select(prompt=fixed)
            self._log_selection(comms.prompt, fixed)
            return
        if fixed_rev in prompts:
            comms.select(prompt=fixed_rev)
            self._log_selection(comms.prompt, fixed_rev)
            return
        comms.select(prompt="CAN bus")
        self._log_selection(comms.prompt, "CAN bus")
        self._set_can_interface_pins(interface)

    def set_interface(self, interface: BoardInterfaceDefinition):
        if_type = interface.if_type
        comms = self._get_comms_choice()

        if if_type == "USB":
            if not interface.pins:
                comms.select(prompt=("USB", "USBSERIAL"))
                self._log_selection(comms.prompt, "USB")
            else:
                usb_prompt = f"USB (on {interface.pins['dm']}/{interface.pins['dp']})"
                comms.select(prompt=usb_prompt)
                self._log_selection(comms.prompt, usb_prompt)
        elif if_type == "CAN":
            self._select_can_bus(interface)
        elif if_type == "USB to CAN Bridge":
            usb_interface = next(
                (i for i in self._board.interfaces if i.if_type == "USB"), None
            )
            self.set_canbridge(interface, usb_interface)
            self._active_interface = interface
            return
        elif if_type == "UART":
            pin_spec = (
                f"({interface.pins['rx'].upper()}/{interface.pins['tx'].upper()}|"
                f"{interface.pins['tx'].upper()}/{interface.pins['rx'].upper()})"
            )
            target_re = re.compile(
                f"^(Serial \\(?on )?US?ART[0-9]* (on )?{pin_spec}\\)?"
            )
            for possible_comms in comms.choices():
                if target_re.match(possible_comms.nodes[0].prompt[0]):
                    possible_comms.set_value(2)
                    self._log_selection(comms.prompt, possible_comms.nodes[0].prompt[0])
                    break
            else:
                raise ValueError(f"Serial not found {interface!r}")
        else:
            raise ValueError(f"Interface type {if_type} is not supported")
        self._active_interface = interface

    def set_interface_speed(self, speed):
        """Set the speed/baud rate of the currently configured interface:
        UART baud rate, or CAN bus bitrate — including USB to CAN Bridge, which
        still needs the compiled-in CAN bus speed symbol set to match the
        physical bus even though it also gets a host-side runtime bitrate
        via systemd-networkd (see check_can_host_state/fix_can_host_state
        in util.py). Raises if the currently configured interface is USB,
        which has no such setting.
        """
        iface = self._active_interface
        prompt = {
            "UART": "Baud rate for serial port",
            "CAN": "CAN bus speed",
            "USB to CAN Bridge": "CAN bus speed",
        }.get(iface.if_type)
        if not prompt:
            raise ValueError("Attempted to set interface speed on a non-CAN/USB to CAN Bridge/UART interface")
        self.kconfig.symbol(prompt=prompt).set(str(speed))
        self._log_selection(prompt, speed)

    def _header(self):
        return (
            "#\n"
            "#-# This file was generated by Klipper-EZFlash.\n"
            f"#-# This config is for: {self._board.manufacturer}/{self._board.variant}/{self._active_interface}\n"
            f"#-# This config was generated at {datetime.now(None).isoformat()}\n"
            "#-# THIS IS WIP SOFTWARE, AND IT MAY EAT YOUR CAT. BE CAREFUL. TRUST (or not) BUT VERIFY\n"
            "#\n"
        )

    def save_config(self, config_path: PathLike) -> Path:
        config_path = Path(config_path).expanduser()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        self.kconfig.kcl.write_config(
            str(config_path), header=self._header(), save_old=False
        )
        return config_path
