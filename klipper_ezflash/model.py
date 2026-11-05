import dataclasses
import json
import logging
import os
import re
from functools import cached_property, cache
from os import PathLike
from pathlib import Path
from typing import Union, Optional, Dict, TextIO, List

from jsonschema import Draft7Validator

from .util import get_boards

logger = logging.getLogger(__name__)


class BoardDatabase(object):
    def __init__(self, source: TextIO | PathLike | None = None):
        # Always load default boards first
        self._boards = list(BoardDefinition.read_from_stream(get_boards()))
        logger.info(f"Loaded {len(self._boards)} default board(s)")
        # Warnings collected while attempting to load/validate user or external boards
        self._load_warnings = []

        # Overlay user boards from XDG config if present
        user_path = _get_user_boards_path()
        if user_path is not None and user_path.exists():
            try:
                with open(user_path, "r") as f:
                    # validate and overlay; _overlay_from_stream handles validation warnings
                    self._overlay_from_stream(f)
                logger.info(f"Overlayed user boards from {user_path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load user boards from {user_path}: {e}")
                # Record a user-facing parse/load warning so the UI can show it
                self._load_warnings.append(f"Failed to load user_boards.json: {e}")

        if source is not None:
            # If an explicit source was passed, overlay it on top of defaults + user
            if isinstance(source, PathLike):
                try:
                    self._overlay_from_file(source)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Failed to load boards from {source}: {e}")
            elif hasattr(source, "read"):
                # typing.TextIO is not a base class of real file objects, so
                # isinstance() would never match; duck-type a stream instead.
                try:
                    self._overlay_from_stream(source)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse provided board source: {e}")
                    self._load_warnings.append(f"Failed to parse provided board source: {e}")

    def _overlay_from_data(self, json_data: dict, errors_list: Optional[list] = None):
        """Overlay boards from an already-parsed JSON dictionary.

        errors_list, if provided, will be appended with human-readable messages
        describing any variants that were skipped due to missing or malformed
        fields. The same messages are also stored in self._load_warnings.

        Defensive at every nesting level, not just the innermost from_data()
        call: the expected shape is exactly four levels deep (category ->
        manufacturer -> product -> variant -> field-dict, per
        boards/readme.md), and a common authoring mistake is collapsing
        product and variant into one level — which doesn't fail until deep
        inside from_data() as a raw 'string indices must be integers, not
        str'/AttributeError when a board's own field values (mcu, usb,
        etc.) get iterated as if they were variant names. Catching that
        structural mismatch explicitly, one level at a time, gives a
        precise "this level should be an object, got a string" message
        instead of a confusing crash with no indication of which file/path
        is actually wrong.
        """
        if not isinstance(json_data, dict):
            msg = f"user_boards.json root must be an object, got {type(json_data).__name__}"
            logger.warning(msg)
            if errors_list is not None:
                errors_list.append(msg)
            self._load_warnings.append(msg)
            return

        for category, manufacturers in json_data.items():
            if not isinstance(manufacturers, dict):
                msg = (
                    f"'{category}' should contain manufacturers (an object), got "
                    f"{type(manufacturers).__name__} — skipping this category entirely"
                )
                logger.warning(msg)
                if errors_list is not None:
                    errors_list.append(msg)
                self._load_warnings.append(msg)
                continue
            for manufacturer, products in manufacturers.items():
                if not isinstance(products, dict):
                    msg = (
                        f"'{category}/{manufacturer}' should contain products (an object), got "
                        f"{type(products).__name__} — skipping this manufacturer entirely"
                    )
                    logger.warning(msg)
                    if errors_list is not None:
                        errors_list.append(msg)
                    self._load_warnings.append(msg)
                    continue
                for product, variants in products.items():
                    if not isinstance(variants, dict):
                        msg = (
                            f"'{category}/{manufacturer}/{product}' should contain variants "
                            f"(an object), got {type(variants).__name__} instead — this usually means "
                            f"a variant level is missing (expected category/manufacturer/product/"
                            f"variant/fields, four levels deep); skipping this product entirely"
                        )
                        logger.warning(msg)
                        if errors_list is not None:
                            errors_list.append(msg)
                        self._load_warnings.append(msg)
                        continue
                    for variant, json_defn in variants.items():
                        if not isinstance(json_defn, dict):
                            msg = (
                                f"User board definition for {category}/{manufacturer}/{product}/{variant} "
                                f"should be an object of fields, got {type(json_defn).__name__} instead — "
                                f"this usually means a variant level is missing (expected category/"
                                f"manufacturer/product/variant/fields, four levels deep); skipping..."
                            )
                            logger.warning(msg)
                            if errors_list is not None:
                                errors_list.append(msg)
                            self._load_warnings.append(msg)
                            continue
                        try:
                            board = BoardDefinition.from_data(
                                manufacturer, product, variant, json_defn
                            )
                            self._replace_board(board)
                        except KeyError as e:
                            msg = (
                                f"User board definition for {category}/{manufacturer}/{product}/{variant} is missing {e.args[0]}, skipping..."
                            )
                            logger.warning(msg)
                            if errors_list is not None:
                                errors_list.append(msg)
                            self._load_warnings.append(msg)
                            continue
                        except (TypeError, AttributeError) as e:
                            # Same class of bug as the isinstance() checks
                            # above catch, but one level deeper — a field
                            # that looked like a dict at this level (e.g.
                            # "mcu") but has the wrong shape/keys inside
                            # it once from_data() actually indexes into it.
                            msg = (
                                f"User board definition for {category}/{manufacturer}/{product}/{variant} "
                                f"is malformed ({e}), skipping..."
                            )
                            logger.warning(msg)
                            if errors_list is not None:
                                errors_list.append(msg)
                            self._load_warnings.append(msg)
                            continue

    def _overlay_from_stream(self, stream: TextIO):
        """Overlay boards from a text stream on top of existing defaults.

        This function performs JSON Schema validation against the packaged
        v1.schema.json. Validation errors are logged and (when possible)
        shown to the user in a dialog, but loading continues — invalid
        entries are skipped by the existing overlay logic.
        """
        json_data = json.load(stream)

        # Load schema and validate; schema is shipped with the package
        schema_path = Path(__file__).parent / "boards" / "v1.schema.json"
        schema_errors = _validate_boards_json(json_data, schema_path)
        if schema_errors:
            for e in schema_errors:
                logger.warning(f"user_boards.json schema violation: {e}")
            # Do not append raw schema messages to _load_warnings here; they
            # will be transformed into user-friendly messages and stored after
            # collecting per-variant skipped messages so we can deduplicate.

        # Proceed to overlay — collect per-variant skipped messages so we can
        # present a concise summary to the user. Collect both schema errors and
        # per-variant skipped messages and show a single combined dialog so the
        # user is not prompted multiple times for the same underlying issue.
        skipped = []
        self._overlay_from_data(json_data, errors_list=skipped)

        # Combine schema and skipped messages into one deduplicated list for storage
        combined = []
        # Transform schema errors into per-variant missing-field messages when possible
        for s in schema_errors:
            try:
                path, msg = s.split(": ", 1)
            except ValueError:
                # Unexpected format, keep raw schema message
                combined.append(f"user_boards.json: {s}")
                continue
            path_parts = path.split("/")
            # If schema pinpoints a missing property in 'mcu', produce the same
            # human-friendly message used elsewhere and avoid double-reporting
            if path_parts and path_parts[-1] == "mcu" and "required property" in msg:
                # Extract the missing property's name from the message
                m = re.search(r"'([^']+)'", msg)
                if m:
                    field = m.group(1)
                    variant_path = "/".join(path_parts[:-1])
                    combined.append(f"User board definition for {variant_path} is missing {field}, skipping...")
                    continue
            # Fallback: include the schema message but tag it with the filename
            combined.append(f"user_boards.json: {s}")

        combined.extend(skipped)
        # Deduplicate while preserving order
        deduped = []
        for msg in combined:
            if msg not in deduped:
                deduped.append(msg)

        # Store deduplicated messages in the persistent load warnings list
        # Clear any previously appended raw schema messages
        self._load_warnings = deduped.copy()
        if deduped:
            logger.info("User boards load produced %d warning(s)", len(deduped))

    def _overlay_from_file(self, file: PathLike):
        with open(file, "r") as f:
            self._overlay_from_stream(f)

    def _replace_board(self, board):
        """Replace a board in the internal list if one with the same key exists."""
        for i, existing in enumerate(self._boards):
            if (
                existing.manufacturer == board.manufacturer
                and existing.model == board.model
                and existing.variant == board.variant
            ):
                self._boards[i] = board
                return
        # Board not found — add it as new
        self._boards.append(board)

    def get_board(self, manufacturer, model, variant):
        if mfr_matches := [x for x in self._boards if x.manufacturer == manufacturer]:
            if model_matches := [x for x in mfr_matches if x.model == model]:
                if variant_matches := [
                    x for x in model_matches if x.variant == variant
                ]:
                    if len(variant_matches) > 1:
                        raise ValueError("Duplicate board definition")
                    elif len(variant_matches) == 1:
                        return variant_matches[0]
        raise ValueError(f"No board data for {manufacturer}/{model}/{variant}")

    def get_all(self):
        return self._boards.copy()

    def get_load_warnings(self) -> list:
        """Return a copy of warnings collected during the last user/external load attempt."""
        return list(self._load_warnings)

    def clear_load_warnings(self) -> None:
        """Clear stored load warnings."""
        self._load_warnings.clear()


