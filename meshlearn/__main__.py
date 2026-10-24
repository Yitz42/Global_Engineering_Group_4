"""Command line: python -m meshlearn {demo,hub,phone,library} ..."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import threading

from .hub import Hub
from .library import run as run_library
from .messenger import Messenger
from .phone import Phone
from .transport import MeshtasticTransport, UdpSimTransport, parse_peers

SAMPLE_ASSIGNMENT = os.path.join(os.path.dirname(__file__), "sample_assignment.json")


def _add_radio_args(p: argparse.ArgumentParser, default_node: str, default_port: int) -> None:
    g = p.add_argument_group("radio link (pick one)")
    g.add_argument("--radio-serial", metavar="DEVICE", help="real Meshtastic radio on USB, e.g. /dev/ttyUSB0 or COM3")
    g.add_argument("--radio-host", metavar="IP", help="real Meshtastic radio over Wi-Fi (TCP), e.g. 192.168.43.20")
    g.add_argument("--sim-node", default=default_node, help=f"simulator: this node's name (default {default_node})")
    g.add_argument("--sim-port", type=int, default=default_port, help=f"simulator: UDP port to listen on (default {default_port})")
    g.add_argument("--sim-peer", action="append", default=[], metavar="NAME=HOST:PORT",
                   help="simulator: another node, e.g. phone-1=127.0.0.1:47002 (repeatable)")
    g.add_argument("--sim-loss", type=float, default=0.0, help="simulator: fraction of packets to drop (0-1)")


def _transport(args):
    if args.radio_serial or args.radio_host:
        return MeshtasticTransport(device=args.radio_serial, host=args.radio_host), 30.0
    return UdpSimTransport(args.sim_node, args.sim_port, parse_peers(args.sim_peer), drop_rate=args.sim_loss), 3.0


def _messenger_factory(transport, db_path, retry_s):
    return lambda handler: Messenger(transport, db_path, handler, retry_s=retry_s)


def _wait_forever(*stoppables):
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        stop.wait(1)
    for s in stoppables:
        s()


def cmd_hub(args) -> int:
    os.makedirs(args.data, exist_ok=True)
    transport, retry = _transport(args)
    hub = Hub(os.path.join(args.data, "hub.db"), _messenger_factory(transport, os.path.join(args.data, "outbox.db"), retry))
    hub.messenger.start()
    server = hub.app.serve_in_thread(args.host, args.port)
    print(f"Teacher hub on http://localhost:{args.port}  (radio node: {transport.node_id or 'unknown'})")
    _wait_forever(server.shutdown, hub.messenger.stop)
    return 0


def cmd_phone(args) -> int:
    transport, retry = _transport(args)
    phone = Phone(args.data, args.hub, args.name,
                  _messenger_factory(transport, os.path.join(args.data, "outbox.db"), retry), kiwix_url=args.kiwix_url)
    phone.messenger.start()
    phone.say_hello()
    server = phone.app.serve_in_thread(args.host, args.port)
    print(f"Student site on http://<this phone's hotspot address>:{args.port}  (radio node: {transport.node_id or 'unknown'})")
    _wait_forever(server.shutdown, phone.messenger.stop)
    return 0


def cmd_demo(args) -> int:
    """Hub and one phone in a single process, linked by the simulated radio."""
    root = args.data or tempfile.mkdtemp(prefix="meshlearn-demo-")
    hub_dir, phone_dir = os.path.join(root, "hub"), os.path.join(root, "phone")
    os.makedirs(hub_dir, exist_ok=True)
    hub_t = UdpSimTransport("hub", args.sim_port, {"phone-1": ("127.0.0.1", args.sim_port + 1)}, drop_rate=args.loss)
    phone_t = UdpSimTransport("phone-1", args.sim_port + 1, {"hub": ("127.0.0.1", args.sim_port)}, drop_rate=args.loss)
    hub = Hub(os.path.join(hub_dir, "hub.db"), _messenger_factory(hub_t, os.path.join(hub_dir, "outbox.db"), 2.0))
    phone = Phone(phone_dir, "hub", "Demo village phone", _messenger_factory(phone_t, os.path.join(phone_dir, "outbox.db"), 2.0))
    hub.messenger.start()
    phone.messenger.start()
    hub.add_phone("phone-1", "Demo village phone")
    phone.say_hello()
    with open(SAMPLE_ASSIGNMENT, encoding="utf-8") as fh:
        sample = hub.create_assignment(json.load(fh))
    hub.send_assignment(sample["id"], "phone-1")
    hub_srv = hub.app.serve_in_thread("127.0.0.1", args.hub_port)
    phone_srv = phone.app.serve_in_thread("127.0.0.1", args.phone_port)
    print(f"""
MeshLearn demo running (simulated radio, {args.loss:.0%} packet loss). Data in {root}

  Teacher hub:   http://localhost:{args.hub_port}
  Student phone: http://localhost:{args.phone_port}

A sample assignment is on its way to the phone. Open the student site, enter a
name, complete it, then watch it arrive on the teacher hub. Ctrl+C to stop.
""")
    _wait_forever(hub_srv.shutdown, phone_srv.shutdown, hub.messenger.stop, phone.messenger.stop)
    return 0


def cmd_library(args) -> int:
    return run_library(args.sd_gb, args.lang, args.dest, reserve_gb=args.reserve_gb, dry_run=args.dry_run)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meshlearn", description="Offline assignments and library over a LoRa mesh.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="run a hub and a phone together with a simulated radio")
    d.add_argument("--hub-port", type=int, default=8000)
    d.add_argument("--phone-port", type=int, default=8080)
    d.add_argument("--sim-port", type=int, default=47001)
    d.add_argument("--loss", type=float, default=0.2, help="fraction of radio packets to drop (default 0.2)")
    d.add_argument("--data", help="folder for demo data (default: a temporary folder)")
    d.set_defaults(func=cmd_demo)

    h = sub.add_parser("hub", help="run the teacher hub")
    h.add_argument("--data", default="hub-data", help="folder for the hub database")
    h.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach the dashboard from other devices")
    h.add_argument("--port", type=int, default=8000)
    _add_radio_args(h, "hub", 47001)
    h.set_defaults(func=cmd_hub)

    p = sub.add_parser("phone", help="run the student phone server")
    p.add_argument("--data", required=True, help="folder on the microSD card, e.g. ~/storage/external-1/meshlearn")
    p.add_argument("--hub", required=True, help="the hub's radio node id (e.g. !a1b2c3d4, or 'hub' in the simulator)")
    p.add_argument("--name", default="Student phone", help="name shown to the teacher")
    p.add_argument("--host", default="0.0.0.0", help="address to serve on (0.0.0.0 = every network, incl. the hotspot)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--kiwix-url", default="", help="where kiwix-serve runs, e.g. http://127.0.0.1:8090, to open .zim files")
    _add_radio_args(p, "phone-1", 47002)
    p.set_defaults(func=cmd_phone)

    lib = sub.add_parser("library", help="download open content sized to an SD card")
    lib.add_argument("--sd-gb", type=float, required=True, help="card size printed on the label, in GB")
    lib.add_argument("--lang", default="en", choices=["en", "pt", "de"])
    lib.add_argument("--dest", required=True, help="the phone's library folder, e.g. /Volumes/SDCARD/meshlearn/library")
    lib.add_argument("--reserve-gb", type=float, default=2.0, help="space to leave free (default 2)")
    lib.add_argument("--dry-run", action="store_true", help="show the plan without downloading")
    lib.set_defaults(func=cmd_library)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
