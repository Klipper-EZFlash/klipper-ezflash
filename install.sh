#!/usr/bin/env bash

# Invoke remotely with bash <(curl -L URL)
# This preserves stdin/out for proper console interactions

# TODO: Write failure handler
# TODO: Check status of everything important

handle_exit() {
  trap - EXIT
  if [[ $_LOGFD ]]; then
    exec {_LOGFD}>&-
    unset _LOGFD
    unset _LOGFDF
  fi
  if [[ $_LOGFILE ]]; then
    echo "Log file written to $_LOGFILE"
    echo "===END===" >> "$_LOGFILE"
    unset _LOGFILE
  fi
}

handle_err(){
  log "FATAL: In error handler"
  echo "A fatal error has occurred. "
  # TODO: Write some details to the log
}

setup_logging() {
  local logdir logname
  if [[ -d "$HOME/printer_data/logs" ]]; then
    logdir="$HOME/printer_data/logs"
  else
    logdir="/tmp"
  fi

  logname="ezfinstall-$EPOCHSECONDS-$$.log"

  _LOGFILE="$logdir/$logname"
  exec {_LOGFD}>>"$_LOGFILE"
  _LOGFDF="/dev/fd/$_LOGFD"
  cat <<<===START=== >&$_LOGFD
  echo "Logging to '${_LOGFILE}'. please provide this file when requesting support." >&2
}

log() {
  echo "$*" >&$_LOGFD
}

# Print a section heading to the terminal and the log
step() {
  echo ""
  echo "==> $*"
  log "STEP: $*"
}

confirm_tty() {
  local response
  while true; do
    read -p "[y/n]? " -n1 -r response
    if [[ $response =~ [yY] ]]; then
      echo ""
      return 0
    elif [[ $response =~ [Nn] ]]; then
      echo ""
      return 1
    fi
  done
}