def _get_user_boards_path():
    """Return the path to user_boards.json in XDG config directory, or None."""
    xdg_config_home = os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config")
    )
    return Path(xdg_config_home) / "klipper-ezflash" / "user_boards.json"


def _validate_boards_json(data: dict, schema_path: Path) -> list:
    """Validate a parsed boards JSON dict against the packaged schema.

    Returns a list of human-readable error strings. An empty list means validation passed.
    """
    try:
        with open(schema_path, "r") as sf:
            schema = json.load(sf)
    except Exception as e:
        # If the schema can't be loaded for any reason, fail open: no schema errors.
        logger.warning(f"Unable to load boards schema from {schema_path}: {e}")
        return []

    validator = Draft7Validator(schema)
    errors = []
    for err in validator.iter_errors(data):
        # Build a path to the offending location to make messages actionable
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")
    return sorted(errors)


@dataclasses.dataclass
class BoardDefinition(object):
    manufacturer: str
    model: str
    variant: str
    mcu: "BoardMCUDefinition"
    can: Union["BoardCANDefinition", str, None] = None
    supports_canbridge: bool = False
    usb: Optional[str] = None
    uart: Optional["BoardUARTDefinition"] = None
    status: Optional[str] = None
    startup_pins: Optional[str] = None
    klipper_options: Optional[Dict[str, str]] = None
    optional_features: Optional[List[str]] = None
    instructions: Optional[Dict[str, str]] = None
    external_can_transceiver: bool = False

    def __str__(self):
        return f"{self.manufacturer}/{self.model}/{self.variant}"

    @classmethod
    def get_one_from_file(
        cls, file: PathLike, category: str, manufacturer: str, model: str, variant: str
    ) -> "BoardDefinition":
        all_data = json.load(Path(file).open())
        board_def_data = (
            all_data.get(category, {})
            .get(manufacturer, {})
            .get(model, {})
            .get(variant, None)
        )
        if not board_def_data:
            raise ValueError(
                f"No definition for {category}/{manufacturer}/{model}/{variant}"
            )
        return BoardDefinition.from_data(manufacturer, model, variant, board_def_data)

    @classmethod
    def read_from_file(cls, file: PathLike):
        with Path(file).open() as f:
            yield from cls.read_from_stream(f)

    @classmethod
    def read_from_stream(cls, stream: TextIO):
        json_data = json.load(stream)
        for category, manufacturers in json_data.items():
            for manufacturer, products in manufacturers.items():
                for product, variants in products.items():
                    for variant, json_defn in variants.items():
                        try:
                            yield BoardDefinition.from_data(
                                manufacturer, product, variant, json_defn
                            )
                        except KeyError as e:
                            logger.warning(
                                f"Board definition for {category}/{manufacturer}/{product}/{variant} is missing {e.args[0]}, skipping..."
                            )
                            continue

    @classmethod
    def get_all_from_file(cls, file: PathLike):
        boards = list(cls.read_from_file(file))
        logger.info(f"Loaded {len(boards)} boards")
        return boards

    @classmethod
    @cache
    def get_all(cls):
        boards = list(cls.read_from_stream(get_boards()))
        logger.info(f"Loaded {len(boards)} boards")
        return boards

    @classmethod
    def get(cls, manufacturer, model, variant):
        if mfr_matches := [x for x in cls.get_all() if x.manufacturer == manufacturer]:
            if model_matches := [x for x in mfr_matches if x.model == model]:
                if variant_matches := [
                    x for x in model_matches if x.variant == variant
                ]:
                    if len(variant_matches) > 1:
                        raise ValueError("Duplicate board definition")
                    elif len(variant_matches) == 1:
                        return variant_matches[0]
        raise ValueError(f"No board data for {manufacturer}/{model}/{variant}")

    @classmethod
    def from_data(
        cls, manufacturer, model, variant, definition: Dict
    ) -> "BoardDefinition":
        opts = {
            "manufacturer": manufacturer,
            "model": model,
            "variant": variant,
            "mcu": BoardMCUDefinition.from_data(definition["mcu"]),
        }
        if (usb := definition.get("usb")) is not None:
            opts["usb"] = usb
        if uart := definition.get("uart"):
            opts["uart"] = BoardUARTDefinition.from_data(uart)
        elif rs232 := definition.get("rs232"):
            opts["uart"] = BoardUARTDefinition.from_data(
                {"tx_pin": rs232["tx"], "rx_pin": rs232["rx"]}
            )
        if can := definition.get("can"):
            opts["can"] = BoardCANDefinition.from_data(can)
            opts['supports_canbridge'] = False
        elif can := definition.get("CAN_Bridge"):
            opts["can"] = BoardCANDefinition.from_data(can)
            opts['supports_canbridge'] = True
        if (status := definition.get("status")) is not None:
            opts["status"] = status
        if (startup_pins := definition.get("startup_pins")) is not None:
            opts["startup_pins"] = startup_pins
        if klipper_opts := definition.get("klipper_options"):
            opts["klipper_options"] = dict(klipper_opts)
        if (opt_features := definition.get("optional_features")) is not None:
            opts["optional_features"] = list(opt_features)
        if instructions := definition.get("instructions"):
            opts["instructions"] = dict(instructions)
        if definition.get("external_can_transceiver"):
            opts["external_can_transceiver"] = True
        return cls(**opts)

    @cached_property
    def interfaces(self):
        interfaces = []
        if self.usb is not None:
            interfaces.append(BoardInterfaceDefinition.usb(self.usb))
        if self.uart:
            interfaces.append(self.uart.as_interface())
        if self.can:
            interfaces.append(self.can.as_interface())
            if self.supports_canbridge:
                interfaces.append(BoardInterfaceDefinition.canbridge(self.can.tx_pin, self.can.rx_pin))
        return interfaces

    def pretty(self, indent=0):
        retstr = f"{self.manufacturer} {self.model}"
        if self.model != self.variant:
            retstr += f" ({self.variant})"
        retstr += "\n"
        retstr += f"  MCU: {self.mcu.pretty()}\n"
        retstr += "  Interfaces:\n"
        for interface in self.interfaces:
            retstr += f"    {interface.pretty()}\n"
        if self.external_can_transceiver:
            retstr += "  CAN transceiver: external (not included on board)\n"
        if self.status is not None:
            retstr += f"  Status LED on {self.status}\n"
        else:
            retstr += "  No status LED\n"

        if self.startup_pins is not None:
            retstr += f"  Pins to set on startup: {self.startup_pins}\n"
        else:
            retstr += "  No startup pins\n"

        if self.optional_features:
            retstr += f"  Optional features: {', '.join(self.optional_features)}\n"

        if self.klipper_options:
            retstr += f"  Advanced options:\n"
            for opt, val in self.klipper_options.items():
                retstr += f"    {opt}={val}\n"
        return retstr


