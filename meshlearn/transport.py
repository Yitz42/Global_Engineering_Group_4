"""Ways to move frames between nodes.

* UdpSimTransport: a simulated radio on one computer (UDP on localhost), with
  optional packet loss and delay, so the whole system can be demoed without
  hardware.
* MeshtasticTransport: real LoRa radios running Meshtastic firmware, reached
  over USB serial or Wi-Fi (TCP). Requires `pip install meshtastic`.
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

FrameHandler = Callable[[bytes, str], None]


class Transport:
    """Sends single frames to a named node and reports frames received."""

    node_id: str = ""

    def start(self, on_frame: FrameHandler) -> None:
        raise NotImplementedError

    def send(self, frame: bytes, dest: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _Pacer:
    """Keeps a minimum gap between transmissions, as a real radio needs."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self.min_interval_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def parse_peers(specs) -> Dict[str, Tuple[str, int]]:
    """Turn ["hub=127.0.0.1:47001", ...] into {"hub": ("127.0.0.1", 47001)}."""
    peers = {}
    for spec in specs or []:
        name, _, addr = spec.partition("=")
        host, _, port = addr.rpartition(":")
        if not name or not port:
            raise ValueError(f"bad peer spec {spec!r}; expected name=host:port")
        peers[name] = (host or "127.0.0.1", int(port))
    return peers


class UdpSimTransport(Transport):
    """Simulated mesh: each node is a UDP port; drop_rate imitates lost packets."""

    def __init__(
        self,
        node_id: str,
        listen_port: int,
        peers: Dict[str, Tuple[str, int]],
        drop_rate: float = 0.0,
        latency_s: float = 0.0,
        min_interval_s: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.node_id = node_id
        self.listen_port = listen_port
        self.peers = dict(peers)
        self.drop_rate = drop_rate
        self.latency_s = latency_s
        self._pacer = _Pacer(min_interval_s)
        self._rng = random.Random(seed)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", listen_port))
        self._sock.settimeout(0.5)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self, on_frame: FrameHandler) -> None:
        self._running = True

        def loop():
            while self._running:
                try:
                    data, _ = self._sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                name_len = data[0]
                src = data[1:1 + name_len].decode("utf-8", "replace")
                frame = data[1 + name_len:]
                try:
                    on_frame(frame, src)
                except Exception:  # keep the radio loop alive whatever the handler does
                    log.exception("frame handler failed")

        self._thread = threading.Thread(target=loop, name=f"udp-{self.node_id}", daemon=True)
        self._thread.start()

    def send(self, frame: bytes, dest: str) -> None:
        if dest not in self.peers:
            raise KeyError(f"unknown peer {dest!r}")
        self._pacer.wait()
        if self._rng.random() < self.drop_rate:
            log.debug("sim: dropped frame to %s", dest)
            return
        name = self.node_id.encode("utf-8")
        datagram = bytes([len(name)]) + name + frame
        if self.latency_s:
            time.sleep(self.latency_s)
        self._sock.sendto(datagram, self.peers[dest])

    def close(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass


class MeshtasticTransport(Transport):
    """Real LoRa radios running Meshtastic firmware.

    Connect with `device="/dev/ttyUSB0"` (USB serial) or `host="192.168.43.20"`
    (a Wi-Fi-enabled node, e.g. one joined to the phone's hotspot). Frames go
    on Meshtastic's PRIVATE_APP port, so normal chat traffic is untouched.
    Node ids are Meshtastic ids such as "!a1b2c3d4".

    min_interval_s paces transmissions; on the default LongFast preset a gap of
    a few seconds keeps the radio's queue from overflowing.
    """

    def __init__(self, device: Optional[str] = None, host: Optional[str] = None, min_interval_s: float = 3.0):
        try:
            from pubsub import pub  # installed with meshtastic
            try:
                from meshtastic.protobuf import portnums_pb2  # meshtastic >= 2.3
            except ImportError:  # older releases
                from meshtastic import portnums_pb2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Real radios need the meshtastic package: pip install meshtastic") from exc

        self._pub = pub
        self._port = portnums_pb2.PortNum.PRIVATE_APP
        self._port_name = "PRIVATE_APP"
        self._pacer = _Pacer(min_interval_s)
        if host:
            from meshtastic.tcp_interface import TCPInterface
            self._iface = TCPInterface(hostname=host)
        else:
            from meshtastic.serial_interface import SerialInterface
            self._iface = SerialInterface(devPath=device)
        info = self._iface.getMyNodeInfo() or {}
        self.node_id = (info.get("user") or {}).get("id", "")
        self._on_frame: Optional[FrameHandler] = None

    def start(self, on_frame: FrameHandler) -> None:
        self._on_frame = on_frame
        self._pub.subscribe(self._receive, "meshtastic.receive")

    def _receive(self, packet, interface=None):
        decoded = packet.get("decoded") or {}
        if decoded.get("portnum") != self._port_name or self._on_frame is None:
            return
        payload = decoded.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            try:
                self._on_frame(bytes(payload), packet.get("fromId") or "")
            except Exception:
                log.exception("frame handler failed")

    def send(self, frame: bytes, dest: str) -> None:
        self._pacer.wait()
        self._iface.sendData(frame, destinationId=dest, portNum=self._port, wantAck=True)

    def close(self) -> None:
        try:
            self._pub.unsubscribe(self._receive, "meshtastic.receive")
        except Exception:
            pass
        self._iface.close()
