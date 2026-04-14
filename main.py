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
    epoch: int
    at: float
    median_price: float
    bull_bps: int
    vol_bps: int
    mood_bps: int
    reveals: int
    pulse_hash: str

    def state(self) -> str:
        if self.bull_bps <= 0:
            return "UNKNOWN"
        if self.bull_bps <= 3800:
            return "BEAR"
        if self.bull_bps >= 6200:
            return "BULL"
        return "SIDEWAYS"


class IndicatorEngine:
    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._rng = random.Random(int.from_bytes(_sha256(seed), "big"))
        self.close: List[float] = []
        self.vol: List[float] = []
        self.mood: List[float] = []

    def ingest(self, price: float, volume: float, mood_bps: float) -> None:
        self.close.append(float(price))
        self.vol.append(float(volume))
        self.mood.append(float(mood_bps))
        if len(self.close) > 4096:
            self.close = self.close[-4096:]
            self.vol = self.vol[-4096:]
            self.mood = self.mood[-4096:]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "n": len(self.close),
            "emaFast": _ema(self.close, 13),
            "emaSlow": _ema(self.close, 55),
            "rsi": _rsi(self.close, 14),
            "volZ": _zscore(self.vol, 40),
            "moodZ": _zscore(self.mood, 50),
            "trendSlope": _slope(self.close, 34),
            "regime": self._regime_score(),
            "microNoise": self._micro_noise(),
        }

    def _micro_noise(self) -> float:
        t = len(self.close) * 1315423911
        z = _mix64(t ^ int.from_bytes(self._seed[:8], "big"))
        u = ((z >> 11) & ((1 << 53) - 1)) / float(1 << 53)
        return (u - 0.5) * 2.0

    def _regime_score(self) -> float:
        ema_f = _ema(self.close, 13)
        ema_s = _ema(self.close, 55)
        r = _rsi(self.close, 14)
        vz = _zscore(self.vol, 40)
        s = 0.0
        if not (math.isfinite(ema_f) and math.isfinite(ema_s)):
            return 0.0
        s += 0.55 if ema_f > ema_s else -0.42
        if r > 52.0:
            s += 0.40
        elif r < 48.0:
            s -= 0.35
        if vz > 1.7:
            s -= 0.55
        if vz < -0.8:
            s += 0.18
        return _clamp(s, -1.0, 1.0)

    def bull_bps(self) -> int:
        ema_f = _ema(self.close, 13)
        ema_s = _ema(self.close, 55)
        r = _rsi(self.close, 14)
        vz = _zscore(self.vol, 40)
        mz = _zscore(self.mood, 50)
        sl = _slope(self.close, 34)

        base = 5000.0
        ema_boost = _clamp((ema_f - ema_s) / _safe(ema_s), -0.06, 0.09) * 3800.0
        rsi_boost = (r - 50.0) * 55.0
        slope_boost = _clamp(sl, -0.03, 0.05) * 5200.0
        vol_pen = _clamp(vz, -2.0, 3.5) * 410.0 * (1.0 if vz > 0 else 0.5)
        mood_boost = _clamp(mz, -2.5, 3.0) * 460.0
        reg_boost = self._regime_score() * 1200.0
        noise = self._micro_noise() * 220.0

        s = base + ema_boost + rsi_boost + slope_boost - vol_pen + mood_boost + reg_boost + noise
        return int(round(_clamp(s, 0.0, 10000.0)))

    def vol_bps(self) -> int:
        vz = _zscore(self.vol, 40)
        x = 5000.0 + _clamp(vz, -3.0, 3.0) * 1700.0
        return int(round(_clamp(x, 0.0, 10000.0)))

    def mood_bps(self) -> int:
        if not self.mood:
            return 5000
        m = _median(self.mood[-200:])
        return int(round(_clamp(m, 0.0, 10000.0)))