@dataclasses.dataclass
class BoardCANDefinition(object):
    tx_pin: str
    rx_pin: str

    @classmethod
    def from_data(cls, data: Dict | str) -> "BoardCANDefinition":
        if type(data) is str:
            tok = data.split("/")
            return cls(rx_pin=tok[0], tx_pin=tok[1])
        else:
            return cls(rx_pin=data["can_rx"], tx_pin=data["can_tx"])

    def as_interface(self):
        return BoardInterfaceDefinition("CAN", {"rx": self.rx_pin, "tx": self.tx_pin})


@dataclasses.dataclass
class BoardUARTDefinition(object):
    tx_pin: str
    rx_pin: str

    @classmethod
    def from_data(cls, data: Dict) -> "BoardUARTDefinition":
        return cls(rx_pin=data["rx_pin"], tx_pin=data["tx_pin"])

    def as_interface(self):
        return BoardInterfaceDefinition("UART", {"rx": self.rx_pin, "tx": self.tx_pin})


@dataclasses.dataclass
class BoardMCUDefinition(object):
    arch: str
    mcu: str
    clock: Optional[str] = None
    flash: Optional[str] = None

    @classmethod
    def from_data(cls, data: Dict) -> "BoardMCUDefinition":
        return cls(
            arch=data["architecture"],
            mcu=data["mcu"],
            clock=data.get("clock"),
            flash=data.get("flash"),
        )

    def pretty(self):
        retstr = f"{self.arch} {self.mcu}"
        if self.clock and self.flash:
            retstr += f" with {self.clock} clock and {self.flash} flash"
        elif self.clock:
            retstr += f" with {self.clock} clock"
        elif self.flash:
            retstr += f" with {self.flash} flash"
        return retstr


@dataclasses.dataclass
class BoardInterfaceDefinition(object):
    if_type: str
    pins: Dict[str, str]

    def __str__(self) -> str:
        return f"{self.if_type.upper()}"

    @classmethod
    def usb(cls, spec):
        if spec:
            tok = spec.split("/")
            pins = {"dm": tok[0], "dp": tok[1]}
        else:
            pins = {}
        return cls("USB", pins)

    @classmethod
    def canbridge(cls, tx_pin: str, rx_pin: str):
        """USB-to-CAN bridge pseudo-interface: klipper is reachable over
        USB, but internally bridges to CAN using these pins, giving it a
        CAN UUID like a native CAN board would have despite the physical
        connection being USB. Kept as a distinct if_type ("USB to CAN Bridge")
        rather than folded into the plain "CAN" interface, so callers can
        tell the two apart (e.g. the UI's interface-selection menu, or
        deciding which flash path applies)."""
        return cls("USB to CAN Bridge", {"rx": rx_pin, "tx": tx_pin})

    def pretty(self):
        retstr = self.if_type
        if self.pins:
            retstr += f" on {'/'.join(self.pins.values())}"
        return retstr
