#!/usr/bin/env bash
set -euo pipefail

# Configure a keyboard and one mouse on the board's USB Device port.
# Supply a VID/PID that you are authorized to use; no IDs are embedded here.
GADGET_ROOT=/sys/kernel/config/usb_gadget
GADGET="$GADGET_ROOT/kvmcp"
PRODUCT='kvmcp HID'
VID="${KVMCP_USB_VID:?Set KVMCP_USB_VID to an authorized 0xNNNN USB vendor ID}"
PID="${KVMCP_USB_PID:?Set KVMCP_USB_PID to an authorized 0xNNNN USB product ID}"
MOUSE_MODE="${KVMCP_MOUSE_MODE:-absolute}"

die() { printf 'kvmcp HID: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'Run as root.'
[[ $VID =~ ^0x[[:xdigit:]]{4}$ && $PID =~ ^0x[[:xdigit:]]{4}$ ]] || die 'VID and PID must each be 0xNNNN.'
case $MOUSE_MODE in
    absolute)
        MOUSE_PROTOCOL=0
        MOUSE_SUBCLASS=0
        MOUSE_REPORT_LENGTH=6
        # Eight buttons, absolute 0..32767 X/Y, and a relative wheel.
        MOUSE_REPORT_DESC='05010902a1010901a1000509190129081500250195087501810205010930093116000026ff7f75109502810209381581257f750895018106c0c0'
        ;;
    relative)
        MOUSE_PROTOCOL=2
        MOUSE_SUBCLASS=1
        MOUSE_REPORT_LENGTH=4
        # Three buttons, signed relative X/Y, and a wheel.
        MOUSE_REPORT_DESC='05010902a1010901a1000509190129031500250195037501810295017505810305010930093109381581257f750895038106c0c0'
        ;;
    *) die 'KVMCP_MOUSE_MODE must be absolute or relative.' ;;
esac

modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs configfs /sys/kernel/config
[[ -d $GADGET_ROOT ]] || die 'USB configfs gadget support is unavailable.'

udcs=(/sys/class/udc/*)
[[ -e ${udcs[0]} ]] || die 'No USB Device Controller (UDC) is available.'
[[ ${#udcs[@]} -eq 1 ]] || die 'More than one UDC exists; select one explicitly before using this script.'
UDC=${udcs[0]##*/}