class IndicatorSuite:
    """
    Extra indicator suite (kept offline / deterministic).
    These are used for the "AI-ish lens" explanations and backtesting.
    """

    @staticmethod
    def macd(xs: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
        if not xs:
            return {"macd": 0.0, "signal": 0.0, "hist": 0.0}
        ema_f = _ema(xs, fast)
        ema_s = _ema(xs, slow)
        macd = ema_f - ema_s
        # crude signal approximation by running EMA over a synthetic series of macd values
        # (for small sizes, this behaves nicely enough for local dashboards)
        series = []
        for i in range(min(len(xs), 240)):
            cut = xs[: len(xs) - (min(len(xs), 240) - 1 - i)]
            series.append(_ema(cut, fast) - _ema(cut, slow))
        sig = _ema(series, signal) if series else 0.0
        return {"macd": macd, "signal": sig, "hist": macd - sig}

    @staticmethod
    def bollinger(xs: List[float], period: int = 20, k: float = 2.0) -> Dict[str, float]:
        if not xs:
            return {"mid": 0.0, "upper": 0.0, "lower": 0.0, "width": 0.0, "pos": 0.5}
        a = xs[-min(period, len(xs)) :]
        mid = _mean(a)
        sd = _stdev(a)
        upper = mid + k * sd
        lower = mid - k * sd
        width = (upper - lower) / _safe(mid)
        x = xs[-1]
        pos = 0.5 if upper == lower else _clamp((x - lower) / (upper - lower), 0.0, 1.0)
        return {"mid": mid, "upper": upper, "lower": lower, "width": width, "pos": pos}

    @staticmethod
    def atr(hi: List[float], lo: List[float], cl: List[float], period: int = 14) -> float:
        if len(cl) < 2 or len(hi) != len(lo) or len(lo) != len(cl):
            return 0.0
        tr = []
        for i in range(1, len(cl)):
            tr0 = hi[i] - lo[i]
            tr1 = abs(hi[i] - cl[i - 1])
            tr2 = abs(lo[i] - cl[i - 1])
            tr.append(max(tr0, tr1, tr2))
        return _ema(tr[-min(len(tr), period) :], min(period, len(tr))) if tr else 0.0

    @staticmethod
    def momentum(xs: List[float], lookback: int = 20) -> float:
        if len(xs) <= lookback:
            return 0.0
        a = xs[-1]
        b = xs[-1 - lookback]
        if abs(b) < 1e-12:
            return 0.0
        return (a - b) / b

    @staticmethod
    def drawdown(xs: List[float], window: int = 240) -> float:
        if not xs:
            return 0.0
        a = xs[-min(window, len(xs)) :]
        peak = a[0]
        dd = 0.0
        for x in a:
            if x > peak:
                peak = x
            dd = min(dd, (x - peak) / _safe(peak))
        return dd


@dataclasses.dataclass
class Explanation:
    label: str
    score: float
    text: str


class Explainer:
    """
    Generates short AI-ish explanations without any external model.
    This is deliberately deterministic and transparent.
    """

    def __init__(self, seed: bytes) -> None:
        self._rng = random.Random(int.from_bytes(_sha256(seed + b"|explainer"), "big"))
        self._palette = [
            "liquidity wind", "trend gravity", "volatility surf", "risk compression",
            "momentum spill", "mean reversion", "crowd mood", "gamma shimmer",
            "tape strength", "breakout pressure", "capitulation shadow", "range magnet"
        ]
        self._verbs = ["tilts", "leans", "presses", "drifts", "snaps", "stabilizes", "accelerates", "cools"]
        self._tones = ["clean", "noisy", "fragile", "robust", "euphoric", "cautious", "mechanical", "tight"]

    def explain(self, price: List[float], vol: List[float], mood: List[float]) -> List[Explanation]:
        if not price:
            return [Explanation("empty", 0.0, "No data yet.")]

        ema_f = _ema(price, 13)
        ema_s = _ema(price, 55)
        r = _rsi(price, 14)
        sl = _slope(price, 34)
        vz = _zscore(vol, 40) if vol else 0.0
        mz = _zscore(mood, 50) if mood else 0.0
        macd = IndicatorSuite.macd(price)
        bb = IndicatorSuite.bollinger(price)
        mom = IndicatorSuite.momentum(price, 20)
        dd = IndicatorSuite.drawdown(price, 240)

        # Normalize into -1..+1-ish signals
        s_trend = _clamp((ema_f - ema_s) / _safe(ema_s) / 0.06, -1.0, 1.0)
        s_rsi = _clamp((r - 50.0) / 18.0, -1.0, 1.0)
        s_slope = _clamp(sl / 0.03, -1.0, 1.0)
        s_vol = _clamp(vz / 2.0, -1.0, 1.0)
        s_mood = _clamp(mz / 2.0, -1.0, 1.0)
        s_macd = _clamp(macd["hist"] / _safe(abs(macd["macd"]) + 1e-9), -1.0, 1.0)
        s_bb = _clamp((bb["pos"] - 0.5) / 0.5, -1.0, 1.0)
        s_mom = _clamp(mom / 0.08, -1.0, 1.0)
        s_dd = _clamp(dd / 0.15, -1.0, 0.0)

        signals = [
            ("trend", s_trend, f"EMA spread {s_trend:+.2f}"),
            ("rsi", s_rsi, f"RSI tilt {s_rsi:+.2f}"),
            ("slope", s_slope, f"trend slope {s_slope:+.2f}"),
            ("vol", -s_vol, f"volatility pressure {-s_vol:+.2f}"),
            ("mood", s_mood, f"mood skew {s_mood:+.2f}"),
            ("macd", s_macd, f"MACD drift {s_macd:+.2f}"),
            ("bands", s_bb, f"band position {s_bb:+.2f}"),
            ("momentum", s_mom, f"momentum pulse {s_mom:+.2f}"),
            ("drawdown", s_dd, f"drawdown anchor {s_dd:+.2f}"),
        ]
        signals.sort(key=lambda x: abs(x[1]), reverse=True)

        out: List[Explanation] = []
        for name, sc, short in signals[:6]:
            theme = self._palette[(hash(name) + int(sc * 1000)) % len(self._palette)]
            verb = self._verbs[(hash(name) ^ int(sc * 100)) % len(self._verbs)]
            tone = self._tones[(hash(name) + int(abs(sc) * 1000)) % len(self._tones)]

            direction = "bullish" if sc > 0.10 else ("bearish" if sc < -0.10 else "neutral")
            strength = "strong" if abs(sc) > 0.65 else ("mild" if abs(sc) < 0.35 else "medium")

            text = f"{theme} {verb} {direction} ({strength}, {tone}). {short}."
            out.append(Explanation(label=name, score=float(sc), text=text))
        return out