become() {
  if [[ $EUID -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
  return $?
}

have_command() {
  command -v "$1" &>/dev/null
  return $?
}

distro_id() {
  while IFS="=" read key val; do
    if [[ $key == "ID" ]]; then
      echo "$val"
      return 0
    fi
  done </etc/os-release
  return 1
}

log_tee() {
  local prefix input_line
  if [[ $1 ]]; then
    prefix="$1: "
  else
    prefix=""
  fi

  while read -r input_line; do
    echo "$prefix$input_line" >&$_LOGFD
    echo "$input_line"
  done
}

install_udev_rules() {
  # Create udev rules to allow access to ST DFU devices (0483:df11) and
  # RP2040 boards in BOOTSEL/UF2 mode (2e8a:0003).
  # RP2350 boards in BOOTSEL/UF2 mode (2e8a:000f).
  # Use MODE=0660 and GROUP=plugdev by default.
  local rulefile="/etc/udev/rules.d/99-klipper-ezflash.rules"
  log "Installing udev rule $rulefile"
  become sh -c "cat > $rulefile <<'UDEVEOF'
# STMicroelectronics DFU (0483:df11)
ATTRS{idVendor}==\"0483\", ATTRS{idProduct}==\"df11\", MODE=\"0660\", GROUP=\"plugdev\"
# Raspberry Pi RP2 Boot / RP2040 BOOTSEL mode (2e8a:0003)
ATTRS{idVendor}==\"2e8a\", ATTRS{idProduct}==\"0003\", MODE=\"0660\", GROUP=\"plugdev\"
# Raspberry Pi RP2 Boot / RP2350 BOOTSEL mode (2e8a:000f)
ATTRS{idVendor}==\"2e8a\", ATTRS{idProduct}==\"000f\", MODE=\"0660\", GROUP=\"plugdev\"
UDEVEOF"
  # Reload udev rules and trigger
  become udevadm control --reload-rules
  become udevadm trigger
  log "udev rule installed"
}

# Ensure the installing user is a member of a group. Two groups are needed:
#   plugdev - raw USB access (STM32 DFU, RP2040/RP2350 BOOTSEL) via the udev
#             rules above
#   dialout - tty access (SoC UARTs, Klipper/Katapult USB serial)
# Pass "create" as the second argument to create a missing group; otherwise a
# missing group is skipped. Sets _NEED_RELOGIN and appends to _NEW_GROUPS if
# membership was changed (group changes only apply to new login sessions).
ensure_group_membership() {
  local group="$1" mode="${2:-}"
  local user="${SUDO_USER:-${USER:-$(id -un)}}"

  if [[ $user == root ]]; then
    log "Running as root, skipping $group membership"
    return
  fi

  if ! getent group "$group" &>/dev/null; then
    if [[ $mode == create ]]; then
      log "$group group missing, creating it"
      become groupadd -r "$group" |& log_tee groupadd
    else
      log "$group group missing, skipping"
      return
    fi
  fi

  if id -nG "$user" | tr ' ' '\n' | grep -qx "$group"; then
    log "$user is already in $group"
    return
  fi

  log "Adding $user to $group"
  echo "Adding $user to the $group group so flashing works without sudo."
  become usermod -aG "$group" "$user" |& log_tee usermod
  _NEED_RELOGIN="yes"
  _NEW_GROUPS="${_NEW_GROUPS:+$_NEW_GROUPS, }$group"
}

_NEED_RELOGIN=""
_NEW_GROUPS=""

# Minimum Python version. Keep in sync with requires-python in pyproject.toml.
# 3.10 is what the code needs today: model.py and util.py use `X | Y` in
# annotations that are evaluated at import time (no `from __future__ import
# annotations`), which is 3.10+. Textual itself needs 3.9+.
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

check_python_version() {
  local found
  if ! have_command python3; then
    log "python3 not found"
    echo "python3 is required but was not found."
    echo "Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} or newer and run this installer again."
    exit 1
  fi

  found="$(python3 -c 'import platform; print(platform.python_version())')"
  if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)"; then
    log "python3 too old: $found (need ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+)"
    echo "EZFlash needs Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} or newer, but found $found."
    echo "Upgrade Python (or the OS image) and run this installer again."
    exit 1
  fi
  log "python3 version ok: $found"
}

main() {
  setup_logging

  echo "Welcome to the EZFlash installer."
  echo ""
  echo "EZFlash is prerelease software."
  echo "It may eat your firmware and/or cat."
  echo -n "Would you like to continue "
  if ! confirm_tty; then
    log Exiting on user request
    echo "Exiting on user request..."
    exit 0
  fi

  local basedir pyenv distro repodir
  local -a missing_debs=()

  distro="$(distro_id)"

  if [[ $distro =~ debian|ubuntu ]]; then
    step "Checking dependencies"
    for pkgname in python3 python3-pip python3-venv python3-serial git; do
      log "checking $pkgname"
      dpkg-query --show "$pkgname" |& log_tee dpkg-query >/dev/null
      if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
        log "found $pkgname"
      else
        log "added $pkgname to install list"
        missing_debs+=( "$pkgname" )
      fi
    done

    if [[ ${#missing_debs[@]} -gt 0 ]]; then
      step "Installing dependencies (your password may be needed)"
      become apt-get update |& log_tee apt-update
      become apt-get install -y "${missing_debs[@]}" |& log_tee apt-install
    else
      echo "All dependencies already present"
    fi
  else
    echo "Unknown distro '$distro', dependencies will not be automatically installed"
    log "Unknown distro $distro, skipping dependency install"
  fi

  # Runs after the dependency step (which is what installs python3 on
  # Debian/Ubuntu) and before anything is created, so an unsupported Python
  # fails here with a clear message instead of partway through pip install.
  step "Checking Python version"
  check_python_version

  basedir="$HOME/.ezf"
  if [[ -d $basedir ]]; then
    log "Basedir already exists"
    echo ""
    echo "There may already be a copy of EZFlash installed."
    echo -n "Proceed anyway "
    if ! confirm_tty; then
      log Exiting on user request
      echo "Exiting on user request..."
      exit 0
    fi
  else
    mkdir -p "$basedir"
  fi

  step "Creating Python environment"
  pyenv="$basedir/py"
  python3 -m venv --clear --prompt ezf "$pyenv" |& log_tee py-venv

  step "Installing Python packages"
  (
    "$pyenv/bin/pip" install -U pip
    "$pyenv/bin/pip" install wheel
  ) |& log_tee pip

  repodir="$basedir/src"
  if [[ -d $repodir ]]; then
    step "Updating existing EZFlash source"
    log "Using existing repo"
    (
      git -C "$repodir" fetch -v origin main
      git -C "$repodir" reset --hard FETCH_HEAD
    ) |& log_tee git
  else
    step "Cloning EZFlash source"
    log "Cloning new repo"
    git clone -v https://github.com/laikulo/klipper-ezflash.git -b main "$repodir" |& log_tee git
  fi

  step "Installing EZFlash"
  log "editable installation"
  "$pyenv/bin/pip" install -e "$repodir" |& log_tee pip

  step "Creating symlinks"
  [[ -d $HOME/.local/bin ]] || mkdir -p "$HOME/.local/bin"
  ln -sf "$pyenv/bin/ezflash" "$HOME/.local/bin/ezflash"
  ln -sf "$pyenv/bin/ezf" "$HOME/.local/bin/ezf"
  ln -sf "$pyenv/bin/ezflash" "$HOME/ezflash"
  ln -sf "$pyenv/bin/ezf" "$HOME/ezf"

  # Make sure the user is in the groups that grant device access: plugdev for
  # USB bootloaders (ST DFU, RP2040/RP2350 BOOTSEL), dialout for serial/UART
  # ports. plugdev is created first if missing so the udev rules below can
  # resolve GROUP="plugdev" when they load.
  step "Checking group membership"
  ensure_group_membership plugdev create
  ensure_group_membership dialout
  step "Installing udev rules"
  install_udev_rules

  echo ""
  echo "Installation complete!"
  echo "Run it with: ~/ezflash"
  if [[ $_NEED_RELOGIN ]]; then
    echo "You were added to the following group(s): $_NEW_GROUPS. Log out and back in"
    echo "(or reboot) before flashing so the new group membership takes effect."
  fi
}

trap handle_exit EXIT
trap handle_err  ERR
main "$@"
exit $?
