#!/usr/bin/env python3
"""
LoRa / RF host for Coherence Monitor Gates 4 and 6.

Expected node line format (UTF-8, newline terminated), as emitted by an
ESP32 + SX1262 / SX1276 sketch:

    NODE,A,seq,tx_ms,entropy_hex,rssi,snr

Example:
    NODE,A,142,3842100,a3f91c02, -98.5, 7.2

Two serial ports may be attached (node A and node B). A single shared
receiver that forwards both node IDs is also supported.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

import numpy as np

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # type: ignore


SAMPLE_INTERVAL_S = 4.2
MI_SIGNAL_BITS = 0.05
MI_NULL_BITS = 0.01


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lora_packets (
            timestamp REAL,
            node_id TEXT,
            rssi REAL,
            snr REAL,
            entropy_hash TEXT,
            tx_timestamp INTEGER,
            rx_timestamp INTEGER,
            packet_error INTEGER,
            seq INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lora_epochs (
            timestamp REAL PRIMARY KEY,
            mi_bits REAL,
            jitter_ratio REAL,
            rssi_a REAL,
            rssi_b REAL,
            snr_a REAL,
            snr_b REAL,
            n_packets INTEGER,
            gate6_flag INTEGER
        )
        """
    )
    conn.commit()
    return conn


def hash_to_unit(h: str) -> float:
    try:
        n = int(h, 16)
    except ValueError:
        n = abs(hash(h))
    return (n % 10_000_000) / 10_000_000.0


def cross_mutual_information(stream_a, stream_b, bins: int = 16) -> float:
    a = np.asarray(stream_a, dtype=np.float64)
    b = np.asarray(stream_b, dtype=np.float64)
    n = min(a.size, b.size)
    if n < 8:
        return 0.0
    a, b = a[-n:], b[-n:]
    # Equal-width bins on [0, 1]
    ja, _, _ = np.histogram2d(a, b, bins=bins, range=[[0, 1], [0, 1]])
    joint = ja / ja.sum()
    pa = joint.sum(axis=1, keepdims=True)
    pb = joint.sum(axis=0, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = joint / (pa * pb)
        logt = np.zeros_like(joint)
        mask = joint > 0
        logt[mask] = np.log2(ratio[mask])
        mi = float(np.sum(joint[mask] * logt[mask]))
    return max(0.0, mi)


@dataclass
class Packet:
    node_id: str
    seq: int
    tx_ms: int
    entropy_hex: str
    rssi: float
    snr: float
    rx_ts: float


def parse_line(line: str) -> Optional[Packet]:
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) < 7 or parts[0].upper() != "NODE":
        return None
    try:
        return Packet(
            node_id=parts[1].upper(),
            seq=int(parts[2]),
            tx_ms=int(float(parts[3])),
            entropy_hex=parts[4],
            rssi=float(parts[5]),
            snr=float(parts[6]),
            rx_ts=time.time(),
        )
    except (ValueError, IndexError):
        return None


