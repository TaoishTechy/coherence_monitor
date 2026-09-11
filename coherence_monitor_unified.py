#!/usr/bin/env python3
"""
Unified Coherence Monitor v1.0

Runs the PC seven-gate loop and optionally merges LoRa Gate 6 mutual
information plus Gate 4 RF timing from:
  - live serial nodes (ESP32 + SX1262/SX1276), or
  - simulated RF peers for bench testing.

Usage:
  python coherence_monitor_unified.py
  python coherence_monitor_unified.py --lora --simulate-lora
  python coherence_monitor_unified.py --lora --port /dev/ttyUSB0
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

import numpy as np

from coherence_monitor_pc import (
    LMAX_ABORT,
    LMAX_CAUTION,
    SAMPLE_INTERVAL_S,
    WARMUP_S,
    MonitorState,
    clock_drift_kurtosis,
    cpu_jitter_entropy,
    init_db as init_pc_db,
    nan_fill,
    network_jitter_ratio,
    payload_hash,
    spectral_metrics,
    triadic_health,
    webcam_dark_current,
    acoustic_noise_floor_db,
)
from lora_gate_host import (
    MI_SIGNAL_BITS,
    Packet,
    SimulatedSource,
    SerialSource,
    cross_mutual_information,
    hash_to_unit,
    init_db as init_lora_db,
    parse_line,
)


class LoRaCollector(threading.Thread):
    def __init__(self, port: str, port_b: str, baud: int, simulate: bool):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.mi = 0.0
        self.jitter = float("nan")
        self.last_rssi_a = float("nan")
        self.last_snr_a = float("nan")
        self.pkt_count = 0
        self.simulate = simulate
        self.port = port
        self.port_b = port_b
        self.baud = baud
        self.streams: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=48))
        self.tx_times: Dict[str, Deque[int]] = defaultdict(lambda: deque(maxlen=48))

    def snapshot(self):
        with self.lock:
            return self.mi, self.jitter, self.last_rssi_a, self.last_snr_a, self.pkt_count

    def _ingest(self, pkt: Packet) -> None:
        with self.lock:
            self.pkt_count += 1
            self.streams[pkt.node_id].append(hash_to_unit(pkt.entropy_hex))
            self.tx_times[pkt.node_id].append(pkt.tx_ms)
            if pkt.node_id == "A":
                self.last_rssi_a = pkt.rssi
                self.last_snr_a = pkt.snr
            nodes = sorted(self.streams.keys())
            if len(nodes) >= 2:
                self.mi = cross_mutual_information(
                    self.streams[nodes[0]], self.streams[nodes[1]]
                )
            if nodes:
                arr = np.diff(np.array(self.tx_times[nodes[0]], dtype=np.float64))
                if arr.size >= 2:
                    self.jitter = float((arr.std() + 1e-9) / (arr.mean() + 1e-9))

    def run(self) -> None:
        sources = []
        try:
            if self.simulate or not self.port:
                sources = [SimulatedSource("A", 11), SimulatedSource("B", 29)]
            else:
                sources.append(SerialSource(self.port, self.baud))
                if self.port_b:
                    sources.append(SerialSource(self.port_b, self.baud))
            while not self.stop_flag.is_set():
                for src in sources:
                    for line in src.read_lines() or []:
                        pkt = parse_line(line)
                        if pkt:
                            self._ingest(pkt)
                time.sleep(0.05)
        finally:
            for src in sources:
                if hasattr(src, "close"):
                    src.close()


def sample_pc_gates():
    return [
        cpu_jitter_entropy(),
        webcam_dark_current(),
        acoustic_noise_floor_db(),
        network_jitter_ratio(),
        clock_drift_kurtosis(),
        0.0,
    ]


def run(args: argparse.Namespace) -> None:
    print("=" * 80)
    print("COHERENCE MONITOR v1.0 — UNIFIED")
    print("PC gates + optional LoRa Gate 4 / Gate 6")
    print("=" * 80)
    print(f"LoRa enabled: {args.lora}   simulate: {args.simulate_lora}")
    print()

    conn = init_pc_db(args.db)
    init_lora_db(args.db)

    collector: Optional[LoRaCollector] = None
    if args.lora:
        collector = LoRaCollector(args.port, args.port_b, args.baud, args.simulate_lora)
        collector.start()
        print("LoRa collector thread started.")

    state = MonitorState()
    defaults = [7.99, 12.0, -70.0, 0.9, 0.0, 0.0]
    print("[WARMUP] Establishing baseline...")
    t_end = time.time() + args.warmup
    try:
        while time.time() < t_end:
            g = nan_fill(sample_pc_gates(), defaults)
            if collector:
                mi, jit, *_ = collector.snapshot()
                g[5] = mi
                if not np.isnan(jit):
                    g[3] = 0.5 * g[3] + 0.5 * jit
            state.history.append(g.tolist())
            time.sleep(min(1.0, SAMPLE_INTERVAL_S))
        print("[WARMUP] Complete.\n")

        abort = False
        while not abort:
            t0 = time.time()
            gates = nan_fill(sample_pc_gates(), defaults)
            mi = 0.0
            jit = float("nan")
            rssi = float("nan")
            snr = float("nan")
            pkts = 0
            if collector:
                mi, jit, rssi, snr, pkts = collector.snapshot()
                gates[5] = mi
                if not np.isnan(jit):
                    gates[3] = 0.5 * gates[3] + 0.5 * jit
            state.history.append(gates.tolist())
            hist = np.array(state.history[-48:], dtype=np.float64)
            rho, sigma, rds, lratio = spectral_metrics(hist)
            health = triadic_health(sigma, rho, rds)
            psi = float(np.clip(1.0 - abs(lratio - 1.0), 0.0, 1.0))
            ci_c = health
            ci_b = float(np.clip((lratio - 1.0) * 0.4 + max(0.0, mi - 0.02) * 2.0, 0.0, 0.5))

            pazuzu = int(lratio >= LMAX_CAUTION)
            status = "NOMINAL"
            if lratio >= LMAX_ABORT or sigma > 0.070 or rho > 1.00:
                status = "PAZUZU_ABORT"
                abort = True
            elif pazuzu or sigma > 0.053 or rho > 0.95:
                status = "CAUTION"
            if mi > MI_SIGNAL_BITS:
                status = "GATE6_SIGNAL" if status == "NOMINAL" else status + "+GATE6"

            phash = payload_hash(gates.tolist() + [health, psi, mi])
            conn.execute(
                """
                INSERT OR REPLACE INTO ledger_packets VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    t0,
                    health,
                    psi,
                    ci_b,
                    ci_c,
                    sigma,
                    rho,
                    rds,
                    lratio,
                    float(gates[0]),
                    float(gates[1]),
                    float(gates[2]),
                    float(gates[3]),
                    float(gates[4]),
                    pazuzu,
                    lratio,
                    phash,
                    status,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO lora_epochs VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    t0,
                    mi,
                    jit,
                    rssi,
                    float("nan"),
                    snr,
                    float("nan"),
                    pkts,
                    int(mi > MI_SIGNAL_BITS),
                ),
            )
            conn.commit()

            ts = time.strftime("%H:%M:%S", time.localtime(t0))
            print(f"[{ts}] {status}")
            print(
                f"  Health: {health:.4f} | PSI: {psi:.4f} | CI_B: {ci_b:.4f} | CI_C: {ci_c:.4f}"
            )
            print(
                f"  sigma: {sigma:.4f} | rho: {rho:.4f} | r/d_s: {rds:.4f} | "
                f"lambda_max/lambda_MP: {lratio:.4f}"
            )
            extra = ""
            if args.lora:
                extra = f" | I(A;B)={mi:.4f} bits | RSSI={rssi:.1f} SNR={snr:.1f}"
            print(
                f"  Gates: E={gates[0]:.3f} P={gates[1]:.3f} A={gates[2]:.1f}dB "
                f"N={gates[3]:.3f} T={gates[4]:.3f} NL={gates[5]:.4f}{extra}"
            )
            print()

            if abort:
                print("PAZUZU_ABORT — stop acquisition.")
                break
            time.sleep(max(0.0, SAMPLE_INTERVAL_S - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nInterrupt received.")
    finally:
        if collector:
            collector.stop_flag.set()
            collector.join(timeout=2.0)
        conn.close()
        print("Resources released. Ledger closed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Unified Coherence Monitor")
    p.add_argument("--db", default="coherence_ledger.db")
    p.add_argument("--warmup", type=float, default=min(WARMUP_S, 15.0))
    p.add_argument("--lora", action="store_true", help="Enable Gate 4/6 LoRa collector")
    p.add_argument("--simulate-lora", action="store_true")
    p.add_argument("--port", default="")
    p.add_argument("--port-b", default="")
    p.add_argument("--baud", type=int, default=115200)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
