"""Find the Meshtastic radio plugged into this computer, and MeshLearn's channel on it.

So the teacher can start the hub without knowing that the radio is /dev/cu.usbmodem1101
today and COM5 tomorrow, or which slot the MeshLearn channel landed in.
"""

from __future__ import annotations

import glob
import sys
from typing import Callable, Iterable, List, Optional

# USB vendor ids of the chips on Meshtastic boards: Espressif's own USB (ESP32-S3), the
# CP210x, CH340/CH9102 and FTDI serial chips, Adafruit/RAK nRF52, Nordic, Seeed and RP2040.
RADIO_VIDS = {0x303A, 0x10C4, 0x1A86, 0x0403, 0x239A, 0x1915, 0x2886, 0x2E8A}

# Serial devices every Mac has that are never a radio.
_NOT_RADIOS = ("Bluetooth-Incoming-Port", "debug-console", "wlan-debug")


class NoRadioFound(RuntimeError):
    pass


def find_radio_ports(comports: Optional[Iterable] = None) -> List[str]:
    """USB serial ports that look like a Meshtastic radio, best guesses first.

    comports: pyserial's port list (for tests); read from the system when None.
    """
    if comports is None:
        try:
            from serial.tools import list_ports  # installed with meshtastic
            comports = list_ports.comports()
        except ImportError:
            return _by_name()
    usb = [p for p in comports if getattr(p, "vid", None) is not None
           and not any(n in p.device for n in _NOT_RADIOS)]
    known = [p.device for p in usb if p.vid in RADIO_VIDS]
    return _dedupe(known or [p.device for p in usb])


def _by_name() -> List[str]:
    if sys.platform.startswith("win"):
        return []
    found = []
    for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB*",
                    "/dev/ttyACM*", "/dev/ttyUSB*"):
        found += sorted(glob.glob(pattern))
    return _dedupe(found)


def _dedupe(ports: List[str]) -> List[str]:
    """On a Mac each port shows up as /dev/cu.X and /dev/tty.X; the cu one is the right one to open."""
    out = []
    for port in ports:
        twin = port.replace("/dev/tty.", "/dev/cu.", 1)
        if twin != port and twin in ports:
            continue
        if port not in out:
            out.append(port)
    return out


def choose_radio_port(ports: Optional[List[str]] = None, ask: Optional[Callable[[str], str]] = None) -> str:
    """The radio's port. With several candidates, asks which one when someone can answer."""
    ports = find_radio_ports() if ports is None else ports
    if not ports:
        tip = ("On Windows the radio may need its USB driver (CP210x or CH340, see docs/hardware.md). "
               if sys.platform.startswith("win") else "")
        raise NoRadioFound("No radio is plugged in. Plug the radio into this computer with a USB cable that "
                           "carries data (some cables only charge), wait a few seconds, and start again. " + tip)
    if len(ports) == 1:
        return ports[0]
    if ask is None and sys.stdin is not None and sys.stdin.isatty():
        ask = input
    if ask is None:
        raise NoRadioFound("More than one radio is plugged in: " + ", ".join(ports) +
                           ". Unplug the extra ones, or start with --radio-serial and the one to use.")
    print("More than one radio is plugged in:")
    for n, port in enumerate(ports, 1):
        print(f"  {n}. {port}")
    while True:
        answer = (ask(f"Which one is the hub's radio? (1-{len(ports)}) ") or "").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(ports):
            return ports[int(answer) - 1]


def meshlearn_channel(channels: Optional[Iterable]) -> Optional[int]:
    """Index of the radio's channel named MeshLearn (any case), or None when it has none.

    channels: Meshtastic Channel messages (index, role, settings.name), as on iface.localNode.channels.
    """
    for ch in channels or ():
        settings = getattr(ch, "settings", None)
        name = (getattr(settings, "name", "") or "").strip().lower()
        if name == "meshlearn" and getattr(ch, "role", 0) != 0:  # role 0 = DISABLED
            return int(ch.index)
    return None