grant_hid_access() {
    getent group video >/dev/null || die 'The video group is required for non-root HID access.'
    command -v udevadm >/dev/null && udevadm settle || true
    local function major minor node node_major node_minor found attempt
    for function in keyboard mouse; do
        IFS=: read -r major minor < "$GADGET/functions/hid.$function/dev"
        found=0
        for ((attempt = 0; attempt < 20 && !found; attempt++)); do
            for node in /dev/hidg*; do
                [[ -c $node ]] || continue
                IFS=: read -r node_major node_minor < <(stat -c '%t:%T' "$node")
                if ((16#$node_major == major && 16#$node_minor == minor)); then
                    chgrp video "$node"
                    chmod 0660 "$node"
                    found=1
                    break
                fi
            done
            ((found)) || sleep 0.1
        done
        ((found)) || die "No /dev/hidg node for hid.$function"
    done
}

if [[ -d $GADGET ]]; then
    [[ -f $GADGET/strings/0x409/product && $(<"$GADGET/strings/0x409/product") == "$PRODUCT" ]] || die 'The kvmcp gadget name is already in use.'
    [[ $(<"$GADGET/UDC") == "$UDC" ]] || die 'A partial or unbound kvmcp gadget exists; inspect or tear it down first.'
    active_vid=$(<"$GADGET/idVendor")
    active_pid=$(<"$GADGET/idProduct")
    [[ ${VID,,} == ${active_vid,,} ]] || die 'A different VID is active; tear the gadget down before changing it.'
    [[ ${PID,,} == ${active_pid,,} ]] || die 'A different PID is active; tear the gadget down before changing it.'
    [[ $(<"$GADGET/functions/hid.mouse/report_length") == "$MOUSE_REPORT_LENGTH" ]] || die 'A different mouse mode is active; tear it down before switching modes.'
    grant_hid_access
    printf 'kvmcp HID (%s mouse) is already bound to %s\n' "$MOUSE_MODE" "$UDC"
    exit 0
fi

for other in "$GADGET_ROOT"/*; do
    [[ -d $other && -f $other/UDC ]] || continue
    [[ $(<"$other/UDC") != "$UDC" ]] || die "UDC $UDC is already used by $other"
done

write_hex() {
    local hex=$1 i escaped=''
    for ((i = 0; i < ${#hex}; i += 2)); do
        escaped+="\\x${hex:i:2}"
    done
    # configfs treats each write(2) as a replacement, so emit the descriptor once.
    printf '%b' "$escaped"
}

created=0
rollback() {
    trap - ERR
    printf '' > "$GADGET/UDC" 2>/dev/null || true
    for function in keyboard mouse; do
        [[ ! -L $GADGET/configs/c.1/hid.$function ]] || rm -- "$GADGET/configs/c.1/hid.$function"
    done
    rmdir "$GADGET/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET/functions/hid.keyboard" 2>/dev/null || true
    rmdir "$GADGET/functions/hid.mouse" 2>/dev/null || true
    rmdir "$GADGET/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET" 2>/dev/null || true
}
trap 'if ((created)); then rollback; fi' ERR

mkdir "$GADGET"
created=1
mkdir "$GADGET/strings/0x409"
printf '%s' "$PRODUCT" > "$GADGET/strings/0x409/product"
printf '%s' "$VID" > "$GADGET/idVendor"
printf '%s' "$PID" > "$GADGET/idProduct"
printf '%s' 'kvmcp' > "$GADGET/strings/0x409/manufacturer"
# A mode switch changes the HID descriptor; use a distinct USB identity so the
# host does not reuse a cached descriptor from the other mouse mode.
printf '%s-%s' "$(cat /etc/machine-id)" "$MOUSE_MODE" > "$GADGET/strings/0x409/serialnumber"
printf '%s' '0x0200' > "$GADGET/bcdUSB"

mkdir "$GADGET/configs/c.1"
mkdir "$GADGET/configs/c.1/strings/0x409"
printf '%s' 'Keyboard and mouse' > "$GADGET/configs/c.1/strings/0x409/configuration"
printf '%s' '250' > "$GADGET/configs/c.1/MaxPower"

mkdir "$GADGET/functions/hid.keyboard"
printf '%s' '1' > "$GADGET/functions/hid.keyboard/protocol"
printf '%s' '1' > "$GADGET/functions/hid.keyboard/subclass"
printf '%s' '8' > "$GADGET/functions/hid.keyboard/report_length"
# 63-byte boot-keyboard descriptor from the Linux HID gadget documentation.
write_hex '05010906a101050719e029e71500250175019508810295017508810395057501050819012905910295017503910395067508150025650507190029658100c0' > "$GADGET/functions/hid.keyboard/report_desc"

mkdir "$GADGET/functions/hid.mouse"
printf '%s' "$MOUSE_PROTOCOL" > "$GADGET/functions/hid.mouse/protocol"
printf '%s' "$MOUSE_SUBCLASS" > "$GADGET/functions/hid.mouse/subclass"
printf '%s' "$MOUSE_REPORT_LENGTH" > "$GADGET/functions/hid.mouse/report_length"
write_hex "$MOUSE_REPORT_DESC" > "$GADGET/functions/hid.mouse/report_desc"

ln -s "$GADGET/functions/hid.keyboard" "$GADGET/configs/c.1/hid.keyboard"
ln -s "$GADGET/functions/hid.mouse" "$GADGET/configs/c.1/hid.mouse"
printf '%s' "$UDC" > "$GADGET/UDC"
grant_hid_access
trap - ERR
printf 'kvmcp HID (%s mouse) bound to %s\n' "$MOUSE_MODE" "$UDC"
