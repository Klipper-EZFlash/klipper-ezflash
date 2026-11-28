import logging
from pathlib import Path

from argparse import ArgumentParser
from ..configurator import Configurator
from ..model import BoardDefinition
from ..util import find_klipper, find_katapult


def main():
    argp = ArgumentParser()
    argp.add_argument("--die-fast", action="store_true")
    args = argp.parse_args()

    die_fast = args.die_fast

    boards = BoardDefinition.get_all()

    klipper_path = find_klipper()
    katapult_path = find_katapult()
    failures = []
    print(f"Checking {len(boards)} boards configuration...")
    for board in boards:
        for target in ((Configurator.klipper, klipper_path), (Configurator.katapult, katapult_path)):
            try:
                config = target[0](target[1], board=board)
                if target[0] == Configurator.katapult and board.bootloader_pins:
                    sym = config.kconfig.symbol(prompt="GPIO pins to set on bootloader entry")
                    if sym is None or sym.get_board() != board.bootloader_pins:
                        failures.append(f"{board} Katapult: bootloader_pins not applied")

                for i in config.get_interfaces():
                    # CAN bridge is Klipper-only
                    if (target[0] == Configurator.katapult
                            and i.if_type in ("USB to CAN Bridge")):
                        continue
                    try:
                        config.set_interface(i)
                    except Exception as e:
                        if die_fast:
                            raise e
                        failures.append(f"{board}/{i}: {e!r}")
            except Exception as e:
                if die_fast:
                    raise e
                failures.append(f"{board} {target[0].__name__}: {e!r}")
    if failures:
        print("==== FAIL ====")
        for failure in failures:
            print(failure)
        raise SystemExit(1)
    else:
        print("==== PASS ====")


if __name__ == "__main__":
    main()
