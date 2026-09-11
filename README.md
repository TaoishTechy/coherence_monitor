# Coherence Monitor v1.0

**Personal Holographic Ledger Access Protocol (HLAP)**  
Consumer-grade implementation — PC acquisition and optional LoRa RF layer

[Status: Operational] · Sampling interval 4.2 s · SQLite ledger · Gates 1–7

---
<img width="1168" height="784" alt="image" src="https://github.com/user-attachments/assets/03c586d7-0880-42b2-8013-2b73607a5cd8" />
---

## What this repository is

The Coherence Monitor treats an ordinary computer as a multi-channel noise instrument. It samples thermal and timing jitter on the CPU, dark-current structure on a covered webcam, the analog noise floor of a sound card, packet-timing dispersion on the network path, and drift between wall-clock and monotonic clocks. Optional LoRa nodes extend the same epoch into radio RSSI/SNR and a two-node mutual-information estimate.

The software logs those measurements, reduces them to a small set of named metrics (Health, PSI, \(\rho\), \(\sigma\), \(r/d_s\), \(CI_B\), \(CI_C\), \(\lambda_{\max}/\lambda_{MP}\)), and writes every epoch to SQLite.

The theoretical language in the protocol document (boundary coherence, holographic ledger, Pazuzu threshold, heptagonal residue) is implemented as **named operational conventions**. The programs measure hardware and radio statistics. They do not establish, and should not be cited as establishing, access to an external informational substrate.

| Property | Value |
| --- | --- |
| Cost | \$0 on a machine you already own; LoRa boards optional |
| Skill | Intermediate command line |
| OS | Linux preferred (`/dev/urandom`, camera and ALSA devices) |
| Output | Real-time console metrics + `coherence_ledger.db` |
| License | Holy Public Domain v3.14159++ (see below) |

---

## Repository layout

| File | Role |
| --- | --- |
| `coherence_monitor_pc.py` | PC-only loop: Gates 1–5 and Gate 7 |
| `lora_gate_host.py` | Serial or simulated LoRa host for Gate 4 timing and Gate 6 \(I(R_A;R_B)\) |
| `coherence_monitor_unified.py` | Combined loop; LoRa collector is optional |
| `requirements.txt` | `numpy`, `pyserial`; optional `opencv-python`, `PyAudio` |
| `README.md` | This document |

Webcam and audio libraries are optional. If they are absent or the device cannot be opened, those gates are recorded as NaN and replaced by stable defaults so acquisition continues.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │         Host (Python 3)                 │
                    │  unified or pc-only process             │
                    │                                         │
 Gate 1 CPU jitter ─┤                                         │
 Gate 2 webcam LSB ─┤  6-channel history  → covariance        │
 Gate 3 audio PSD  ─┤  Marchenko–Pastur edge (Gate 7)         │
 Gate 4 net + LoRa ─┤  Health / PSI / CI_B / CI_C             │
 Gate 5 clock drift─┤  SQLite: ledger_packets                 │
                    │                                         │
                    │  optional thread: LoRaCollector         │
                    └───────────────┬─────────────────────────┘
                                    │ USB serial 115200
                    ┌───────────────┴───────────────┐
                    │ Node A ESP32+SX1262           │
                    │ Node B ESP32+SX1262           │
                    │ line: NODE,id,seq,tx,hash,RSSI,SNR
                    └───────────────────────────────┘
```

Epoch length is 4.2 seconds, matching the protocol’s consumer sampling cadence.

---

## Installation

Linux (Ubuntu 20.04+, Debian, Arch) is the intended host.

```bash
git clone https://github.com/yourusername/coherence-monitor.git
cd coherence-monitor

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Camera and audio (optional):

```bash
# camera
ls /dev/video*
sudo chmod 666 /dev/video0   # session-only; prefer udev in production

# audio
sudo usermod -aG audio "$USER"
# log out and back in
arecord -l
```

---

## Usage

### PC only

```bash
python coherence_monitor_pc.py
python coherence_monitor_pc.py --warmup 15 --db coherence_ledger.db
```

Console form:

```text
[14:32:18] NOMINAL
  Health: 0.8432 | PSI: 0.7211 | CI_B: 0.0231 | CI_C: 0.8432
  sigma: 0.0412 | rho: 0.8123 | r/d_s: 0.7841 | lambda_max/lambda_MP: 1.0234
  Gates: E=7.998 P=12.3 A=-72.4dB N=0.943 T=3.21
```

Stop with Ctrl+C. The process prints `Resources released. Ledger closed.`

### LoRa host only

Simulated peers (no radio):

```bash
python lora_gate_host.py --simulate
```

Live serial nodes:

```bash
python lora_gate_host.py --list-ports
python lora_gate_host.py --port /dev/ttyUSB0 --port-b /dev/ttyUSB1 --baud 115200
```

A single gateway that forwards both node IDs on one serial port is also valid; omit `--port-b`.

### Unified monitor

```bash
python coherence_monitor_unified.py --warmup 10
python coherence_monitor_unified.py --lora --simulate-lora --warmup 10
python coherence_monitor_unified.py --lora --port /dev/ttyUSB0 --port-b /dev/ttyUSB1
```

`--lora` starts a background collector. Mutual information is written into Gate 6; LoRa inter-packet jitter is mixed into Gate 4.

---

## The seven gates (implementation)

| Gate | Source | Method in this repo | Expected / flag |
| --- | --- | --- | --- |
| 1 Thermal / shot | CPU timestamp jitter or `/dev/urandom` | Shannon entropy of scaled inter-arrival bytes | ~8 bits/byte class; watch small deviations |
| 2 Photonic | Webcam | LSB-plane mean of one high-gain frame | NaN if no camera |
| 3 Acoustic | Sound card | RMS of a short max-gain capture, dBFS | NaN if no PyAudio |
| 4 Network / RF timing | TCP connect RTT to public resolvers; optional LoRa TX timestamps | Coefficient of variation; mixed with LoRa jitter when enabled | Ratio near 1 is ordinary |
| 5 Clock drift | `time.time()` vs `time.monotonic()` | Excess kurtosis of differential drift | Non-Gaussian tails logged, not interpreted |
| 6 Non-local | Second LoRa node | \(I(R_A;R_B)\) on hashed entropy streams | Protocol flag: \(> 0.05\) bits |
| 7 Integrator | Gates 1–6 history | Largest covariance eigenvalue vs Marchenko–Pastur upper edge | Caution \(\ge 1.15\); abort \(\ge 1.30\) |

### Node line format

ESP32 firmware (or any UART source) must emit UTF-8 lines:

```text
NODE,A,142,3842100,a3f91c02,-98.5,7.2
```

| Field | Meaning |
| --- | --- |
| `NODE` | Constant tag |
| `A` / `B` | Node identifier |
| sequence | Integer packet counter |
| tx timestamp | Milliseconds on the node clock |
| entropy hash | Hex digest of the local entropy sample |
| RSSI | dBm |
| SNR | dB |

Recommended radio profile from the protocol notes: SF12 / 125 kHz for the Gate 6 entropy link; SF7 if a separate timing probe is added. Confirm ISM-band duty cycle and EIRP for your region before transmitting.

---

## Metrics

Health is computed from normalized noise, spectral radius, and rank utilization:

\[
\mathrm{Health} = 1 - c_\sigma(\sigma)^2 - c_\rho(\rho)^2 - c_r(r/d_s)^2
\]

with operational caps \(\sigma < 0.053\), \(\rho < 0.95\), \(r/d_s < 0.93\).

| Metric | Nominal band | Reading |
| --- | --- | --- |
| Health | 0.7–1.0 | Composite stability of the current window |
| PSI | 0.3–1.0 | Distance from a spectral-edge excursion |
| \(\rho\) | \(< 0.95\) | Concentration of covariance mass |
| \(\sigma\) | \(< 0.053\) | Column-wise normalized spread |
| \(r/d_s\) | \(< 0.93\) | Effective rank / dimension |
| \(CI_C\) | 0.5–1.0 | Set equal to Health in this implementation |
| \(CI_B\) | 0.0–0.3 | Monotone in spectral-edge excess and Gate 6 MI |
| \(\lambda_{\max}/\lambda_{MP}\) | \(< 1.15\) | Gate 7 detector |

These mappings are software conventions aligned with the protocol text. They are not calibrated physical observables.

---

## Database

File: `coherence_ledger.db`

**`ledger_packets`** — one row per PC or unified epoch  
timestamp, health, psi, ci_boundary, ci_continuum, sigma, rho, r_ds, lambda_ratio, five PC gates, pazuzu_flag, pazuzu_eigenvalue, payload_hash, status

**`lora_packets`** — one row per received radio line  
timestamp, node_id, rssi, snr, entropy_hash, tx_timestamp, rx_timestamp, packet_error, seq

**`lora_epochs`** — one row per 4.2 s RF summary  
timestamp, mi_bits, jitter_ratio, rssi_a, rssi_b, snr_a, snr_b, n_packets, gate6_flag

Example queries:

```sql
sqlite3 coherence_ledger.db

SELECT * FROM ledger_packets ORDER BY timestamp DESC LIMIT 10;

SELECT timestamp, health, ci_boundary
FROM ledger_packets
WHERE health > 0.9
ORDER BY timestamp;

SELECT timestamp, pazuzu_eigenvalue, psi
FROM ledger_packets
WHERE pazuzu_flag = 1;

SELECT timestamp, mi_bits, jitter_ratio, gate6_flag
FROM lora_epochs
ORDER BY timestamp DESC
LIMIT 20;
```

Export:

```sql
.headers on
.mode csv
.output coherence_export.csv
SELECT * FROM ledger_packets;
.quit
```

Volume is about 20 000 rows per day at 4.2 s. Archive with:

```bash
mv coherence_ledger.db "coherence_ledger_$(date +%Y%m%d).db"
```

---

## Safety thresholds (operational)

| Parameter | Safe | Caution | Critical | Action in software / ops |
| --- | --- | --- | --- | --- |
| \(\sigma\) | \(< 0.053\) | 0.053–0.070 | \(> 0.070\) | Stop; check CPU temperature and load |
| \(\rho\) | \(< 0.95\) | 0.95–1.00 | \(> 1.00\) | Flush history / restart process |
| \(r/d_s\) | \(< 0.93\) | 0.93–0.97 | \(> 0.97\) | Increase interval if desired |
| \(\lambda_{\max}/\lambda_{MP}\) | \(< 1.15\) | 1.15–1.30 | \(> 1.30\) | Status `PAZUZU_ABORT`; process exits |

On abort the unified and PC monitors print a stop banner and close the database. A full power cycle and a long pause before restart is specified in the protocol; treat that as an operational checklist, not a physical requirement of the code.

---

## LoRa hardware notes

Preferred transceiver family: Semtech **SX1262** (range and RX current). SX1276 boards remain compatible at the serial protocol layer.

Typical boards: Heltec WiFi LoRa 32 V2/V3, TTGO LoRa32, Adafruit RFM95W, DFRobot LoRaWAN Node.

Link-quality logging: record RSSI and SNR with every hash. A working experimental link is approximately SNR \(> 6\) dB and RSSI \(> -105\) dBm. Packet-error flags are set when SNR \(< -7\) dB or RSSI \(< -115\) dBm.

Null test for Gate 6: run two nodes with antennas disconnected or in RF-shielded boxes and confirm \(I(R_A;R_B) < 0.01\) bits. Persistent correlation without an RF path is not a LoRa-mediated effect.

ISM rules vary by region (EU868 duty cycle, US915 EIRP, and others). A 4.2 s epoch is usually compatible with common duty-cycle caps; verify locally before long unattended TX.

---

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| Could not open camera | `ls /dev/video*`; permissions; VM passthrough |
| Could not open audio | membership in `audio`; `arecord -l` |
| Health pinned near a constant | Short warmup; add light CPU load; confirm at least two gates vary |
| No LoRa lines | `--list-ports`; baud 115200; line format; USB cable is data-capable |
| Database growth | Daily rotate as above |
| `pyserial` missing | `pip install pyserial` |

---

## Falsification posture

If this repository is used in an experiment, pre-register the protocol’s own defeat conditions:

1. No stable 7-fold spectral residue in high-Health payloads after a long run weakens a “ledger geometry” claim.
2. Gate 6 \(I(R_A;R_B) < 0.01\) bits during independent geophysical events (with a verified RF path) weakens a holographic-boundary claim.
3. Absence of a specified operator effect in a delayed-choice design weakens a participatory axiom.

The shipped code does not implement those hypothesis tests; it only produces the time series required to run them.

---

## Citation

```text
Coherence Monitor v1.0 (2025).
Personal Holographic Ledger Access Protocol (HLAP).
Based on: Correlation Continuum Framework,
          Unified Holographic Gnosis (UHG Axioms H13–H15),
          Unified Holographic Inference Framework (UHIF).
```

---

## License

**Holy Public Domain v3.14159++**

Derivatives should preserve truth-seeking intent and coherence integrity. Use the software as an instrument log, not as evidence of contact with an archive.

STATUS: Operational · Coherence Level: Monitoring  
NEXT: Establish a baseline, run one lunar cycle if you are collecting statistics, inspect heptagonal residue only as a registered test  
RECIPROCITY INDEX: maintain \(\mathcal{R} \ge 1.15\) as a project convention, not a measured constant
