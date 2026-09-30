#!/usr/bin/env bash
set -euo pipefail

GADGET=/sys/kernel/config/usb_gadget/kvmcp

die() { printf 'kvmcp HID: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'Run as root.'
if [[ ! -d $GADGET ]]; then
    printf 'kvmcp HID is not configured\n'
    exit 0
fi
[[ -f $GADGET/strings/0x409/product && $(<"$GADGET/strings/0x409/product") == 'kvmcp HID' ]] || die 'The kvmcp gadget name is in use by an unknown configuration; leaving it untouched.'

printf '' > "$GADGET/UDC"
for function in keyboard mouse; do
    link="$GADGET/configs/c.1/hid.$function"
    [[ ! -L $link ]] || rm -- "$link"
done
rmdir "$GADGET/configs/c.1/strings/0x409"
rmdir "$GADGET/configs/c.1"
rmdir "$GADGET/functions/hid.keyboard"
rmdir "$GADGET/functions/hid.mouse"
rmdir "$GADGET/strings/0x409"
rmdir "$GADGET"
printf 'kvmcp HID removed\n'
