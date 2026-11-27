#!/bin/bash
# Local Boardsource Left build for the WSL workspace (see .github/workflows/build.yml for the CI recipe this mirrors).
# usage: build_local_boardsource.sh OUTDIR [extra EXTRA_CONF_FILE entries, ;-separated] [extra DTC overlay]
set -e
export ZEPHYR_SDK_INSTALL_DIR=$HOME/steno-ws/zephyr-sdk-0.17.0 ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export PATH=$HOME/.local/bin:$PATH
repo="$(cd "$(dirname "$0")/../.." && pwd)"
split="$repo/config/nicenano-wireless-corne"
bs="$repo/config/boardsource-wireless-corne"
cd "$HOME/steno-ws/ws"
conf="$split/corne.conf;$split/corne_left.conf;$bs/phase1-everyday.conf;$bs/transfer.conf;$bs/memory.conf"
[ -n "$2" ] && conf="$conf;$2"
args=("-DZMK_EXTRA_MODULES=$repo" "-DBOARD_ROOT=$HOME/steno-ws/vendor" "-DZMK_CONFIG=$bs" "-DEXTRA_CONF_FILE=$conf")
[ -n "$3" ] && args+=("-DEXTRA_DTC_OVERLAY_FILE=$3")
west build -s zmk/app -d "$HOME/steno-ws/$1" -b blecorne_left -S studio-rpc-usb-uart -p auto -- "${args[@]}" > "$HOME/steno-ws/$1.log" 2>&1
tail -4 "$HOME/steno-ws/$1.log"
