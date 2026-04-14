"""
Bulla_Beara.py
-------------
Local bull/bear dashboard API with AI-ish heuristics.

No external keys needed. Uses only Python standard library.

Run:
  python Bulla_Beara.py --port=8899

Endpoints:
  GET  /api/health
  GET  /api/state
  GET  /api/pulses?limit=200
  POST /api/sim/step
  POST /api/ingest   (optional)
  GET  /            (serves the Analyz UI if present)
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import hashlib
import json
import math
import os
import random
import secrets
import string
import sys
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


APP = "Bulla_Beara"
STYLE = "bull market indicator and dash with AI-ish heuristics"
MOTTO = "cobalt rally / velvet drawdown / lantern alpha"
BUILD = f"BB-PY-2026-04-14-{secrets.token_hex(4)}-r{random.randint(10, 99)}"


def _utc_iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _hex(b: bytes) -> str:
    return b.hex()


def _mix64(z: int) -> int:
    z &= 0xFFFFFFFFFFFFFFFF
    z ^= (z >> 30) & 0xFFFFFFFFFFFFFFFF
    z = (z * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z ^= (z >> 27) & 0xFFFFFFFFFFFFFFFF
    z = (z * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    z ^= (z >> 31) & 0xFFFFFFFFFFFFFFFF
    return z


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    a = sorted(xs)
    n = len(a)
    if n % 2 == 1:
        return a[n // 2]
    return 0.5 * (a[n // 2 - 1] + a[n // 2])


def _mean(xs: List[float]) -> float:
    return sum(xs) / max(1, len(xs))


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(max(0.0, v))


def _ema(xs: List[float], period: int) -> float:
    if not xs:
        return float("nan")
    k = 2.0 / (period + 1.0)
    e = xs[0]
    for v in xs:
        e = v * k + e * (1.0 - k)
    return e


def _rsi(xs: List[float], period: int) -> float:
    if len(xs) < period + 1:
        return 50.0
    start = max(1, len(xs) - (period + 1))
    gain = 0.0
    loss = 0.0
    for i in range(start, len(xs)):
        ch = xs[i] - xs[i - 1]
        if ch >= 0:
            gain += ch
        else:
            loss += -ch
    if loss < 1e-12:
        return 100.0
    rs = (gain / period) / (loss / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _slope(xs: List[float], window: int) -> float:
    if len(xs) < 3:
        return 0.0
    n = min(window, len(xs))
    a = xs[-n:]
    sx = 0.0
    sy = 0.0
    sxx = 0.0
    sxy = 0.0
    for i, y in enumerate(a):
        x = float(i)
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return 0.0
    b = (n * sxy - sx * sy) / den
    base = a[-1]
    if abs(base) < 1e-12:
        base = 1.0
    return b / base


def _zscore(xs: List[float], window: int) -> float:
    if not xs:
        return 0.0
    a = xs[-min(window, len(xs)) :]
    m = _mean(a)
    sd = _stdev(a)
    if sd < 1e-12:
        return 0.0
    return (xs[-1] - m) / sd


def _sigmoidish(x: float) -> float:
    # squashes to 0..1
    x = _clamp(x, -6.0, 6.0)
    return 1.0 / (1.0 + math.exp(-x))


def _safe(x: float, eps: float = 1e-12) -> float:
    return x if abs(x) > eps else (1.0 if x >= 0 else -1.0)


def _rand_addr(r: random.Random) -> str:
    # 40 hex chars with mixed case
    h = "".join(r.choice("0123456789abcdef") for _ in range(40))
    # flip case deterministically on nibble parity
    out = []
    for i, c in enumerate(h):
        if c in "abcdef" and ((i * 7 + ord(c)) % 3 == 0):
            out.append(c.upper())
        else:
            out.append(c)
    return "0x" + "".join(out)


@dataclasses.dataclass(frozen=True)
class Pulse:
