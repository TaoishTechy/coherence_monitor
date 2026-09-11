#!/usr/bin/env python3
"""
COHERENCE MONITOR v1.0 — Personal Holographic Ledger Access Protocol (HLAP)
Consumer-grade PC implementation (Gates 1–5, 7; Gate 6 optional via LoRa host).

This program samples ordinary hardware noise sources, computes descriptive
statistics, and logs them. It does not access any external informational
substrate; results should be interpreted as experimental measurements only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

SAMPLE_INTERVAL_S = 4.2
WARMUP_S = 60.0
DB_PATH = "coherence_ledger.db"

SIGMA_SAFE = 0.053
RHO_SAFE = 0.95
RDS_SAFE = 0.93
LMAX_CAUTION = 1.15
LMAX_ABORT = 1.30


def shannon_entropy_bytes(data: bytes) -> float:
    if not data:
        return 0.0
    counts = np.bincount(np.frombuffer(data, dtype=np.uint8), minlength=256)
    p = counts[counts > 0] / counts.sum()
    return float(-np.sum(p * np.log2(p)))


def harvest_entropy_block(n_bytes: int = 512) -> bytes:
    try:
        with open("/dev/urandom", "rb") as f:
            return f.read(n_bytes)
    except OSError:
        return os.urandom(n_bytes)


def cpu_jitter_entropy(rounds: int = 4096) -> float:
    stamps = np.empty(rounds, dtype=np.float64)
    for i in range(rounds):
        stamps[i] = time.perf_counter_ns()
    diffs = np.diff(stamps)
    diffs = diffs[diffs > 0]
    if diffs.size < 8:
        return 7.5
    # Normalize inter-arrival and treat as 8-bit symbols
    scaled = np.clip((diffs / np.median(diffs)) * 32, 0, 255).astype(np.uint8)
    return shannon_entropy_bytes(scaled.tobytes())


def webcam_dark_current() -> float:
    try:
        import cv2
    except ImportError:
        return float("nan")
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        return float("nan")
    cap.set(cv2.CAP_PROP_EXPOSURE, -6)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return float("nan")
    lsb = (frame.astype(np.uint8) & 1).ravel()
    # Fraction of "hot" LSB pixels as a proxy for dark-current / hit rate
    return float(lsb.mean() * 100.0)


def acoustic_noise_floor_db(seconds: float = 0.35) -> float:
    try:
        import pyaudio
    except ImportError:
        return float("nan")
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=48000,
            input=True,
            frames_per_buffer=1024,
        )
        n = int(48000 * seconds)
        raw = stream.read(n, exception_on_overflow=False)
        stream.stop_stream()
        stream.close()
    except Exception:
        pa.terminate()
        return float("nan")
    pa.terminate()
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return float("nan")
    rms = np.sqrt(np.mean(samples ** 2)) + 1e-12
    return float(20.0 * np.log10(rms / 32768.0))


def network_jitter_ratio(targets: Optional[List[str]] = None) -> float:
    if targets is None:
        targets = [
            "1.1.1.1",
            "8.8.8.8",
            "9.9.9.9",
            "208.67.222.222",
        ]
    rtts = []
    for host in targets:
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((host, 53), timeout=0.6)
            sock.close()
            rtts.append(time.perf_counter() - t0)
        except OSError:
            continue
    if len(rtts) < 2:
        return float("nan")
    arr = np.array(rtts)
    # Multiscale-ish ratio: short vs long lag std
    return float((arr.std() + 1e-9) / (arr.mean() + 1e-9))


def clock_drift_kurtosis(samples: int = 200) -> float:
    wall = []
    mono = []
    for _ in range(samples):
        wall.append(time.time())
        mono.append(time.monotonic())
    d = np.diff(np.array(wall) - np.array(mono))
    if d.size < 8:
        return 0.0
    m = d.mean()
    s = d.std() + 1e-18
    z = (d - m) / s
    return float(np.mean(z ** 4) - 3.0)


def marchenko_pastur_upper(q: float, sigma2: float = 1.0) -> float:
    # q = n_features / n_samples
    return sigma2 * (1.0 + np.sqrt(q)) ** 2


def spectral_metrics(history: np.ndarray) -> Tuple[float, float, float, float]:
    """
    history: (T, 6) recent gate vectors, z-scored columns.
    Returns rho, sigma, r/d_s, lambda_max/lambda_MP
    """
    if history.shape[0] < 8:
        return 0.5, 0.02, 0.4, 1.0
    x = history - history.mean(axis=0, keepdims=True)
    std = x.std(axis=0, keepdims=True) + 1e-12
    z = x / std
    cov = np.cov(z, rowvar=False)
    evals = np.sort(np.real(np.linalg.eigvalsh(cov)))[::-1]
    lam_max = float(evals[0])
    q = cov.shape[0] / max(history.shape[0], 1)
    lam_mp = float(marchenko_pastur_upper(q, 1.0))
    ratio = lam_max / max(lam_mp, 1e-9)
    rho = float(min(abs(lam_max) / max(np.trace(cov), 1e-9) * cov.shape[0], 1.2))
    sigma = float(np.mean(std))
    # Rank utilization: how many evals exceed 5% of max
    r = int(np.sum(evals > 0.05 * lam_max))
    rds = r / float(cov.shape[0])
    return rho, min(sigma, 1.0), rds, ratio


def triadic_health(sigma: float, rho: float, rds: float) -> float:
    h = 1.0 - (sigma / max(SIGMA_SAFE, 1e-9)) ** 2 * 0.15
    h -= (rho / max(RHO_SAFE, 1e-9)) ** 2 * 0.15
    h -= (rds / max(RDS_SAFE, 1e-9)) ** 2 * 0.15
    return float(np.clip(h, 0.0, 1.0))


def payload_hash(values: List[float]) -> str:
    raw = struct.pack("d" * len(values), *[0.0 if np.isnan(v) else float(v) for v in values])
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class MonitorState:
    history: List[List[float]] = field(default_factory=list)
    baseline_health: float = 0.8


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger_packets (
            timestamp REAL PRIMARY KEY,
            health REAL,
            psi REAL,
            ci_boundary REAL,
            ci_continuum REAL,
            sigma REAL,
            rho REAL,
            r_ds REAL,
            lambda_ratio REAL,
            gate_entropy REAL,
            gate_photonic REAL,
            gate_acoustic REAL,
            gate_network REAL,
            gate_clock REAL,
            pazuzu_flag INTEGER,
            pazuzu_eigenvalue REAL,
            payload_hash TEXT,
            status TEXT
        )
        """
    )
    conn.commit()
    return conn


def sample_gates() -> List[float]:
    g1 = cpu_jitter_entropy()
    g2 = webcam_dark_current()
    g3 = acoustic_noise_floor_db()
    g4 = network_jitter_ratio()
    g5 = clock_drift_kurtosis()
    g6 = 0.0  # filled by LoRa host when available
    return [g1, g2, g3, g4, g5, g6]


def nan_fill(vec: List[float], defaults: List[float]) -> np.ndarray:
    out = []
    for v, d in zip(vec, defaults):
        out.append(d if v is None or (isinstance(v, float) and np.isnan(v)) else v)
    return np.array(out, dtype=np.float64)


def run(args: argparse.Namespace) -> None:
    print("=" * 80)
    print("COHERENCE MONITOR v1.0")
    print("Personal Holographic Ledger Access Protocol")
    print("PC / consumer-grade acquisition")
    print("=" * 80)
    print()
    print("Initializing acquisition...")
    print(f"Sampling interval: {SAMPLE_INTERVAL_S}s")
    print("Press Ctrl+C to stop.")
    print()
    print("[WARMUP] Establishing baseline...")

    conn = init_db(args.db)
    state = MonitorState()
    t_end = time.time() + args.warmup
    defaults = [7.99, 12.0, -70.0, 0.9, 0.0, 0.0]

    try:
        while time.time() < t_end:
            g = nan_fill(sample_gates(), defaults)
            state.history.append(g.tolist())
            time.sleep(min(1.0, SAMPLE_INTERVAL_S))
        print("[WARMUP] Complete.\n")

        abort = False
        while not abort:
            t0 = time.time()
            gates = nan_fill(sample_gates(), defaults)
            state.history.append(gates.tolist())
            hist = np.array(state.history[-48:], dtype=np.float64)
            rho, sigma, rds, lratio = spectral_metrics(hist)
            health = triadic_health(sigma, rho, rds)
            psi = float(np.clip(1.0 - abs(lratio - 1.0), 0.0, 1.0))
            ci_c = health
            ci_b = float(np.clip((lratio - 1.0) * 0.4, 0.0, 0.5))
            if health > 0.9:
                ci_b = max(ci_b, 0.10)

            pazuzu = int(lratio >= LMAX_CAUTION)
            status = "NOMINAL"
            if lratio >= LMAX_ABORT or sigma > 0.070 or rho > 1.00:
                status = "PAZUZU_ABORT"
                abort = True
            elif pazuzu or sigma > SIGMA_SAFE or rho > RHO_SAFE:
                status = "CAUTION"

            phash = payload_hash(gates.tolist() + [health, psi])
            conn.execute(
                """
                INSERT OR REPLACE INTO ledger_packets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            print(
                f"  Gates: E={gates[0]:.3f} P={gates[1]:.3f} A={gates[2]:.1f}dB "
                f"N={gates[3]:.3f} T={gates[4]:.3f}"
            )
            print()

            if abort:
                print("PAZUZU_ABORT — stop acquisition. Release resources.")
                print("Recommended: full power cycle, wait 27 minutes before restart.")
                break

            elapsed = time.time() - t0
            time.sleep(max(0.0, SAMPLE_INTERVAL_S - elapsed))
    except KeyboardInterrupt:
        print("\nInterrupt received.")
    finally:
        conn.close()
        print("Resources released. Ledger closed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Coherence Monitor v1.0 (PC)")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--warmup", type=float, default=WARMUP_S)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