class SerialSource:
    def __init__(self, port: str, baud: int = 115200):
        if serial is None:
            raise RuntimeError("pyserial is required. pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.2)

    def read_lines(self):
        try:
            n = self.ser.in_waiting
            if n:
                chunk = self.ser.read(n).decode("utf-8", errors="ignore")
                for line in chunk.splitlines():
                    if line.strip():
                        yield line
        except Exception:
            return

    def close(self):
        self.ser.close()


class SimulatedSource:
    """Offline / bench mode when no radio is attached."""

    def __init__(self, node_id: str, seed: int):
        self.node_id = node_id
        self.rng = np.random.default_rng(seed)
        self.seq = 0
        self._next = time.time()

    def read_lines(self):
        now = time.time()
        if now < self._next:
            return
        self._next = now + SAMPLE_INTERVAL_S
        self.seq += 1
        entropy = f"{int(self.rng.integers(0, 2**32)):08x}"
        rssi = float(-95 + self.rng.normal(0, 3))
        snr = float(6 + self.rng.normal(0, 1.5))
        tx = int(now * 1000) % 1_000_000_000
        yield f"NODE,{self.node_id},{self.seq},{tx},{entropy},{rssi:.1f},{snr:.1f}"


def list_ports() -> None:
    if serial is None:
        print("pyserial not installed.")
        return
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        print(f"  {p.device:20s}  {p.description}")


def run(args: argparse.Namespace) -> None:
    print("=" * 80)
    print("COHERENCE MONITOR — LoRa Gate 4 / Gate 6 host")
    print("=" * 80)

    conn = init_db(args.db)
    sources = []
    if args.simulate or not args.port:
        print("Mode: simulated RF streams (no radio attached).")
        sources = [SimulatedSource("A", 1), SimulatedSource("B", 2)]
    else:
        sources.append(SerialSource(args.port, args.baud))
        if args.port_b:
            sources.append(SerialSource(args.port_b, args.baud))
        print(f"Mode: serial  port={args.port}  port_b={args.port_b}")

    streams: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=64))
    tx_times: Dict[str, Deque[int]] = defaultdict(lambda: deque(maxlen=64))
    last_rssi: Dict[str, float] = {}
    last_snr: Dict[str, float] = {}
    pkt_count = 0
    next_epoch = time.time() + SAMPLE_INTERVAL_S

    print("Listening. Ctrl+C to stop.\n")
    try:
        while True:
            for src in sources:
                for line in src.read_lines() or []:
                    pkt = parse_line(line)
                    if pkt is None:
                        continue
                    pkt_count += 1
                    streams[pkt.node_id].append(hash_to_unit(pkt.entropy_hex))
                    tx_times[pkt.node_id].append(pkt.tx_ms)
                    last_rssi[pkt.node_id] = pkt.rssi
                    last_snr[pkt.node_id] = pkt.snr
                    err = int(pkt.snr < -7 or pkt.rssi < -115)
                    conn.execute(
                        """
                        INSERT INTO lora_packets
                        (timestamp, node_id, rssi, snr, entropy_hash,
                         tx_timestamp, rx_timestamp, packet_error, seq)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            pkt.rx_ts,
                            pkt.node_id,
                            pkt.rssi,
                            pkt.snr,
                            pkt.entropy_hex,
                            pkt.tx_ms,
                            int(pkt.rx_ts * 1000),
                            err,
                            pkt.seq,
                        ),
                    )
                    conn.commit()
                    print(
                        f"  RX {pkt.node_id} seq={pkt.seq} RSSI={pkt.rssi:.1f} "
                        f"SNR={pkt.snr:.1f} hash={pkt.entropy_hex}"
                    )

            now = time.time()
            if now >= next_epoch:
                next_epoch = now + SAMPLE_INTERVAL_S
                nodes = sorted(streams.keys())
                mi = 0.0
                if len(nodes) >= 2:
                    mi = cross_mutual_information(streams[nodes[0]], streams[nodes[1]])
                # Gate 4 proxy: inter-packet tx timestamp jitter ratio
                jitter = float("nan")
                if nodes:
                    arr = np.diff(np.array(tx_times[nodes[0]], dtype=np.float64))
                    if arr.size >= 2:
                        jitter = float((arr.std() + 1e-9) / (arr.mean() + 1e-9))
                flag = int(mi > MI_SIGNAL_BITS)
                a = nodes[0] if nodes else None
                b = nodes[1] if len(nodes) > 1 else None
                conn.execute(
                    """
                    INSERT OR REPLACE INTO lora_epochs VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        now,
                        mi,
                        jitter,
                        last_rssi.get(a, float("nan")) if a else float("nan"),
                        last_rssi.get(b, float("nan")) if b else float("nan"),
                        last_snr.get(a, float("nan")) if a else float("nan"),
                        last_snr.get(b, float("nan")) if b else float("nan"),
                        pkt_count,
                        flag,
                    ),
                )
                conn.commit()
                status = "GATE6_SIGNAL" if flag else "NOMINAL"
                print(
                    f"[{time.strftime('%H:%M:%S')}] {status}  "
                    f"I(A;B)={mi:.4f} bits  jitter_ratio={jitter:.4f}  pkts={pkt_count}"
                )
                if mi < MI_NULL_BITS and len(nodes) >= 2:
                    print("  (below null-test threshold 0.01 bits)")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        for src in sources:
            if hasattr(src, "close"):
                src.close()
        conn.close()
        print("LoRa host closed.")


def main() -> None:
    p = argparse.ArgumentParser(description="LoRa Gate 4/6 host")
    p.add_argument("--db", default="coherence_ledger.db")
    p.add_argument("--port", default="", help="Serial device for node A / gateway")
    p.add_argument("--port-b", default="", help="Optional second serial device")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--list-ports", action="store_true")
    args = p.parse_args()
    if args.list_ports:
        list_ports()
        return
    run(args)


if __name__ == "__main__":
    main()
