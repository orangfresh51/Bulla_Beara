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

