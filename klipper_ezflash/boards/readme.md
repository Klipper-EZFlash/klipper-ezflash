Hirerarchy
* Pirmary Category (e.g. mainboards)
  * manufactuerr
    * Product name
      * Product variant
        * (descriptior)

Fields for descriptiors:
* mcu
  * mcu (as per Klipper's MCU field)
  * clock (in Mhz)
* CAN_Bridge (empty object/absent/null means not supported)
  * can_tx (pin_name)
  * can_rx (pin_name)
* external_can_transceiver (optional boolean)
  * true: the board exposes CAN pins but has no onboard CAN transceiver; one
    must be wired up externally. Applies to both `can` and `CAN_Bridge`.
  * absent/false: an onboard transceiver is assumed.
* usb
  * "${PIN}/${PIN}"
* uart
* status (pin_name)
* klipper_options
  * serial_number - str
  * ... (per target)
* optional_features
  * List of exact Klipper Kconfig symbol names from the "Optional features
    (to reduce code size)" menu in src/Kconfig (e.g. "WANT_NEOPIXEL",
    "WANT_ST7920") -- NOT free-text tags. Only needed for small-flash MCUs
    (e.g. STM32F042) where Klipper's defaults (everything on) overflow the
    firmware. When present, every other optional-feature symbol is
    disabled. See OPTIONAL_FEATURE_SYMBOLS in configurator.py
    for the full known list.



TODOs:
* Merge CAN_Bridge and can
* Merge uart and rs232
* Standardize on pin naming for rs232/uart
* Get rid of nulls for usb
* Figure out how to represent USB with no options
  * Right now, empty object, but that can be a mess
* Document order of / notation
