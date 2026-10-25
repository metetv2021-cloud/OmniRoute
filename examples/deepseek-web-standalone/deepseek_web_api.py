#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek Web -> OpenAI-compatible API, as one standalone Python script.

A port of OmniRoute's ``deepseek-web`` provider (open-sse/executors/deepseek-web.ts
and the helpers it uses). `login` reuses the chat.deepseek.com login you already have
in Google Chrome, Microsoft Edge, Brave or another Chromium-based browser, or opens a
normal browser window for you to sign in; the script then serves an OpenAI-compatible
API backed by your DeepSeek web account:

    POST /v1/chat/completions   streaming and non-streaming, reasoning
                                ("reasoning_content"), web search with sources,
                                tool calling, image input
    GET  /v1/models

Requirements: Python 3.9+ and a Chromium-based browser. No pip packages.

Windows quick start (cmd or PowerShell, in this folder):

    py deepseek_web_api.py login     <- reuses your browser's DeepSeek login, or opens a window
    py deepseek_web_api.py           <- serves http://127.0.0.1:20129/v1

Point any OpenAI-compatible client at base_url http://127.0.0.1:20129/v1 with any
API key (unless you start the server with --api-key). Run with --help for options.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as _dt
import glob
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

__version__ = "1.0.0"

# ════════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════════

DEEPSEEK_BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
CONTINUE_PATH = "/api/v0/chat/continue"
UPLOAD_PATH = "/api/v0/file/upload_file"
FETCH_FILES_PATH = "/api/v0/file/fetch_files"

# What the chat.deepseek.com web client sent at the time of writing. A pasted "Copy as
# cURL" (login --manual) stores the live values instead, and a login read from a browser
# gets a user agent that matches that browser's version.
DEFAULT_CLIENT_VERSION = "2.7.0"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 20129
APP_NAME = "DeepSeekWebAPI"

ACCESS_TOKEN_TTL = 3600  # seconds, same as OmniRoute
AUTH_ERROR_CODES = {40001, 40002, 40003}
MAX_CONTINUES = 4  # automatic "continue" rounds when DeepSeek stops a long answer early
DEFAULT_AUTO_HISTORY_WINDOW = 20  # OmniRoute #10527
MAX_REQUEST_BYTES = 100 * 1024 * 1024

# The model ids OmniRoute exposes for deepseek-web. Any other name works too: the
# options are derived from substrings (pro/expert, think/r1/reason, search).
MODEL_IDS = [
    "deepseek-v4-flash",
    "deepseek-v4-flash-think",
    "deepseek-v4-flash-search",
    "deepseek-v4-flash-think-search",
    "deepseek-v4-pro",
    "deepseek-v4-pro-think",
    "deepseek-v4-pro-search",
    "deepseek-v4-pro-think-search",
    "deepseek-chat",
    "deepseek-reasoner",
    "DeepSeek-R1",
    "DeepSeek-R1-Search",
    "DeepSeek-V3.2",
    "DeepSeek-Search",
]

# ════════════════════════════════════════════════════════════════════════════
# Logging
# ════════════════════════════════════════════════════════════════════════════

_log_lock = threading.Lock()
_verbose = False


def log(message: str, level: str = "INFO") -> None:
    stamp = time.strftime("%H:%M:%S")
    with _log_lock:
        try:
            sys.stderr.write(f"[{stamp}] {level:<5} {message}\n")
            sys.stderr.flush()
        except (OSError, ValueError, AttributeError):
            pass


def debug(message: str) -> None:
    if _verbose:
        log(message, "DEBUG")


def say(message: str = "") -> None:
    """User-facing output for the CLI commands."""
    try:
        print(message, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


# ════════════════════════════════════════════════════════════════════════════
# Errors
# ════════════════════════════════════════════════════════════════════════════


class UpstreamError(Exception):
    """A failure talking to chat.deepseek.com, with the HTTP status to report."""

    status = 502
    error_type = "upstream_error"

    def __init__(self, message: str, status: Optional[int] = None, code: Any = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.code = code


class AuthError(UpstreamError):
    status = 401
    error_type = "authentication_error"


class RateLimitError(UpstreamError):
    status = 429
    error_type = "rate_limit_error"


class WafError(UpstreamError):
    status = 503
    error_type = "upstream_blocked"


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PowError(UpstreamError):
    pass


class ImageError(UpstreamError):
    error_type = "invalid_request_error"


class LoginError(Exception):
    pass


# ════════════════════════════════════════════════════════════════════════════
# Credential storage
# ════════════════════════════════════════════════════════════════════════════


def app_dir() -> Path:
    """Per-user data folder: %LOCALAPPDATA%\\DeepSeekWebAPI on Windows."""
    override = os.environ.get("DEEPSEEK_WEB_API_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / APP_NAME if base else Path.home() / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "deepseek-web-api"


def credentials_path() -> Path:
    return app_dir() / "credentials.json"


def browser_profile_dir() -> Path:
    return app_dir() / "browser-profile"


class CredentialStore:
    """credentials.json, re-read automatically when another process (``login``) rewrites it."""

    def __init__(self, path: Path, env_token: Optional[str] = None):
        self.path = path
        self.env_token = env_token
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._stamp: Any = None

    def _file_creds(self) -> Dict[str, Any]:
        with self._lock:
            try:
                st = self.path.stat()
                stamp: Any = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            if stamp != self._stamp:
                self._cache = self._read() if stamp else {}
                self._stamp = stamp
            return dict(self._cache)

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            log(f"Could not read {self.path}: {exc}", "WARN")
            return {}

    def get(self) -> Dict[str, Any]:
        creds = self._file_creds()
        if self.env_token:
            creds["token"] = self.env_token
        return creds

    def save(self, creds: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            data = json.dumps(creds, indent=2, ensure_ascii=False)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            for attempt in range(5):  # antivirus scanners briefly lock new files on Windows
                try:
                    os.replace(str(tmp), str(self.path))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
            self._cache = dict(creds)
            try:
                st = self.path.stat()
                self._stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._stamp = None

    def update(self, **fields: Any) -> None:
        creds = self._file_creds()
        creds.update(fields)
        self.save(creds)

    def delete(self) -> bool:
        with self._lock:
            self._cache, self._stamp = {}, None
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False


_TOKEN_CHARS_RE = re.compile(r"[A-Za-z0-9+/=._\-]{16,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9+/=._\-]{16,})")


def extract_user_token(raw: Any) -> Optional[str]:
    """Accept the token itself, the localStorage JSON wrapper ({"value": "..."}),
    a "Bearer ..." header, or a whole "Copy as cURL" command."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get("value")
            return value.strip() if isinstance(value, str) and value.strip() else None
    match = _BEARER_RE.search(text)
    if match:
        return match.group(1)
    text = text.strip("\"'")
    return text if _TOKEN_CHARS_RE.fullmatch(text) else None


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "(none)"
    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)" if len(value) > 12 else "***"


# ════════════════════════════════════════════════════════════════════════════
# Proof of work: DeepSeekHashV1
#
# DeepSeekHashV1 is SHA3-256's sponge with KECCAK-p[1600, 23] (rounds 1..23 of the
# 24 FIPS 202 rounds), per OmniRoute's clean-room reference
# (open-sse/lib/deepseek-pow-hash.js). hashlib can't change the round count and a
# scalar pure-Python Keccak manages ~3k hashes/s, far too slow for a 144k
# challenge. So the search is bit-sliced: every 64-bit lane holds the same lane of
# 1024 candidate nonces side by side in one big Python int, and each XOR/AND/shift
# processes all of them at once (~250k hashes/s, about 0.3 s for a typical challenge).
# ════════════════════════════════════════════════════════════════════════════

POW_ALGORITHM = "DeepSeekHashV1"
POW_MAX_DIFFICULTY = 2_000_000
_M64 = (1 << 64) - 1
_RATE = 136  # SHA3-256 rate in bytes
_POW_BATCH = 1024

_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
# Rotation offsets indexed by lane x + 5y, and the pi step's destination lane.
_KECCAK_ROT = (0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14)
_KECCAK_PI = tuple((i // 5) + 5 * ((2 * (i % 5) + 3 * (i // 5)) % 5) for i in range(25))


def _keccak_p1600(lanes: List[int], rounds: int = 23) -> List[int]:
    """Scalar KECCAK-p[1600, rounds] (the last `rounds` rounds). Used for one-off hashes."""
    a = list(lanes)
    for rnd in range(24 - rounds, 24):
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x + 4) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64) for x in range(5)]
        b = [0] * 25
        for i in range(25):
            v = a[i] ^ d[i % 5]
            n = _KECCAK_ROT[i]
            b[_KECCAK_PI[i]] = (((v << n) | (v >> (64 - n))) & _M64) if n else v
        a = [b[i] ^ (~b[5 * (i // 5) + (i + 1) % 5] & b[5 * (i // 5) + (i + 2) % 5]) for i in range(25)]
        a[0] ^= _KECCAK_RC[rnd]
    return a


def _absorb_block(lanes: List[int], block: bytes, rounds: int = 23) -> List[int]:
    lanes = list(lanes)
    for i in range(_RATE // 8):
        lanes[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
    return _keccak_p1600(lanes, rounds)


def keccak_sha3_256(data: bytes, rounds: int = 23) -> bytes:
    """SHA3-256 with a selectable round count: 24 is FIPS SHA3-256, 23 is DeepSeekHashV1."""
    padded = bytearray(data)
    padded.append(0x06)
    padded.extend(b"\x00" * ((-len(padded)) % _RATE))
    padded[-1] ^= 0x80
    lanes = [0] * 25
    for offset in range(0, len(padded), _RATE):
        lanes = _absorb_block(lanes, bytes(padded[offset : offset + _RATE]), rounds)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


def deepseek_hash_v1(data: bytes) -> str:
    return keccak_sha3_256(data, 23).hex()


class _SliceConsts:
    """Masks and round constants replicated into every 64-bit slot of an n-slot int."""

    __slots__ = ("n", "rep", "lmask", "rmask", "rcs")

    def __init__(self, n: int):
        self.n = n
        self.rep = int.from_bytes(b"\x01\x00\x00\x00\x00\x00\x00\x00" * n, "little")
        self.lmask = [0] + [((_M64 << k) & _M64) * self.rep for k in range(1, 64)]
        self.rmask = [0] + [((1 << k) - 1) * self.rep for k in range(1, 64)]
        self.rcs = [rc * self.rep for rc in _KECCAK_RC[1:]]  # rounds 1..23


_slice_cache: Dict[int, _SliceConsts] = {}


def _slice_consts(n: int) -> _SliceConsts:
    consts = _slice_cache.get(n)
    if consts is None:
        consts = _slice_cache[n] = _SliceConsts(n)
    return consts


def _sliced_keccak_p23(st: List[int], k: _SliceConsts) -> List[int]:
    """KECCAK-p[1600, 23] on bit-sliced lanes. A rotation inside every 64-bit slot is
    ((v << n) & L[n]) | ((v >> (64 - n)) & R[n]); chi's ~b1 & b2 is (b1 | b2) ^ b1."""
    (a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12,
     a13, a14, a15, a16, a17, a18, a19, a20, a21, a22, a23, a24) = st  # fmt: skip
    L = k.lmask
    R = k.rmask
    l1 = L[1]
    r1 = R[1]
    for rc in k.rcs:
        c0 = a0 ^ a5 ^ a10 ^ a15 ^ a20
        c1 = a1 ^ a6 ^ a11 ^ a16 ^ a21
        c2 = a2 ^ a7 ^ a12 ^ a17 ^ a22
        c3 = a3 ^ a8 ^ a13 ^ a18 ^ a23
        c4 = a4 ^ a9 ^ a14 ^ a19 ^ a24
        d0 = c4 ^ (((c1 << 1) & l1) | ((c1 >> 63) & r1))
        d1 = c0 ^ (((c2 << 1) & l1) | ((c2 >> 63) & r1))
        d2 = c1 ^ (((c3 << 1) & l1) | ((c3 >> 63) & r1))
        d3 = c2 ^ (((c4 << 1) & l1) | ((c4 >> 63) & r1))
        d4 = c3 ^ (((c0 << 1) & l1) | ((c0 >> 63) & r1))
        b0 = a0 ^ d0
        v = a1 ^ d1
        b10 = ((v << 1) & L[1]) | ((v >> 63) & R[1])
        v = a2 ^ d2
        b20 = ((v << 62) & L[62]) | ((v >> 2) & R[62])
        v = a3 ^ d3
        b5 = ((v << 28) & L[28]) | ((v >> 36) & R[28])
        v = a4 ^ d4
        b15 = ((v << 27) & L[27]) | ((v >> 37) & R[27])
        v = a5 ^ d0
        b16 = ((v << 36) & L[36]) | ((v >> 28) & R[36])
        v = a6 ^ d1
        b1 = ((v << 44) & L[44]) | ((v >> 20) & R[44])
        v = a7 ^ d2
        b11 = ((v << 6) & L[6]) | ((v >> 58) & R[6])
        v = a8 ^ d3
        b21 = ((v << 55) & L[55]) | ((v >> 9) & R[55])
        v = a9 ^ d4
        b6 = ((v << 20) & L[20]) | ((v >> 44) & R[20])
        v = a10 ^ d0
        b7 = ((v << 3) & L[3]) | ((v >> 61) & R[3])
        v = a11 ^ d1
        b17 = ((v << 10) & L[10]) | ((v >> 54) & R[10])
        v = a12 ^ d2
        b2 = ((v << 43) & L[43]) | ((v >> 21) & R[43])
        v = a13 ^ d3
        b12 = ((v << 25) & L[25]) | ((v >> 39) & R[25])
        v = a14 ^ d4
        b22 = ((v << 39) & L[39]) | ((v >> 25) & R[39])
        v = a15 ^ d0
        b23 = ((v << 41) & L[41]) | ((v >> 23) & R[41])
        v = a16 ^ d1
        b8 = ((v << 45) & L[45]) | ((v >> 19) & R[45])
        v = a17 ^ d2
        b18 = ((v << 15) & L[15]) | ((v >> 49) & R[15])
        v = a18 ^ d3
        b3 = ((v << 21) & L[21]) | ((v >> 43) & R[21])
        v = a19 ^ d4
        b13 = ((v << 8) & L[8]) | ((v >> 56) & R[8])
        v = a20 ^ d0
        b14 = ((v << 18) & L[18]) | ((v >> 46) & R[18])
        v = a21 ^ d1
        b24 = ((v << 2) & L[2]) | ((v >> 62) & R[2])
        v = a22 ^ d2
        b9 = ((v << 61) & L[61]) | ((v >> 3) & R[61])
        v = a23 ^ d3
        b19 = ((v << 56) & L[56]) | ((v >> 8) & R[56])
        v = a24 ^ d4
        b4 = ((v << 14) & L[14]) | ((v >> 50) & R[14])
        a0 = b0 ^ ((b1 | b2) ^ b1)
        a1 = b1 ^ ((b2 | b3) ^ b2)
        a2 = b2 ^ ((b3 | b4) ^ b3)
        a3 = b3 ^ ((b4 | b0) ^ b4)
        a4 = b4 ^ ((b0 | b1) ^ b0)
        a5 = b5 ^ ((b6 | b7) ^ b6)
        a6 = b6 ^ ((b7 | b8) ^ b7)
        a7 = b7 ^ ((b8 | b9) ^ b8)
        a8 = b8 ^ ((b9 | b5) ^ b9)
        a9 = b9 ^ ((b5 | b6) ^ b5)
        a10 = b10 ^ ((b11 | b12) ^ b11)
        a11 = b11 ^ ((b12 | b13) ^ b12)
        a12 = b12 ^ ((b13 | b14) ^ b13)
        a13 = b13 ^ ((b14 | b10) ^ b14)
        a14 = b14 ^ ((b10 | b11) ^ b10)
        a15 = b15 ^ ((b16 | b17) ^ b16)
        a16 = b16 ^ ((b17 | b18) ^ b17)
        a17 = b17 ^ ((b18 | b19) ^ b18)
        a18 = b18 ^ ((b19 | b15) ^ b19)
        a19 = b19 ^ ((b15 | b16) ^ b15)
        a20 = b20 ^ ((b21 | b22) ^ b21)
        a21 = b21 ^ ((b22 | b23) ^ b22)
        a22 = b22 ^ ((b23 | b24) ^ b23)
        a23 = b23 ^ ((b24 | b20) ^ b24)
        a24 = b24 ^ ((b20 | b21) ^ b20)
        a0 ^= rc
    return [a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12,
            a13, a14, a15, a16, a17, a18, a19, a20, a21, a22, a23, a24]  # fmt: skip


def solve_deepseek_pow(
    challenge: Any, salt: Any, expire_at: Any, difficulty: Any, time_budget: float = 30.0
) -> int:
    """Find the nonce n in [0, difficulty) with DeepSeekHashV1(f"{salt}_{expire_at}_{n}")
    == challenge. Returns -1 when there is none (like OmniRoute's solver)."""
    if not isinstance(challenge, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", challenge):
        raise PowError("DeepSeek PoW challenge must be a 64-character hex digest")
    if not isinstance(salt, str) or not 0 < len(salt) <= 1024:
        raise PowError("DeepSeek PoW salt must contain 1-1024 characters")
    if isinstance(expire_at, bool) or not isinstance(expire_at, int) or expire_at < 0:
        raise PowError("DeepSeek PoW expiry must be a non-negative integer")
    if isinstance(difficulty, bool) or not isinstance(difficulty, int):
        raise PowError("DeepSeek PoW difficulty must be an integer")
    if not 1 <= difficulty <= POW_MAX_DIFFICULTY:
        raise PowError(f"DeepSeek PoW difficulty must be between 1 and {POW_MAX_DIFFICULTY}")

    target = bytes.fromhex(challenge)
    target0 = int.from_bytes(target[:8], "little")
    prefix = f"{salt}_{expire_at}_".encode("utf-8")
    deadline = time.monotonic() + time_budget

    base = [0] * 25
    full = len(prefix) - len(prefix) % _RATE
    for offset in range(0, full, _RATE):
        base = _absorb_block(base, prefix[offset : offset + _RATE])
    tail = prefix[full:]
    t = len(tail)

    digits = 1
    while True:
        lo = 0 if digits == 1 else 10 ** (digits - 1)
        hi = min(10**digits, difficulty)
        if lo >= hi:
            return -1
        if t + digits + 1 > _RATE:
            # The nonce would spill into a second block (needs a >100-char salt): scalar path.
            for nonce in range(lo, hi):
                if nonce % 4096 == 0 and time.monotonic() > deadline:
                    raise PowError("DeepSeek PoW took too long")
                if keccak_sha3_256(prefix + str(nonce).encode("ascii")) == target:
                    return nonce
            digits += 1
            continue

        # Everything except the nonce digits is the same for every candidate of this length.
        block = bytearray(_RATE)
        block[:t] = tail
        block[t + digits] ^= 0x06
        block[_RATE - 1] ^= 0x80
        lanes = [
            base[i] ^ int.from_bytes(block[8 * i : 8 * i + 8], "little") if i < _RATE // 8 else base[i]
            for i in range(25)
        ]
        digit_lanes = sorted({(t + j) // 8 for j in range(digits)})

        for start in range(lo, hi, _POW_BATCH):
            if time.monotonic() > deadline:
                raise PowError("DeepSeek PoW took too long")
            n = min(_POW_BATCH, hi - start)
            k = _slice_consts(n)
            st = [v * k.rep for v in lanes]
            text = "".join(map(str, range(start, start + n))).encode("ascii")
            for lane in digit_lanes:
                buf = bytearray(8 * n)
                for j in range(digits):
                    pos = t + j
                    if pos // 8 == lane:
                        buf[pos % 8 :: 8] = text[j::digits]
                st[lane] ^= int.from_bytes(buf, "little")
            first_lane = _sliced_keccak_p23(st, k)[0] ^ (target0 * k.rep)
            words = memoryview(first_lane.to_bytes(8 * n, "little")).cast("Q").tolist()
            idx = -1
            while True:
                try:
                    idx = words.index(0, idx + 1)
                except ValueError:
                    break
                nonce = start + idx
                if keccak_sha3_256(prefix + str(nonce).encode("ascii")) == target:
                    return nonce
        digits += 1


def pow_response_header(challenge: Dict[str, Any], answer: int, target_path: str) -> str:
    """Base64 JSON for the X-Ds-Pow-Response header (same field order as the web client)."""
    payload = {
        "algorithm": challenge.get("algorithm"),
        "challenge": challenge.get("challenge"),
        "salt": challenge.get("salt"),
        "answer": answer,
        "signature": challenge.get("signature"),
        "target_path": challenge.get("target_path") or target_path,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


# ════════════════════════════════════════════════════════════════════════════
# HTTP helpers (urllib honours HTTPS_PROXY and the Windows system proxy)
# ════════════════════════════════════════════════════════════════════════════

_opener = urllib.request.build_opener()


def configure_proxy(url: Optional[str]) -> None:
    global _opener
    if url:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))


def http_open(method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None, timeout: float = 60.0):
    """Open a request; HTTP error statuses come back as the (readable) HTTPError response."""
    host = urllib.parse.urlsplit(url).hostname or url
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        return _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise UpstreamError(f"Timed out connecting to {host}", 504) from None
        raise UpstreamError(f"Could not reach {host}: {exc.reason}", 502) from None
    except (socket.timeout, TimeoutError):
        raise UpstreamError(f"Timed out waiting for {host}", 504) from None
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamError(f"Network error talking to {host}: {exc}", 502) from None


def response_status(resp: Any) -> int:
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(resp, "code", None)
    return int(status or 0)


def close_quietly(resp: Any) -> None:
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def read_body(resp: Any, limit: int = 64 * 1024 * 1024) -> bytes:
    try:
        raw = resp.read(limit + 1) or b""
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamError(f"Network error reading the DeepSeek response: {exc}", 502) from None
    finally:
        close_quietly(resp)
    if len(raw) > limit:
        raise UpstreamError("DeepSeek response too large", 502)
    headers = getattr(resp, "headers", None)
    encoding = (headers.get("Content-Encoding") or "").strip().lower() if headers else ""
    try:
        if encoding == "gzip":
            raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except zlib.error as exc:
        raise UpstreamError(f"Could not decompress the DeepSeek response: {exc}", 502) from None
    return raw


def check_waf(status: int, headers: Any, what: str) -> None:
    """chat.deepseek.com sits behind AWS WAF: a 202 "challenge", 405 "captcha" or an
    HTML error page means the request never reached DeepSeek."""
    action = (headers.get("x-amzn-waf-action") or "").lower() if headers else ""
    ctype = (headers.get("Content-Type") or "").lower() if headers else ""
    if action or (status in (202, 403, 405) and "text/html" in ctype):
        detail = f"WAF action '{action}'" if action else f"HTTP {status}"
        raise WafError(
            f"DeepSeek's firewall blocked the request to {what} ({detail}). Wait a few "
            "minutes and retry; if you use a VPN or proxy, try without it."
        )


def unwrap_envelope(path: str, status: int, payload: Any) -> Any:
    """DeepSeek wraps answers as {code, msg, data: {biz_code, biz_msg, biz_data}}."""
    if not isinstance(payload, dict):
        raise UpstreamError(f"DeepSeek returned an unexpected response for {path}")
    code = payload.get("code")
    msg = payload.get("msg") if isinstance(payload.get("msg"), str) else ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if code in AUTH_ERROR_CODES or (status in (401, 403) and code in (None, 0)):
        raise AuthError(
            f"DeepSeek rejected the login token ({msg or code or status}). Run the 'login' command again.",
            code=code,
        )
    if status == 429:
        raise RateLimitError(f"DeepSeek rate limited the request{': ' + msg if msg else ''}. Wait and retry.", code=code)
    if code not in (None, 0):
        detail = msg or data.get("biz_msg") or "unknown error"
        raise UpstreamError(f"DeepSeek error {code}: {detail}", status if 400 <= status < 600 else 502, code)
    biz_code = data.get("biz_code")
    if biz_code not in (None, 0):
        detail = data.get("biz_msg") or f"error {biz_code}"
        raise UpstreamError(f"DeepSeek {path} failed: {detail}", 502, biz_code)
    if status >= 400:
        raise UpstreamError(f"DeepSeek {path} failed (HTTP {status})", status if status < 600 else 502)
    if "biz_data" in data:
        return data.get("biz_data")
    return payload.get("biz_data", data)


def utc_offset_seconds() -> int:
    offset = _dt.datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset else 0


def client_hint_headers(creds: Dict[str, Any], user_agent: str) -> Dict[str, str]:
    """sec-ch-ua headers matching the browser used for login (or derived from the UA)."""
    brands = creds.get("ua_brands")
    if isinstance(brands, list) and brands:
        parts = [
            f'"{b.get("brand")}";v="{b.get("version", "")}"'
            for b in brands
            if isinstance(b, dict) and b.get("brand")
        ]
        sec_ua = ", ".join(parts)
    else:
        chrome = re.search(r"Chrome/(\d+)", user_agent)
        if not chrome:
            return {}
        edge = re.search(r"Edg/(\d+)", user_agent)
        major = chrome.group(1)
        lead = f'"Microsoft Edge";v="{edge.group(1)}"' if edge else f'"Google Chrome";v="{major}"'
        sec_ua = f'{lead}, "Chromium";v="{major}", "Not(A:Brand";v="24"'
    platform = creds.get("ua_platform")
    if not isinstance(platform, str) or not platform:
        platform = (
            "Windows" if "Windows" in user_agent
            else "macOS" if "Mac OS X" in user_agent
            else "Linux" if "Linux" in user_agent
            else ""
        )  # fmt: skip
    headers = {"sec-ch-ua": sec_ua, "sec-ch-ua-mobile": "?0"}
    if platform:
        headers["sec-ch-ua-platform"] = f'"{platform}"'
    return headers


def _walk_dicts(value: Any, depth: int = 0) -> Iterator[Dict[str, Any]]:
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child, depth + 1)


# ════════════════════════════════════════════════════════════════════════════
# chat.deepseek.com client
# ════════════════════════════════════════════════════════════════════════════

MAX_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024
UPLOAD_CACHE_TTL = 30 * 60
_FILE_FAILED = {"FAILED", "CONTENT_FILTER", "CONTENT_TOO_LONG", "CANCELLED", "CONTENT_EMPTY"}


class DeepSeekClient:
    """The chat.deepseek.com web API calls used by OmniRoute's deepseek-web executor."""

    def __init__(self, store: CredentialStore, keep_sessions: bool = False):
        self.store = store
        self.keep_sessions = keep_sessions
        self.account: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._access: Optional[Tuple[str, str, float]] = None
        self._models: Optional[Tuple[float, frozenset]] = None
        self._uploads: Dict[str, Tuple[str, float]] = {}
        self._cleanups: List[threading.Thread] = []
        self._fallback_device_id = str(uuid.uuid4())

    # ── request plumbing ────────────────────────────────────────────────────

    def headers(
        self,
        bearer: Optional[str] = None,
        content_type: Optional[str] = "application/json",
        accept_encoding: str = "gzip, deflate",
    ) -> Dict[str, str]:
        creds = self.store.get()
        user_agent = creds.get("user_agent") or DEFAULT_USER_AGENT
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": accept_encoding,
            "Accept-Language": creds.get("accept_language") or "en-US,en;q=0.9",
            "Origin": DEEPSEEK_BASE,
            "Referer": DEEPSEEK_BASE + "/",
            "User-Agent": user_agent,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-locale": creds.get("locale") or "en_US",
            "x-client-platform": "web",
            "x-client-timezone-offset": str(utc_offset_seconds()),
            "x-client-version": creds.get("client_version") or DEFAULT_CLIENT_VERSION,
        }
        headers.update(client_hint_headers(creds, user_agent))
        device_id = creds.get("device_id")
        if isinstance(device_id, str) and device_id:
            headers["x-device-id"] = device_id
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def api(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        bearer: Optional[str] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        url = DEEPSEEK_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = self.headers(bearer=bearer, content_type="application/json" if data is not None else None)
        resp = http_open(method, url, headers, data, timeout)
        status = response_status(resp)
        resp_headers = resp.headers
        raw = read_body(resp)
        check_waf(status, resp_headers, path)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if status == 429:
                raise RateLimitError("DeepSeek rate limited the request. Wait and retry.") from None
            if status in (401, 403):
                raise AuthError("DeepSeek rejected the login token. Run the 'login' command again.") from None
            raise UpstreamError(
                f"DeepSeek returned an unexpected response for {path} (HTTP {status})",
                502 if status < 500 else status,
            ) from None
        return unwrap_envelope(path, status, payload)

    # ── auth ────────────────────────────────────────────────────────────────

    def user_token(self) -> str:
        token = extract_user_token(self.store.get().get("token"))
        if not token:
            raise AuthError("Not logged in. Run: python deepseek_web_api.py login")
        return token

    def access_token(self, force: bool = False) -> str:
        """userToken -> bearer token via /users/current, cached for an hour (as in OmniRoute)."""
        user_token = self.user_token()
        now = time.time()
        with self._lock:
            cached = self._access
            if not force and cached and cached[0] == user_token and cached[2] > now:
                return cached[1]
        debug("Fetching the access token from /users/current")
        biz = self.api("GET", "/api/v0/users/current", bearer=user_token, timeout=20)
        token = biz.get("token") if isinstance(biz, dict) else None
        if not isinstance(token, str) or not token:
            raise AuthError("DeepSeek did not return an access token. Run the 'login' command again.")
        with self._lock:
            self._access = (user_token, token, now + ACCESS_TOKEN_TTL)
            self.account = {
                "email": biz.get("email") or biz.get("mobile_number") or "",
                "id": biz.get("id") or "",
            }
        return token

    def invalidate(self) -> None:
        with self._lock:
            self._access = None
            self._models = None

    def enabled_model_types(self, access: str) -> frozenset:
        """Model types switched on for this account (the web app's model settings)."""
        now = time.time()
        with self._lock:
            if self._models and self._models[0] > now:
                return self._models[1]
        types, ttl = set(), 1800
        try:
            device_id = self.store.get().get("device_id") or self._fallback_device_id
            biz = self.api(
                "GET",
                "/api/v0/client/settings",
                bearer=access,
                query={"did": device_id, "scope": "model"},
                timeout=15,
            )
            for item in _walk_dicts(biz):
                model_type = item.get("model_type")
                if isinstance(model_type, str) and model_type and item.get("enabled") is True:
                    types.add(model_type)
        except AuthError:
            raise
        except UpstreamError as exc:
            debug(f"Model settings unavailable: {exc}")
            ttl = 120
        result = frozenset(types or {"default"})
        with self._lock:
            self._models = (now + ttl, result)
        return result

    # ── proof of work, sessions, completions ────────────────────────────────

    def pow_header(self, access: str, target_path: str) -> str:
        biz = self.api(
            "POST", "/api/v0/chat/create_pow_challenge", {"target_path": target_path}, bearer=access, timeout=20
        )
        challenge = biz.get("challenge") if isinstance(biz, dict) else None
        if not isinstance(challenge, dict):
            raise PowError("DeepSeek did not return a proof-of-work challenge")
        if challenge.get("algorithm") != POW_ALGORITHM:
            raise PowError(f"Unsupported DeepSeek PoW algorithm: {challenge.get('algorithm')!r}")
        if not all(isinstance(challenge.get(key), str) for key in ("challenge", "salt", "signature")):
            raise PowError("Invalid DeepSeek proof-of-work challenge")
        started = time.perf_counter()
        answer = solve_deepseek_pow(
            challenge.get("challenge"), challenge.get("salt"), challenge.get("expire_at"), challenge.get("difficulty")
        )
        if answer < 0:
            raise PowError("DeepSeek proof-of-work solver found no answer")
        debug(
            f"PoW solved: difficulty={challenge.get('difficulty')} answer={answer} "
            f"in {time.perf_counter() - started:.2f}s"
        )
        return pow_response_header(challenge, answer, target_path)

    def create_session(self, access: str) -> str:
        biz = self.api("POST", "/api/v0/chat_session/create", {}, bearer=access, timeout=20)
        session = biz.get("chat_session") if isinstance(biz, dict) else None
        sid = session.get("id") if isinstance(session, dict) else None
        if not sid and isinstance(biz, dict):
            sid = biz.get("id")
        if not isinstance(sid, str) or not sid:
            raise UpstreamError("DeepSeek did not return a chat session id")
        return sid

    def delete_session(self, access: str, sid: str) -> None:
        try:
            self.api("POST", "/api/v0/chat_session/delete", {"chat_session_id": sid}, bearer=access, timeout=15)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            debug(f"Could not delete chat session {sid}: {exc}")

    def delete_session_async(self, access: str, sid: Optional[str]) -> None:
        """Remove the temporary chat from the DeepSeek history (unless --keep-sessions)."""
        if self.keep_sessions or not sid:
            return
        thread = threading.Thread(target=self.delete_session, args=(access, sid), daemon=True, name="ds-cleanup")
        with self._lock:
            self._cleanups = [t for t in self._cleanups if t.is_alive()] + [thread]
        thread.start()

    def wait_for_cleanup(self, timeout: float = 5.0) -> None:
        """Let pending chat deletions finish before the process exits."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._cleanups)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def open_stream(self, access: str, path: str, payload: Dict[str, Any], timeout: float = 180.0) -> Any:
        """POST a completion-like request and return the open SSE response."""
        headers = self.headers(bearer=access, accept_encoding="identity")
        headers["Referer"] = f"{DEEPSEEK_BASE}/a/chat/s/{payload.get('chat_session_id')}"
        headers["priority"] = "u=1, i"
        # completion, continue and regenerate all use completion PoW challenges in the web client
        headers["x-ds-pow-response"] = self.pow_header(access, COMPLETION_PATH)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        resp = http_open("POST", DEEPSEEK_BASE + path, headers, body, timeout)
        status = response_status(resp)
        ctype = (resp.headers.get("Content-Type") or "").lower() if resp.headers else ""
        if status == 200 and "text/event-stream" in ctype:
            return resp
        resp_headers = resp.headers
        raw = read_body(resp)
        check_waf(status, resp_headers, path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if status == 429:
                raise RateLimitError("DeepSeek rate limited the request. Wait and retry.") from None
            raise UpstreamError(f"DeepSeek returned an unexpected response (HTTP {status})", 502) from None
        unwrap_envelope(path, status, data)  # raises for error envelopes
        raise UpstreamError("DeepSeek answered without a stream; try again", 502)

    # ── images ──────────────────────────────────────────────────────────────

    def upload_images(
        self, access: str, images: List[Tuple[bytes, str]], model_type: str, thinking: bool
    ) -> List[str]:
        """Upload images and wait until DeepSeek has processed them. Only file ids are
        cached (per account, 30 minutes), never image bytes."""
        ids: List[str] = []
        deadline = time.monotonic() + 120
        for index, (data, fmt) in enumerate(images, 1):
            key = hashlib.sha256(access.encode("utf-8") + b"\0" + data).hexdigest()
            now = time.time()
            with self._lock:
                cached = self._uploads.get(key)
                if cached and cached[1] > now:
                    ids.append(cached[0])
                    continue
                self._uploads.pop(key, None)
            file_id = self._upload_image(access, data, fmt, index, model_type, thinking, deadline)
            with self._lock:
                if len(self._uploads) >= 256:
                    self._uploads.pop(next(iter(self._uploads)))
                self._uploads[key] = (file_id, now + UPLOAD_CACHE_TTL)
            ids.append(file_id)
        return ids

    def _upload_image(
        self, access: str, data: bytes, fmt: str, index: int, model_type: str, thinking: bool, deadline: float
    ) -> str:
        ext = "jpg" if fmt == "jpeg" else fmt
        boundary = "----WebKitFormBoundary" + "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
        )
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="image-{index}.{ext}"\r\n'
            f"Content-Type: image/{fmt}\r\n\r\n"
        ).encode("ascii") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        headers = self.headers(bearer=access, content_type=f"multipart/form-data; boundary={boundary}")
        headers["x-ds-pow-response"] = self.pow_header(access, UPLOAD_PATH)
        headers["x-file-size"] = str(len(data))
        headers["x-model-type"] = model_type
        headers["x-thinking-enabled"] = "1" if thinking else "0"
        resp = http_open("POST", DEEPSEEK_BASE + UPLOAD_PATH, headers, body, timeout=120)
        status = response_status(resp)
        resp_headers = resp.headers
        raw = read_body(resp)
        check_waf(status, resp_headers, UPLOAD_PATH)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ImageError(f"DeepSeek image upload failed (HTTP {status})", 502) from None
        try:
            info = unwrap_envelope(UPLOAD_PATH, status, payload)
        except (AuthError, RateLimitError):
            raise
        except UpstreamError as exc:
            raise ImageError(f"DeepSeek rejected the image: {exc}", 502) from None
        file_id = info.get("id") if isinstance(info, dict) else None
        file_status = info.get("status") if isinstance(info, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise ImageError("DeepSeek image response has no file id", 502)
        while file_status != "SUCCESS":
            if isinstance(file_status, str) and (
                file_status in _FILE_FAILED or re.search(r"FAIL|ERROR|REJECT|INVALID|UNSUPPORT", file_status)
            ):
                raise ImageError(f"DeepSeek could not process the image ({file_status})", 502)
            if time.monotonic() > deadline:
                raise ImageError("DeepSeek image processing timed out", 504)
            time.sleep(1.0)
            polled = self.api("GET", FETCH_FILES_PATH, bearer=access, query={"file_ids": file_id}, timeout=20)
            files = polled.get("files") if isinstance(polled, dict) else None
            found = next(
                (f for f in files or [] if isinstance(f, dict) and f.get("id") == file_id),
                None,
            )
            if found is None:
                raise ImageError("DeepSeek's file status response omitted the image", 502)
            file_status = found.get("status")
        return file_id


# ── image extraction from OpenAI / Anthropic style messages ────────────────

_DATA_URI_HEADER_RE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64$", re.I)


def find_image_refs(messages: List[Any]) -> List[str]:
    """Image URLs / data URIs in message content (image_url, input_image, image+source)."""
    refs: List[str] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind in ("image_url", "input_image"):
            raw = node.get("image_url")
            url = raw if isinstance(raw, str) else raw.get("url") if isinstance(raw, dict) else None
            if isinstance(url, str) and url:
                refs.append(url)
                return
        if kind == "image":
            source = node.get("source")
            if isinstance(source, dict):
                if source.get("type") == "base64" and isinstance(source.get("data"), str):
                    media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
                    refs.append(f"data:{media};base64,{source['data']}")
                    return
                if source.get("type") == "url" and isinstance(source.get("url"), str) and source["url"]:
                    refs.append(source["url"])
                    return
        for child in node.values():
            if isinstance(child, (list, dict)):
                visit(child, depth + 1)

    for message in messages:
        if isinstance(message, dict):
            visit(message.get("content"), 0)
    return list(dict.fromkeys(refs))


def sniff_image_format(data: bytes) -> Optional[str]:
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 6 and data[:4] == b"GIF8":
        return "gif"
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _check_public_https(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ImageError("Image URLs must use HTTPS", 400)
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise ImageError("Could not resolve the image host", 400) from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ImageError("Image URLs must point to a public host", 400)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        _check_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_image(url: str, timeout: float = 30.0) -> bytes:
    _check_public_https(url)
    opener = urllib.request.build_opener(_PublicRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "image/*"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ImageError(f"Could not fetch the image (HTTP {exc.code})", 400) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise ImageError("Could not fetch the image from a public HTTPS URL", 400) from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("Images must be 20 MiB or smaller", 400)
    return data


def load_image(ref: str) -> Tuple[bytes, str]:
    if ref.startswith("data:"):
        header, sep, payload = ref.partition(",")
        payload = re.sub(r"\s+", "", payload)
        if (
            not sep
            or not _DATA_URI_HEADER_RE.match(header)
            or not payload
            or len(payload) > (MAX_IMAGE_BYTES + 2) // 3 * 4
        ):
            raise ImageError("Expected a base64 PNG, JPEG, GIF or WebP image (maximum 20 MiB)", 400)
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise ImageError("Invalid base64 image", 400) from None
    else:
        data = fetch_public_image(ref)
    fmt = sniff_image_format(data)
    if not fmt or not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageError("Invalid image or image exceeds 20 MiB", 400)
    return data, fmt


def load_images(messages: List[Any]) -> List[Tuple[bytes, str]]:
    refs = find_image_refs(messages)
    if len(refs) > MAX_IMAGES:
        raise ImageError("DeepSeek supports at most 10 images per request", 400)
    images, total = [], 0
    for ref in refs:
        data, fmt = load_image(ref)
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise ImageError("Images exceed the 50 MiB request limit", 400)
        images.append((data, fmt))
    return images


# ════════════════════════════════════════════════════════════════════════════
# DeepSeek stream parser
#
# chat.deepseek.com streams JSON-patch-like events. "p" (path) and "o" (operation)
# are sticky: an event with only "v" continues the previous path, e.g.
#   data: {"v":{"response":{"fragments":[{"id":2,"type":"THINK","content":"We"}]}}}
#   data: {"p":"response/fragments/-1/content","o":"APPEND","v":" need"}
#   data: {"v":" answer"}
#   data: {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"391"}]}
#   data: {"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":69}, ...]}
#   data: {"p":"response/status","o":"SET","v":"FINISHED"}
# Fragment types say what the text is: THINK -> reasoning_content, RESPONSE -> content,
# SEARCH -> web search (its "results" carry the citation sources).
# ════════════════════════════════════════════════════════════════════════════

FINISH_REASONS = {
    "FINISHED": "stop",
    "INCOMPLETE": "length",
    "CONTENT_FILTER": "content_filter",
    "CONTEXT_LENGTH_EXCEEDED": "length",
    "TIMEOUT": "stop",
}
_THINK_FRAGMENTS = {"THINK", "THINKING", "REASONING"}
_HIDDEN_FRAGMENTS = {"SEARCH"}


class DeepSeekStreamParser:
    def __init__(self) -> None:
        self.fragments: List[Dict[str, Any]] = []
        self.status: Optional[str] = None
        self.token_usage: Optional[int] = None
        self.response_message_id: Any = None
        self.model_type: Optional[str] = None
        self.search_results: Dict[int, Dict[str, Any]] = {}
        self._emitted: Dict[Any, int] = {}
        self._path: Optional[str] = None
        self._op: Optional[str] = None
        self._legacy_results: List[Any] = []

    def begin_stream(self) -> None:
        """Call before reading each upstream stream (a "continue" opens a new one)."""
        self._path = None
        self._op = None
        self.status = None

    @property
    def terminal(self) -> bool:
        return self.status in FINISH_REASONS

    def feed(self, line: str) -> List[Tuple[str, str]]:
        """Parse one SSE line; returns [(kind, text)] with kind "reasoning" or "content"."""
        line = line.rstrip("\r\n")
        if not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            event = json.loads(payload)
        except ValueError:
            return []
        if not isinstance(event, dict):
            return []
        out: List[Tuple[str, str]] = []
        if "v" not in event:
            # named events: ready (message ids), update_session, title, close
            if "response_message_id" in event:
                self.response_message_id = event.get("response_message_id")
                if isinstance(event.get("model_type"), str):
                    self.model_type = event["model_type"]
            return out
        # Like the web client: o and p are each sticky (`op = e.o ?? this.op`).
        if isinstance(event.get("o"), str):
            self._op = event["o"]
        if isinstance(event.get("p"), str):
            self._path = event["p"]
        for path, op, value in self._expand(self._path, self._op, event.get("v")):
            self._apply(path, op, value, out)
        return out

    @classmethod
    def _expand(cls, path: Optional[str], op: Optional[str], value: Any) -> List[Tuple[Optional[str], Optional[str], Any]]:
        """Flatten BATCH ops. Items of a batch have their own sticky o/p, relative to the batch path."""
        if op != "BATCH":
            return [(path, op, value)]
        if not isinstance(value, list):
            return []
        flat: List[Tuple[Optional[str], Optional[str], Any]] = []
        sub_op: Optional[str] = None
        sub_path: Optional[str] = None
        for item in value:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("o"), str):
                sub_op = item["o"]
            if isinstance(item.get("p"), str):
                sub_path = item["p"]
            for rel, item_op, item_value in cls._expand(sub_path, sub_op, item.get("v")):
                full = f"{path}/{rel}" if path and rel else (path or rel)
                flat.append((full, item_op, item_value))
        return flat

    # ── patch application ────────────────────────────────────────────────────

    def _apply(self, path: Optional[str], op: Optional[str], value: Any, out: List[Tuple[str, str]]) -> None:
        if not path:
            if isinstance(value, dict) and isinstance(value.get("response"), dict):
                self._merge_response(value["response"], out)
            elif isinstance(value, str):  # very old protocol: bare text
                self._legacy_append("content", value, op, out)
            return
        if path == "response":
            if isinstance(value, dict):
                self._merge_response(value, out)
            return
        if not path.startswith("response/"):
            return
        key = path[len("response/") :]
        if key == "fragments":
            if isinstance(value, list):
                for fragment in value:
                    self._add_fragment(fragment, out)
            elif isinstance(value, dict):
                self._add_fragment(value, out)
            elif isinstance(value, str) and self.fragments:
                self._set_content(self.fragments[-1], value, op, out)
            return
        if key.startswith("fragments/"):
            parts = key.split("/", 2)
            try:
                index = int(parts[1])
            except ValueError:
                return
            fragment = self._fragment_at(index)
            if fragment is None:
                return
            field = parts[2] if len(parts) > 2 else ""
            if not field:
                if isinstance(value, dict):
                    self._add_fragment({**value, "id": fragment.get("id")}, out)
            elif field == "content":
                if isinstance(value, str):
                    self._set_content(fragment, value, op, out)
            elif field == "results":
                if isinstance(value, list):
                    self._index_results(value)
            elif field == "type" and isinstance(value, str):
                fragment["type"] = value.upper()
            return
        if key == "status":
            if isinstance(value, str):
                self.status = value
        elif key == "accumulated_token_usage":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.token_usage = int(value)
        elif key == "search_results":  # older protocol (what OmniRoute still parses)
            self._legacy_search_results(op, value)
        elif key in ("content", "thinking_content") and isinstance(value, str):  # older protocol
            self._legacy_append("reasoning" if key == "thinking_content" else "content", value, op, out)

    def _merge_response(self, response: Dict[str, Any], out: List[Tuple[str, str]]) -> None:
        status = response.get("status")
        if isinstance(status, str) and status != "WIP":
            self.status = status
        usage = response.get("accumulated_token_usage")
        if isinstance(usage, (int, float)) and not isinstance(usage, bool) and usage:
            self.token_usage = int(usage)
        fragments = response.get("fragments")
        if isinstance(fragments, list):
            for fragment in fragments:
                self._add_fragment(fragment, out)
        if isinstance(response.get("search_results"), list):
            self._legacy_search_results("SET", response["search_results"])

    def _fragment_at(self, index: int) -> Optional[Dict[str, Any]]:
        try:
            return self.fragments[index]
        except IndexError:
            return None

    def _add_fragment(self, raw: Any, out: List[Tuple[str, str]]) -> None:
        if not isinstance(raw, dict):
            return
        fid = raw.get("id")
        fragment = None
        if fid is not None:
            fragment = next((f for f in self.fragments if f.get("id") == fid), None)
        if fragment is None:
            fragment = {"id": fid, "type": str(raw.get("type") or "").upper(), "content": ""}
            self.fragments.append(fragment)
        elif raw.get("type"):
            fragment["type"] = str(raw["type"]).upper()
        if isinstance(raw.get("results"), list):
            self._index_results(raw["results"])
        if isinstance(raw.get("content"), str):
            self._set_content(fragment, raw["content"], "SET", out)

    def _set_content(self, fragment: Dict[str, Any], text: str, op: Optional[str], out: List[Tuple[str, str]]) -> None:
        fragment["content"] = (fragment.get("content") or "") + text if op == "APPEND" else text
        ftype = fragment.get("type") or ""
        if ftype in _HIDDEN_FRAGMENTS or ftype.startswith("TOOL"):
            return
        kind = "reasoning" if ftype in _THINK_FRAGMENTS else "content"
        key = fragment.get("id") if fragment.get("id") is not None else id(fragment)
        content = fragment["content"]
        done = self._emitted.get(key, 0)
        if len(content) > done:  # only text not sent yet (a continue may resend a snapshot)
            out.append((kind, content[done:]))
            self._emitted[key] = len(content)

    def _legacy_append(self, kind: str, text: str, op: Optional[str], out: List[Tuple[str, str]]) -> None:
        fid = "__legacy_" + kind
        fragment = next((f for f in self.fragments if f.get("id") == fid), None)
        if fragment is None:
            fragment = {"id": fid, "type": "THINK" if kind == "reasoning" else "RESPONSE", "content": ""}
            self.fragments.append(fragment)
        self._set_content(fragment, text, "SET" if op == "SET" else "APPEND", out)

    def _index_results(self, results: List[Any]) -> None:
        for result in results:
            if isinstance(result, dict) and isinstance(result.get("cite_index"), int):
                self.search_results[result["cite_index"]] = result

    def _legacy_search_results(self, op: Optional[str], value: Any) -> None:
        if not isinstance(value, list):
            return
        if op != "BATCH":
            self._legacy_results = list(value)
        else:
            for item in value:
                match = re.match(r"^(\d+)/cite_index$", str(item.get("p") if isinstance(item, dict) else ""))
                if match and int(match.group(1)) < len(self._legacy_results):
                    entry = self._legacy_results[int(match.group(1))]
                    if isinstance(entry, dict):
                        entry["cite_index"] = item.get("v")
        self._index_results(self._legacy_results)


# ── citations ───────────────────────────────────────────────────────────────

_CITATION_RE = re.compile(r"\[citation:(\d+)\]")
# A trailing prefix of "[citation:<digits>" that the next delta may complete.
_CITATION_PARTIAL_RE = re.compile(r"\[(?:c(?:i(?:t(?:a(?:t(?:i(?:o(?:n(?::\d*)?)?)?)?)?)?)?)?)?\Z")


class CitationFormatter:
    """Search answers cite sources as "[citation:3]". Like OmniRoute, rewrite them as
    "[3]" (or drop them for *-search-silent models) and list the cited sources at the
    end. Markers split across stream deltas are held back until complete."""

    def __init__(self, mode: str):
        self.mode = mode  # "off" | "number" | "strip"
        self.cited: List[int] = []
        self._pending = ""

    def feed(self, text: str) -> str:
        if self.mode == "off":
            return text
        buf = self._pending + text
        match = _CITATION_PARTIAL_RE.search(buf)
        if match:
            self._pending, buf = buf[match.start() :], buf[: match.start()]
        else:
            self._pending = ""
        return self._convert(buf)

    def flush(self) -> str:
        buf, self._pending = self._pending, ""
        return buf if self.mode == "off" else self._convert(buf)

    def _convert(self, text: str) -> str:
        def replace(match: "re.Match[str]") -> str:
            number = int(match.group(1))
            if number not in self.cited:
                self.cited.append(number)
            return "" if self.mode == "strip" else f"[{number}]"

        return _CITATION_RE.sub(replace, text)

    def references(self, results: Dict[int, Dict[str, Any]]) -> str:
        if self.mode != "number" or not self.cited:
            return ""
        lines = []
        for number in sorted(self.cited):
            result = results.get(number)
            url = str(result.get("url") or "").strip() if result else ""
            if not url:
                continue
            title = str(result.get("title") or result.get("site_name") or url)
            title = " ".join(title.replace("[", "(").replace("]", ")").split())
            if any(ch in url for ch in " ()<>"):
                url = "<" + url.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E") + ">"
            lines.append(f"- [{number}] [{title}]({url})")
        return "\n\nSources:\n" + "\n".join(lines) if lines else ""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (DeepSeek web only reports a running total)."""
    if not text:
        return 0
    wide = sum(1 for ch in text if "⺀" <= ch <= "鿿" or "가" <= ch <= "힯" or "豈" <= ch <= "﫿")
    return wide + (len(text) - wide + 3) // 4


# ════════════════════════════════════════════════════════════════════════════
# Request -> prompt (ports of OmniRoute's messagesToPrompt / tool prompt builders)
# ════════════════════════════════════════════════════════════════════════════

# JavaScript's String.prototype.trim() whitespace set; Python's str.strip() differs slightly.
_JS_WS = "\t\n\v\f\r                  　﻿"


def _js_trim(value: str) -> str:
    return value.strip(_JS_WS)


def _js_stringify(value: Any) -> str:
    """JSON.stringify equivalent (compact separators, raw non-ASCII)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _json_strict(raw: str) -> Any:
    """JSON.parse equivalent: rejects NaN/Infinity like JavaScript does."""

    def reject(token: str) -> Any:
        raise ValueError(f"invalid JSON constant {token}")

    return json.loads(raw, parse_constant=reject)


def _json_or_none(raw: str) -> Any:
    try:
        return _json_strict(raw)
    except (ValueError, RecursionError):
        return None


def _js_truthy(value: Any) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and (value == 0 or value != value):
        return False
    return True


def _js_string(value: Any) -> str:
    """JavaScript's String(value) for the JSON-ish values a message can hold."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def message_text(content: Any, keep_falsy: bool = False) -> str:
    """Text parts of a message. OmniRoute has two variants: messagesToPrompt's
    String(content || "") and the tool prompt's `content == null ? "" : String(content)`
    (keep_falsy=True)."""
    if isinstance(content, list):
        return "\n".join(
            _js_string(item.get("text")) if item.get("text") is not None else ""
            for item in content
            if isinstance(item, dict) and item.get("type") in ("text", "input_text")
        )
    if content is None or (not keep_falsy and not _js_truthy(content)):
        return ""
    return _js_string(content)


_MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\(.*?\)")


def messages_to_prompt(messages: List[Dict[str, Any]], history_window: int = 0) -> str:
    """The web endpoint takes one prompt string, not messages[]. Single-turn requests send
    the system prompt(s) + the last user message; multi-turn requests replay the last N
    turns as a role-tagged transcript (OmniRoute #2942 / #10527)."""
    if not messages:
        return ""
    system_parts: List[str] = []
    conversation: List[Tuple[str, str]] = []
    call_names: Dict[str, str] = {}
    last_user = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _js_trim(message_text(message.get("content")))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif role in ("user", "assistant"):
            if text:
                conversation.append((role, text))
            if role == "user":
                last_user = text
            calls = message.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and call.get("id") and isinstance(fn.get("name"), str):
                    call_names[call["id"]] = fn["name"]
        elif role == "tool":
            # Tool results have no native slot in the single-prompt format (#4712).
            if text:
                name = call_names.get(message.get("tool_call_id") or "") or message.get("name") or "tool"
                conversation.append(("tool", f"({name}) {text}"))

    parts: List[str] = []
    if system_parts:
        parts.append("\n\n".join(system_parts))
    window = history_window if history_window > 0 else (DEFAULT_AUTO_HISTORY_WINDOW if len(conversation) > 1 else 0)
    if window > 0 and len(conversation) > 1:
        labels = {"assistant": "Assistant: ", "tool": "Tool result ", "user": "User: "}
        parts.append("\n\n".join(labels[role] + text for role, text in conversation[-window:]))
    elif last_user:
        parts.append(last_user)
    return _MARKDOWN_IMAGE_RE.sub("", "\n\n".join(parts))


def build_tool_conversation_prompt(messages: List[Dict[str, Any]], tool_system_prompt: str) -> str:
    """Agentic turns replay the WHOLE trajectory, including earlier <tool> calls and every
    tool result, so the model continues instead of restarting (OmniRoute #2820)."""
    system_parts = [tool_system_prompt] if tool_system_prompt else []
    lines: List[str] = []
    call_names: Dict[str, str] = {}
    saw_tool_activity = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in ("system", "developer"):
            text = _js_trim(message_text(message.get("content"), keep_falsy=True))
            if text:
                system_parts.append(text)
        elif role == "user":
            text = _js_trim(message_text(message.get("content"), keep_falsy=True))
            if text:
                lines.append(f"User: {text}")
        elif role == "assistant":
            text = _js_trim(message_text(message.get("content"), keep_falsy=True))
            calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            parts = [text] if text else []
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                name = fn.get("name") if isinstance(fn, dict) and isinstance(fn.get("name"), str) else ""
                raw_args = fn.get("arguments") if isinstance(fn, dict) else None
                if isinstance(raw_args, str) and raw_args:
                    args = raw_args
                else:
                    args = _js_stringify(raw_args if raw_args is not None else {})
                if isinstance(call, dict) and call.get("id"):
                    call_names[call["id"]] = name
                parts.append(f'<tool>{{"name": {_js_stringify(name)}, "arguments": {args}}}</tool>')
                saw_tool_activity = True
            if parts:
                lines.append("Assistant: " + "\n".join(parts))
        elif role == "tool":
            text = _js_trim(message_text(message.get("content"), keep_falsy=True))
            name = call_names.get(message.get("tool_call_id") or "") or message.get("name") or "tool"
            lines.append(f"Tool result ({name}): {text or '(no output)'}")
            saw_tool_activity = True

    parts = []
    if system_parts:
        parts.append("\n\n".join(system_parts))
    if lines:
        parts.append("\n\n".join(lines))
    if saw_tool_activity:
        parts.append(
            "Continue the task using the tool results above. Do NOT repeat tool calls that already "
            "succeeded; perform the next step or give the final answer."
        )
    return _MARKDOWN_IMAGE_RE.sub("", "\n\n".join(parts))


def serialize_deepseek_tool_prompt(tools: Any, nonce: str) -> str:
    """OpenAI tools[] -> DeepSeek tool contract with the per-request nonce binding (#9343)."""
    if not isinstance(tools, list) or not tools or not nonce:
        return ""
    lines: List[str] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        desc = fn["description"] if isinstance(fn.get("description"), str) and fn.get("description") else ""
        try:
            schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else None
            if schema is not None:
                properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
                required = [r for r in schema.get("required") or [] if isinstance(r, str)] if isinstance(
                    schema.get("required"), list
                ) else []
                bound = dict(schema)
                bound["properties"] = {**properties, "_nonce": {"type": "string", "const": nonce}}
                bound["required"] = list(dict.fromkeys(required + ["_nonce"]))
            else:
                bound = fn.get("parameters")
            params = _js_stringify(bound) if _js_truthy(bound) else ""
        except (TypeError, ValueError):
            params = ""
        lines.append(f"- {fn['name']}{': ' + desc if desc else ''}{chr(10) + '  parameters: ' + params if params else ''}")
    if not lines:
        return ""
    return "\n".join(
        [
            "You can call tools. To call a tool, output ONLY this exact block (no markdown fence):",
            f'<tool>{{"name": "<tool_name>", "arguments": {{ ... }}, "_nonce": "{nonce}"}}</tool>',
            "Rules:",
            "- Use exactly <tool>...</tool>. Do NOT use <tool:name>, <tool_call>, <name>, <parameter>, "
            "id=/name= attributes, <｜｜DSML｜｜> markers, or code fences.",
            f'- Include the secret binding "_nonce": "{nonce}" exactly as shown.',
            '- "name" must be one of the tools below; "arguments" must be a JSON object.',
            "- When a tool is needed, emit the <tool> block instead of only describing the plan.",
            "- Emit one <tool> block per call; you may put several blocks back to back.",
            "- If your runtime forces DSML instead, every DSML invoke MUST include this exact binding:",
            f'<｜｜DSML｜｜ parameter name="_nonce" string="true">{nonce}</｜｜DSML｜｜ parameter>',
            "  A DSML invoke without that binding is invalid and will be rejected.",
            "- If no tool is needed, just answer normally without any <tool> block.",
            "",
            "Available tools:",
            *lines,
        ]
    )


def resolve_model_options(model: str, body: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """(model_type, thinking, search) from the model name and request flags (OmniRoute's
    resolveModelOptions; a "thinking" object or reasoning_effort "none" also count)."""
    name = (model or "").lower()
    model_type = "expert" if "pro" in name or "expert" in name else "default"
    thinking_flag = body.get("thinking")
    effort = body.get("reasoning_effort")
    thinking = (
        any(key in name for key in ("r1", "think", "reason"))
        or body.get("thinking_enabled") is True
        or thinking_flag is True
        or (isinstance(thinking_flag, dict) and thinking_flag.get("type") == "enabled")
        or (_js_truthy(effort) and str(effort).lower() != "none")
    )
    search = (
        "search" in name
        or body.get("search_enabled") is True
        or body.get("search") is True
        or body.get("web_search") is True
        or isinstance(body.get("web_search_options"), dict)
    )
    return model_type, bool(thinking), bool(search)


# ════════════════════════════════════════════════════════════════════════════
# Tool calling
#
# chat.deepseek.com has no native function calling, so (like OmniRoute, #2820) the
# OpenAI tools[] are serialized into a prompt contract and the model's text reply is
# parsed back into OpenAI tool_calls. DeepSeek answers in many ad-hoc shapes:
#   <tool>{"name":..,"arguments":..}</tool>      canonical
#   <tool:todowrite>{json}</tool>                name in the tag suffix
#   <tool_call>{id,type,params}</tool_call>      alternate key names
#   <tool name="x">{json}</tool>, <tool id="x">  name in an attribute
#   <tool><tool ...>{json}</tool></tool>         doubled wrappers
#   <tool id="1"><name>x</name><arguments>{json}</arguments></tool>
#   <tool:write><parameter name="content" content="...">
#   <｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="bash">...   DeepSeek's native DSML
#   <｜tool▁call▁begin｜>name<｜tool▁call▁argument▁begin｜>{json}...   native tokens
# This is a line-by-line port of open-sse/translator/webTools.ts,
# deepseekWebTools.ts and deepseekWebToolBinding.ts.
# ════════════════════════════════════════════════════════════════════════════

_MISSING = object()  # JavaScript's `undefined`, where it differs from null


def new_tool_nonce() -> str:
    # Always starts with a letter so a DSML parameter value never parses as a JSON number.
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(string.ascii_lowercase + string.digits) for _ in range(7)
    )


class ToolContext:
    """The requested tools plus this request's nonce (OmniRoute keeps it in a WeakMap)."""

    def __init__(self, tools: Any, nonce: Optional[str] = None):
        self.tools: List[Any] = tools if isinstance(tools, list) else []
        self.nonce = (nonce or new_tool_nonce()) if self.tools else ""
        self.requested = get_requested_tool_names(self.tools)


# ── webTools.ts ─────────────────────────────────────────────────────────────


def get_requested_tool_names(tools: Any) -> List[Tuple[str, str]]:
    if not isinstance(tools, list):
        return []
    names: List[Tuple[str, str]] = []
    seen = set()
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = _js_trim(fn["name"]) if isinstance(fn, dict) and isinstance(fn.get("name"), str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        names.append((name, _normalize_tool_name(name)))
    return names


def _normalize_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost)
        previous = current
    return previous[len(b)]


def _score_tool_name(emitted: str, original: str, normalized_requested: str) -> float:
    if emitted == original:
        return 1.0
    normalized = _normalize_tool_name(emitted)
    if not normalized or not normalized_requested:
        return 0.0
    if normalized == normalized_requested:
        return 0.98
    shorter = min(len(normalized), len(normalized_requested))
    longer = max(len(normalized), len(normalized_requested))
    if shorter >= 4 and (normalized in normalized_requested or normalized_requested in normalized):
        return 0.86 - (longer - shorter) / max(longer, 1) / 4
    similarity = 1 - _levenshtein(normalized, normalized_requested) / max(longer, 1)
    return similarity if similarity >= 0.72 else 0.0


def resolve_requested_tool_name(emitted: str, requested: List[Tuple[str, str]]) -> Optional[str]:
    """Map an emitted name onto a requested tool (exact, normalized or fuzzy match)."""
    if not requested:
        return emitted
    best_name, best_score, second = None, None, 0.0
    for original, normalized in requested:
        score = _score_tool_name(emitted, original, normalized)
        if best_score is None or score > best_score:
            second = best_score if best_score is not None else 0.0
            best_name, best_score = original, score
        elif score > second:
            second = score
    if best_score is None or best_score < 0.72:
        return None
    if best_score < 0.98 and best_score - second < 0.08:  # ambiguous fuzzy match
        return None
    return best_name


def _strip_code_fence(value: str) -> str:
    text = _js_trim(value)
    text = re.sub(r"^```(?:json|javascript|js|python)?\s*", "", text, count=1, flags=re.I)
    text = re.sub(r"\s*```\Z", "", text, count=1, flags=re.I)
    return _js_trim(text)


def _convert_single_quoted_strings(value: str) -> str:
    result: List[str] = []
    in_single = in_double = escaped = False
    for ch in value:
        if escaped:
            result.append('\\"' if ch == '"' and in_single else ch)
            escaped = False
            continue
        if ch == "\\":
            result.append(ch)
            escaped = True
            continue
        if ch == '"':
            if in_single:
                result.append('\\"')
            else:
                in_double = not in_double
                result.append(ch)
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            result.append('"')
            continue
        result.append(ch)
    return "".join(result)


def _replace_python_literals(value: str) -> str:
    result: List[str] = []
    token: List[str] = []
    in_string = escaped = False

    def flush() -> None:
        word = "".join(token)
        result.append({"True": "true", "False": "false", "None": "null"}.get(word, word))
        token.clear()

    for ch in value:
        if escaped:
            if token:
                flush()
            result.append(ch)
            escaped = False
            continue
        if ch == "\\":
            if token:
                flush()
            result.append(ch)
            escaped = in_string
            continue
        if ch == '"':
            if token:
                flush()
            in_string = not in_string
            result.append(ch)
            continue
        if not in_string and ("A" <= ch <= "Z" or "a" <= ch <= "z"):
            token.append(ch)
            continue
        if token:
            flush()
        result.append(ch)
    if token:
        flush()
    return "".join(result)


def _normalize_loose_json(value: str) -> str:
    text = _replace_python_literals(_convert_single_quoted_strings(value))
    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)", r'\1"\2"\3', text)
    return re.sub(r",\s*([}\]])", r"\1", text)


def parse_loose_json_object(raw: Any) -> Optional[Dict[str, Any]]:
    """JSON object from model output, tolerating code fences, single quotes, Python
    literals, unquoted keys and trailing commas."""
    if not isinstance(raw, str):
        return None
    trimmed = _strip_code_fence(raw)
    for candidate in (trimmed, _normalize_loose_json(trimmed)):
        try:
            parsed = _json_strict(candidate)
        except (ValueError, RecursionError):
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def strip_ranges(text: str, ranges: List[Tuple[int, int]]) -> str:
    # Merge overlapping ranges first. OmniRoute cuts a call and its enclosing DSML wrapper
    # separately, so the second cut (made with stale offsets) eats text after the wrapper.
    merged: List[List[int]] = []
    for start, end in sorted(ranges, key=lambda r: r[0]):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    content = text
    for start, end in reversed(merged):
        line_start = content.rfind("\n", 0, start) + 1
        next_break = content.find("\n", end)
        line_end = len(content) if next_break == -1 else next_break
        whole_line = _js_trim(content[line_start:start]) == "" and _js_trim(content[end:line_end]) == ""
        cut_start = line_start if whole_line else start
        if whole_line:
            cut_end = next_break + 1 if next_break != -1 else line_end
        else:
            cut_end = end
        content = content[:cut_start] + content[cut_end:]
    return _js_trim(re.sub(r"\n{3,}", "\n\n", content))


def to_arguments_string(value: Any = _MISSING) -> str:
    if value is _MISSING:
        return "{}"
    if isinstance(value, str):
        parsed = parse_loose_json_object(value)
        return _js_stringify(parsed) if parsed is not None else value
    try:
        return _js_stringify(value)
    except (TypeError, ValueError):
        return "{}"


def _find_tag_blocks(text: str, open_re: "re.Pattern[str]", close_re: "re.Pattern[str]") -> List[Tuple[int, int, str]]:
    """(start, end, inner) for each open...close block, scanning forward once."""
    blocks = []
    position = 0
    while True:
        opening = open_re.search(text, position)
        if not opening:
            break
        closing = close_re.search(text, opening.end())
        if not closing:
            break
        blocks.append((opening.start(), closing.end(), text[opening.end() : closing.start()]))
        position = closing.end()
    return blocks


_TOOL_OPEN_RE = re.compile(r"<tool>")
_TOOL_CLOSE_RE = re.compile(r"</tool>")
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call(?:\s[^<>]*)?>")
_TOOL_CALL_CLOSE_RE = re.compile(r"</tool_call>")
_DSML_PARAM_RE = re.compile(
    r"<(?:｜｜DSML｜｜|\|DSML\|)\s*parameter\s+name=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)"
    r"(?:</?(?:｜｜DSML｜｜|\|DSML\|)\s*parameter\s*>"
    r"|(?=<(?:｜｜DSML｜｜|\|DSML\|)\s*parameter\s+name=|</(?:｜｜DSML｜｜|\|DSML\|)\s*invoke\s*>))"
)
_DSML_INVOKE_NAMED_RE = re.compile(
    r"<(?P<dsml>｜｜DSML｜｜|\|DSML\|)\s*invoke\s+name=[\"']([^\"']+)[\"']\s*>([\s\S]*?)</(?P=dsml)\s*invoke\s*>"
)
_DSML_CALLS_TAG_RE = re.compile(r"</?(?:｜｜DSML｜｜|\|DSML\|)\s*calls\s*>")


def _normalize_dsml_tool_calls(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        name, body = match.group(2), match.group(3)
        parameters: Dict[str, Any] = {}
        for param in _DSML_PARAM_RE.finditer(body):
            raw = _js_trim(param.group(2))
            try:
                parameters[param.group(1)] = _json_strict(raw)
            except (ValueError, RecursionError):
                parameters[param.group(1)] = raw
        if not parameters:
            parsed = _json_or_none(_js_trim(body) or "{}")
            if isinstance(parsed, dict):
                parameters.update(parsed)
        return f'<tool>{{"name":{_js_stringify(name)},"arguments":{_js_stringify(parameters)}}}</tool>'

    return _DSML_CALLS_TAG_RE.sub("", _DSML_INVOKE_NAMED_RE.sub(replace, text))


def parse_tool_calls_from_text(text: Any, id_seed: str, ctx: ToolContext) -> Tuple[Any, Optional[List[Dict[str, Any]]]]:
    """Canonical <tool>/<tool_call> parser. Bare JSON is never promoted (#9343)."""
    normalized = _normalize_dsml_tool_calls(text) if isinstance(text, str) else text
    if not isinstance(normalized, str) or ("<tool>" not in normalized and "<tool_call" not in normalized):
        return (text if text is not None else ""), None
    candidates = sorted(
        _find_tag_blocks(normalized, _TOOL_OPEN_RE, _TOOL_CLOSE_RE)
        + _find_tag_blocks(normalized, _TOOL_CALL_OPEN_RE, _TOOL_CALL_CLOSE_RE),
        key=lambda block: block[0],
    )
    tool_calls: List[Dict[str, Any]] = []
    accepted: List[Tuple[int, int]] = []
    for start, end, inner in candidates:
        parsed = parse_loose_json_object(_js_trim(inner))
        emitted = None
        if parsed is not None:
            if isinstance(parsed.get("name"), str):
                emitted = parsed["name"]
            elif isinstance(parsed.get("command"), str):
                emitted = parsed["command"]
        if not emitted:
            continue
        if ctx.nonce and "_nonce" in parsed and parsed["_nonce"] != ctx.nonce:
            continue  # wrong nonce: copy attack or hallucination
        name = resolve_requested_tool_name(emitted, ctx.requested) or emitted
        tool_calls.append(
            {
                "id": f"{id_seed}_{len(tool_calls)}",
                "type": "function",
                "function": {"name": name, "arguments": to_arguments_string(parsed.get("arguments", _MISSING))},
            }
        )
        accepted.append((start, end))
    if not tool_calls:
        return text, None
    return strip_ranges(normalized, accepted), tool_calls


# ── deepseekWebToolBinding.ts ───────────────────────────────────────────────


def validate_tool_arguments(tools: Any, name: str, args: Dict[str, Any]) -> bool:
    definition = None
    if isinstance(tools, list):
        definition = next(
            (t for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name") == name),
            None,
        )
    if definition is None:
        return False
    fn = definition["function"]
    schema = fn.get("parameters", _MISSING)
    if schema is _MISSING:
        return True
    if not isinstance(schema, (dict, bool)):
        return False
    return json_schema_valid(schema, args)


def finalize_bound_calls(raw_text: str, parsed: Tuple[Any, Any], ctx: ToolContext) -> Tuple[Any, Any]:
    """Full nonce + schema contract only for calls whose arguments carry _nonce."""
    content, calls = parsed
    if not calls:
        return parsed
    cleaned = []
    for call in calls:
        args = parse_loose_json_object(call["function"]["arguments"])
        if args is None:
            return raw_text, None
        if "_nonce" not in args:
            cleaned.append(call)
            continue
        if args["_nonce"] != ctx.nonce:
            return raw_text, None
        rest = {k: v for k, v in args.items() if k != "_nonce"}
        if not validate_tool_arguments(ctx.tools, call["function"]["name"], rest):
            return raw_text, None
        cleaned.append({**call, "function": {**call["function"], "arguments": _js_stringify(rest)}})
    return content, cleaned


def finalize_parsed_tool_calls(raw_text: str, parsed: Tuple[Any, Any], ctx: ToolContext) -> Tuple[Any, Any]:
    content, calls = parsed
    if not calls:
        return parsed
    cleaned = []
    for call in calls:
        args = parse_loose_json_object(call["function"]["arguments"])
        if args is None:
            return raw_text, None
        if "_nonce" in args and args["_nonce"] != ctx.nonce:
            return raw_text, None
        clean = {k: v for k, v in args.items() if k != "_nonce"}
        if not validate_tool_arguments(ctx.tools, call["function"]["name"], clean):
            return raw_text, None
        cleaned.append({**call, "function": {**call["function"], "arguments": _js_stringify(clean)}})
    return content, cleaned


_MALFORMED_DSML_RE = re.compile(r"</?(?:｜｜|\|\|?)DSML(?:｜｜|\|\|?)\s*(?:calls|invoke|parameter)\b", re.I)
_MALFORMED_TOOL_RE = re.compile(r"</?(?:tool|tool_call)(?::|\s|>)", re.I)


def has_malformed_deepseek_tool_markup(text: Any) -> bool:
    if not isinstance(text, str) or not _js_trim(text):
        return False
    return bool(_MALFORMED_DSML_RE.search(text) or _MALFORMED_TOOL_RE.search(text))


def has_malformed_deepseek_tool_intent(text: Any, ctx: ToolContext) -> bool:
    """True only for output that expresses tool intent but failed safe parsing."""
    if has_malformed_deepseek_tool_markup(text):
        return True
    if not isinstance(text, str) or not _js_trim(text):
        return False
    parsed = parse_loose_json_object(text)
    if parsed is None or "arguments" not in parsed:
        return False
    emitted = next((v for v in (parsed.get("name"), parsed.get("command")) if isinstance(v, str) and v), None)
    return bool(emitted) and bool(resolve_requested_tool_name(emitted, ctx.requested))


# ── deepseekWebTools.ts ─────────────────────────────────────────────────────


class _Tag:
    __slots__ = ("start", "end", "closing", "suffix", "attrs")

    def __init__(self, start: int, end: int, closing: bool = False, suffix: str = "", attrs: str = ""):
        self.start, self.end, self.closing, self.suffix, self.attrs = start, end, closing, suffix, attrs


class _Block:
    __slots__ = ("open", "close", "inner_start", "inner_end")

    def __init__(self, open_tag: _Tag, close_tag: _Tag, inner_start: int, inner_end: int):
        self.open, self.close, self.inner_start, self.inner_end = open_tag, close_tag, inner_start, inner_end


_TAG_TOKEN_RE = re.compile(r"<(/?)(?:tool_call|tool)(:[A-Za-z0-9_.+-]+)?((?:\s[^>]*)?)/?>")


def _tokenize_tool_tags(text: str) -> List[_Tag]:
    return [
        _Tag(m.start(), m.end(), m.group(1) == "/", m.group(2)[1:] if m.group(2) else "", m.group(3) or "")
        for m in _TAG_TOKEN_RE.finditer(text)
    ]


def _pair_tool_blocks(tokens: List[_Tag], text_len: int) -> List[_Block]:
    """Each closing tag pairs with the nearest unmatched opening tag; leftover openings get
    a synthetic close at the end of the text so their body is still parsed."""
    blocks: List[_Block] = []
    stack: List[_Tag] = []
    for token in tokens:
        if not token.closing:
            stack.append(token)
            continue
        if not stack:
            continue
        opening = stack.pop()
        blocks.append(_Block(opening, token, opening.end, token.start))
    for opening in stack:
        blocks.append(_Block(opening, _Tag(text_len, text_len, True), opening.end, text_len))
    return blocks


def _get_attr(attrs: str, name: str) -> Optional[str]:
    """Attribute value, tolerating backslash-escaped quotes inside the value."""
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*(\"|')", attrs)
    if not match:
        return None
    quote = match.group(1)
    j = match.end()
    out: List[str] = []
    while j < len(attrs):
        ch = attrs[j]
        if ch == "\\":
            out.append(attrs[j + 1] if j + 1 < len(attrs) else "")
            j += 2
            continue
        if ch == quote:
            break
        out.append(ch)
        j += 1
    return "".join(out)


def _get_xml_child(inner: str, tag: str) -> Optional[str]:
    match = re.search(rf"<{tag}\b[^>]*>([\s\S]*?)</{tag}>", inner, re.I)
    return _js_trim(match.group(1)) if match else None


_PARAM_TAG_RE = re.compile(r"<parameter\b([^>]*?)/?>(?:((?:(?!<parameter\b)[\s\S])*?)</parameter>)?", re.I)


def _build_args_from_parameters(inner: str) -> Optional[Dict[str, Any]]:
    out: Dict[str, Any] = {}
    for match in _PARAM_TAG_RE.finditer(inner):
        attrs, body = match.group(1) or "", match.group(2)
        name = _get_attr(attrs, "name")
        if not name:
            continue
        value = _get_attr(attrs, "content")
        out[name] = value if value is not None else (_js_trim(body) if body is not None else "")
    return out or None


def _as_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _build_schema_param_map(tools: Any) -> Dict[str, set]:
    result: Dict[str, set] = {}
    for tool in tools if isinstance(tools, list) else []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters")
        props = params.get("properties") if isinstance(params, dict) else None
        result[fn["name"]] = set(props.keys()) if isinstance(props, dict) else set()
    return result


def _salvage_leading_json_object(text: str) -> Optional[str]:
    """First balanced {...} at the start of a block, ignoring trailing garbage."""
    start = text.find("{")
    if start == -1:
        return None
    depth, quote, escaped = 0, "", False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if quote:
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_call(
    tag_name: str, inner_raw: str, requested: List[Tuple[str, str]], schema_map: Dict[str, set]
) -> Optional[Tuple[str, str]]:
    """One tool block -> (name, JSON arguments), or None without a plausible name."""
    inner = _js_trim(inner_raw)
    name_child = _get_xml_child(inner, "name")
    args_child = _get_xml_child(inner, "arguments")
    if args_child is None:
        args_child = _get_xml_child(inner, "parameters")
    param_obj = None if args_child else _build_args_from_parameters(inner)
    has_xml = bool(name_child) or bool(args_child) or bool(param_obj)

    obj = None if has_xml else parse_loose_json_object(inner)
    if obj is None and not has_xml:
        salvaged = _salvage_leading_json_object(inner)
        if salvaged:
            obj = parse_loose_json_object(salvaged)
    json_name = (_as_string(obj.get("name")) or _as_string(obj.get("type"))) if obj is not None else None

    child_resolved = resolve_requested_tool_name(name_child, requested) if name_child else None
    json_resolved = resolve_requested_tool_name(json_name, requested) if json_name else None
    tag_resolved = resolve_requested_tool_name(tag_name, requested) if tag_name else None

    # Prefer names that map to a requested tool; the JSON body beats the tag attribute (#3260).
    name, name_from_tag = None, False
    for value, from_tag in (
        (child_resolved, False),
        (json_resolved, False),
        (tag_resolved, True),
        (name_child, False),
        (json_name, False),
        (tag_name, True),
    ):
        if not name and value:
            name, name_from_tag = value, from_tag

    if not name and not tag_name and obj is not None:
        command = _as_string(obj.get("command"))
        resolved = resolve_requested_tool_name(command, requested) if command else None
        if resolved:
            name, name_from_tag = resolved, False

    # Nameless <parameter> block (#5154): adopt the one tool whose schema covers every key.
    if not name and param_obj and schema_map:
        keys = list(param_obj.keys())
        if keys:
            candidates = [tool for tool, props in schema_map.items() if props and all(k in props for k in keys)]
            if len(candidates) == 1:
                name, name_from_tag = candidates[0], False

    if not name:
        return None

    if args_child:
        parsed_args = parse_loose_json_object(args_child)
        args_value: Any = parsed_args if parsed_args is not None else args_child
    elif param_obj:
        args_value = param_obj
    elif obj is not None:
        if "arguments" in obj:
            args_value = obj["arguments"]
        elif "params" in obj:
            args_value = obj["params"]
        elif name_from_tag:
            args_value = obj  # <tool:bash>{"command": ...}: the object is the arguments
        else:
            args_value = {
                k: v for k, v in obj.items() if k not in ("name", "type", "id", "command", "arguments", "params")
            }
    else:
        args_value = {}
    return name, to_arguments_string(args_value)


# DeepSeek's native double-pipe DSML dialect; closers are sloppy in the wild.
_DSML_PIPE = "[｜|]"
_DSML_MARK = f"{_DSML_PIPE}{{2}}DSML{_DSML_PIPE}{{2}}"
_HAS_DSML_RE = re.compile(rf"</?{_DSML_MARK}\s*(?:calls|invoke|parameter)\b", re.I)
_DSML_ANY_TAG_RE = re.compile(
    rf"</?{_DSML_MARK}\s*(?:calls|invoke|parameter)\b[^>]*>|<{_DSML_MARK}\s*/\s*(?:calls|invoke|parameter)\s*>",
    re.I,
)
_DSML_KIND_RES = {
    kind: re.compile(rf"(?:</?{_DSML_MARK}\s*{kind}\b([^>]*)>|<{_DSML_MARK}\s*/\s*{kind}\s*>)", re.I)
    for kind in ("invoke", "parameter")
}
_DSML_CALLS_BLOCK_RE = re.compile(
    rf"<{_DSML_MARK}\s*calls\s*>([\s\S]*?)(?:</{_DSML_MARK}\s*calls\s*>|<{_DSML_MARK}\s*/\s*calls\s*>)", re.I
)
_DSML_CALLS_CLOSE_RE = re.compile(rf"(?:</{_DSML_MARK}\s*calls\s*>|<{_DSML_MARK}\s*/\s*calls\s*>)\s*\Z", re.I)


class _DsmlCall:
    __slots__ = ("start", "end", "name", "args")

    def __init__(self, start: int, end: int, name: str, args: Dict[str, Any]):
        self.start, self.end, self.name, self.args = start, end, name, args


def _collect_dsml_tags(source: str, kind: str, base: int) -> List[_Tag]:
    return [_Tag(base + m.start(), base + m.end(), attrs=m.group(1) or "") for m in _DSML_KIND_RES[kind].finditer(source)]


def _build_dsml_call(
    open_tag: _Tag, close_tag: Optional[_Tag], text: str, text_end: int, requested: List[Tuple[str, str]]
) -> Optional[_DsmlCall]:
    raw_name = _get_attr(open_tag.attrs, "name")
    if not raw_name:
        return None
    inner_end = close_tag.start if close_tag else text_end
    ptags = _collect_dsml_tags(text[open_tag.end : inner_end], "parameter", open_tag.end)
    args: Dict[str, Any] = {}
    consumed_end = open_tag.end
    for k, p_open in enumerate(ptags):
        p_name = _get_attr(p_open.attrs, "name")
        if not p_name:
            continue
        p_close = ptags[k + 1] if k + 1 < len(ptags) else None
        raw_body = _js_trim(text[p_open.end : p_close.start if p_close else inner_end])
        if raw_body != "":
            args[p_name] = raw_body
        else:
            content = _get_attr(p_open.attrs, "content")
            args[p_name] = content if content is not None else ""
        consumed_end = p_close.end if p_close else inner_end
    if not any(v != "" for v in args.values()):
        return None  # empty or ambiguous invoke: never emit a half-built call
    name = resolve_requested_tool_name(raw_name, requested) or raw_name
    return _DsmlCall(open_tag.start, close_tag.end if close_tag else consumed_end, name, args)


def _parse_dsml_blocks(text: str, requested: List[Tuple[str, str]]) -> Tuple[List[_DsmlCall], List[Tuple[int, int]]]:
    calls: List[_DsmlCall] = []
    strip: List[Tuple[int, int]] = []
    envelopes: List[Tuple[int, int]] = []

    def take(tags: List[_Tag], text_end: int) -> None:
        i = 0
        while i < len(tags):
            if not _get_attr(tags[i].attrs, "name"):
                i += 1  # stray closer: skip without consuming the next tag
                continue
            nxt = tags[i + 1] if i + 1 < len(tags) else None
            call = _build_dsml_call(tags[i], nxt, text, text_end, requested)
            if call:
                calls.append(call)
            i += 2 if nxt else 1

    for match in _DSML_CALLS_BLOCK_RE.finditer(text):
        envelopes.append((match.start(), match.end()))
        body = match.group(1)
        whole = match.group(0)
        body_base = match.start() + whole.index(body)
        close = _DSML_CALLS_CLOSE_RE.search(whole)
        strip.append((match.start(), body_base))
        strip.append((match.start() + (close.start() if close else len(whole)), match.end()))
        take(_collect_dsml_tags(body, "invoke", body_base), body_base + len(body))
    root = [
        t for t in _collect_dsml_tags(text, "invoke", 0) if not any(t.start >= s and t.end <= e for s, e in envelopes)
    ]
    take(root, len(text))
    calls.sort(key=lambda c: c.start)
    return calls, strip


_DSML_INVOKE_OPEN_RE = re.compile(r"<[｜|]{1,2}DSML[｜|]{1,2}\s+(calls|invoke|parameter)([^>]*)>", re.I)
_DSML_INVOKE_CLOSE_RE = re.compile(r"</[｜|]{1,2}DSML[｜|]{1,2}\s+(calls|invoke|parameter)\s*>", re.I)


def _normalize_dsml_invoke_markup(text: str) -> str:
    if "DSML" not in text:
        return text

    def opener(match: "re.Match[str]") -> str:
        tag = match.group(1).lower()
        if tag == "calls":
            return ""
        name = _get_attr(match.group(2), "name")
        name_attr = f' name="{name}"' if name is not None else ""
        return f"<tool{name_attr}>" if tag == "invoke" else f"<parameter{name_attr}>"

    def closer(match: "re.Match[str]") -> str:
        tag = match.group(1).lower()
        return "" if tag == "calls" else "</tool>" if tag == "invoke" else "</parameter>"

    return _DSML_INVOKE_CLOSE_RE.sub(closer, _DSML_INVOKE_OPEN_RE.sub(opener, text))


_DISCLAIMER_RE = re.compile(r"\s*This response is AI-generated, for reference only\.\s*\Z", re.I)


def _normalize_deepseek_markup(text: str) -> str:
    text = _normalize_dsml_invoke_markup(text)
    if (
        re.search(r"[｜|]{1,2}DSML[｜|]{1,2}", text, re.I)
        or re.search(r"<parameter\b", text, re.I)
        or re.search(r"</(?:parameter|invoke|arguments|calls|tool_calls|function_calls)>", text, re.I)
        or "This response is AI-generated, for reference only." in text
    ):
        text = _DISCLAIMER_RE.sub("", text, count=1)
        text = re.sub(r"</?(?:[｜|]{1,2})DSML(?:[｜|]{1,2})\s*(?:calls|tool_calls|function_calls)>", "", text, flags=re.I)
        text = re.sub(r"<(/?)(?:(?:[｜|]{1,2})DSML(?:[｜|]{1,2})\s*)?invoke\b", r"<\1tool", text, flags=re.I)
        text = re.sub(r"<(/?)(?:(?:[｜|]{1,2})DSML(?:[｜|]{1,2})\s*)?parameter\b", r"<\1parameter", text, flags=re.I)
        text = re.sub(r"(}\s*)</(?:parameter|invoke|calls|tool_calls|function_calls)>", r"\1", text, flags=re.I)
        if re.search(r"<parameter\b", text, re.I) and not re.search(r"<tool\b", text, re.I):
            text = re.sub(r"((?:<parameter\b[\s\S]*?</parameter>\s*)+)", r"<tool>\1</tool>", text, flags=re.I)
    return text


# WMAdapter-compatible native output: printable-ASCII and full-width token spellings.
_NATIVE_DSML_INVOKE_RE = re.compile(
    r"<(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))invoke\b([^>]*)>([\s\S]*?)</(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))invoke\s*>",
    re.I,
)
_NATIVE_DSML_PARAMETER_RE = re.compile(
    r"<(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))parameter\b([^>]*)>([\s\S]*?)"
    r"(?:</(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))parameter\s*>|<(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))parameter\s*>)",
    re.I,
)
_NATIVE_DSML_TOKEN_RE = re.compile(
    r"<(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))(?:calls|call|invoke|parameter)\b[^>]*>[\s\S]*?"
    r"</(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))(?:calls|call|invoke|parameter)\s*>",
    re.I,
)
_NATIVE_TOOL_CALL_TOKEN_RE = re.compile(
    r"(?:<\|tool_call_begin\|>|<｜tool▁call▁begin｜>)[\s\n]*([^<\s]+)[\s\n]*"
    r"(?:<\|tool_call_argument_begin\|>|<｜tool▁call▁argument▁begin｜>)([\s\S]*?)"
    r"(?:<\|tool_call_argument_end\|>|<｜tool▁call▁argument▁end｜>)[\s\n]*"
    r"(?:<\|tool_call_end\|>|<｜tool▁call▁end｜>)"
)
_NATIVE_DSML_WRAPPER_RE = re.compile(
    r"<(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))calls\s*>[\s\S]*?</(?:(?:\|DSML\|)|(?:｜｜DSML｜｜))calls\s*>", re.I
)


def _normalize_native_arguments(args: Dict[str, Any], name: str, tools: Any) -> Dict[str, Any]:
    """Schema-guided coercion: string-typed properties get JSON-encoded if not strings."""
    tool = None
    if isinstance(tools, list):
        tool = next(
            (t for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name") == name),
            None,
        )
    params = tool["function"].get("parameters") if tool else None
    properties = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(properties, dict):
        return args
    for key, value in list(args.items()):
        prop = properties.get(key)
        if isinstance(prop, dict) and prop.get("type") == "string" and not isinstance(value, str):
            args[key] = _js_stringify(value)
    return args


def _parse_native_deepseek_calls(text: str, id_seed: str, ctx: ToolContext) -> Tuple[str, Any, bool]:
    calls: List[Dict[str, Any]] = []
    ranges: List[Tuple[int, int]] = []
    for match in _NATIVE_TOOL_CALL_TOKEN_RE.finditer(text):
        resolved = resolve_requested_tool_name(match.group(1), ctx.requested)
        parsed = _json_or_none(_js_trim(match.group(2)))
        if not resolved or not isinstance(parsed, dict):
            continue
        args = _normalize_native_arguments(parsed, resolved, ctx.tools)
        calls.append({"id": f"{id_seed}_{len(calls)}", "type": "function", "function": {"name": resolved, "arguments": _js_stringify(args)}})
        ranges.append(match.span())
    for match in _NATIVE_DSML_INVOKE_RE.finditer(text):
        name = _get_attr(match.group(1) or "", "name")
        resolved = resolve_requested_tool_name(name, ctx.requested) if name else None
        if not resolved:
            continue
        args, found = {}, False
        for param in _NATIVE_DSML_PARAMETER_RE.finditer(match.group(2)):
            param_name = _get_attr(param.group(1) or "", "name")
            if not param_name:
                continue
            raw = _js_trim(param.group(2))
            value = _json_or_none(raw)
            args[param_name] = value if value is not None else raw
            found = True
        if not found and _js_trim(match.group(2)):
            continue  # an invoke without parameters is valid; a malformed body is not
        args = _normalize_native_arguments(args, resolved, ctx.tools)
        calls.append({"id": f"{id_seed}_{len(calls)}", "type": "function", "function": {"name": resolved, "arguments": _js_stringify(args)}})
        ranges.append(match.span())
    if calls:
        for wrapper in _NATIVE_DSML_WRAPPER_RE.finditer(text):
            if any(s >= wrapper.start() and e <= wrapper.end() for s, e in ranges):
                ranges.append(wrapper.span())
    recognized = bool(_NATIVE_DSML_TOKEN_RE.search(text)) or bool(re.search(r"<(?:(?:\|tool_call_)|(?:｜tool▁call▁))", text))
    if not calls:
        return text, None, recognized
    return strip_ranges(text, ranges), calls, recognized


def _extract_full_width_dsml_args(body: str) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    for param in _DSML_PARAM_RE.finditer(body):
        raw = _js_trim(param.group(2))
        value = _json_or_none(raw)
        args[param.group(1)] = value if value is not None else raw
    if args:
        return args
    parsed = _json_or_none(_js_trim(body) or "{}")
    if isinstance(parsed, dict):
        args.update(parsed)
    return args


def _parse_full_width_dsml_calls(text: str, id_seed: str, ctx: ToolContext) -> Tuple[str, Any, bool]:
    """Nonce-bound, schema-validated DSML invokes (OmniRoute #15448)."""
    if not re.search(r"<(?:(?:｜｜DSML｜｜)|(?:\|DSML\|))", text):
        return text, None, False
    calls: List[Dict[str, Any]] = []
    ranges: List[Tuple[int, int]] = []
    saw_invoke = rejected = False
    for match in _DSML_INVOKE_NAMED_RE.finditer(text):
        saw_invoke = True
        resolved = resolve_requested_tool_name(match.group(2), ctx.requested)
        if not ctx.nonce or not resolved:
            rejected = True
            continue
        parsed_args = _extract_full_width_dsml_args(match.group(3))
        nested = parsed_args.get("arguments") if isinstance(parsed_args.get("arguments"), dict) else None
        emitted_nonce = parsed_args.get("_nonce")
        if emitted_nonce is None and nested is not None:
            emitted_nonce = nested.get("_nonce")
        if emitted_nonce != ctx.nonce:
            rejected = True
            continue
        if nested is not None:
            args = {k: v for k, v in nested.items() if k != "_nonce"}
        else:
            args = {k: v for k, v in parsed_args.items() if k not in ("_nonce", "name")}
        if not validate_tool_arguments(ctx.tools, resolved, args):
            rejected = True
            continue
        calls.append({"id": f"{id_seed}_{len(calls)}", "type": "function", "function": {"name": resolved, "arguments": _js_stringify(args)}})
        ranges.append(match.span())
    if rejected or not calls:
        return text, None, saw_invoke
    for envelope in _DSML_CALLS_TAG_RE.finditer(text):
        ranges.append(envelope.span())
    return strip_ranges(text, ranges), calls, True


def parse_deepseek_tool_calls(text: Any, id_seed: str, ctx: ToolContext) -> Tuple[Any, Optional[List[Dict[str, Any]]]]:
    """DeepSeek reply text -> (content with tool blocks stripped, OpenAI tool_calls or None)."""
    if not isinstance(text, str) or text == "":
        return (text if isinstance(text, str) else ""), None
    text = _DISCLAIMER_RE.sub("", text, count=1)
    requested = ctx.requested
    dsml_parsed = _parse_dsml_blocks(text, requested) if _HAS_DSML_RE.search(text) else None
    dsml = dsml_parsed if dsml_parsed and dsml_parsed[0] else None
    if dsml is None:
        has_tag_block = bool(re.search(r"<tool\b|<tool:", text))

        def claims(calls: Any) -> bool:
            return bool(calls) or not has_tag_block

        content, calls, recognized = _parse_full_width_dsml_calls(text, id_seed, ctx)
        if recognized and claims(calls):
            return finalize_parsed_tool_calls(text, (content, calls), ctx)
        content, calls, recognized = _parse_native_deepseek_calls(text, id_seed, ctx)
        if recognized and claims(calls):
            return finalize_parsed_tool_calls(text, (content, calls), ctx)
        text = _normalize_deepseek_markup(text)

    tokens = _tokenize_tool_tags(text)
    if not tokens and dsml is None:
        return finalize_parsed_tool_calls(text, parse_tool_calls_from_text(text, id_seed, ctx), ctx)

    schema_map = _build_schema_param_map(ctx.tools)
    blocks = _pair_tool_blocks(tokens, len(text))
    leaves = [
        b
        for b in blocks
        if not any(o is not b and o.open.start >= b.inner_start and o.close.end <= b.inner_end for o in blocks)
    ]
    found: List[Tuple[int, int, Tuple[str, str]]] = []
    for block in sorted(leaves, key=lambda b: b.open.start):
        tag_name = (
            block.open.suffix or _get_attr(block.open.attrs, "name") or _get_attr(block.open.attrs, "id") or ""
        )
        inner = _DSML_ANY_TAG_RE.sub("", text[block.inner_start : block.inner_end])
        call = _extract_call(tag_name, inner, requested, schema_map)
        if not call:
            continue
        if ctx.nonce:  # JSON-body blocks carrying a wrong _nonce are rejected (#9343)
            parsed = parse_loose_json_object(inner)
            if parsed is not None and isinstance(parsed.get("name"), str) and "_nonce" in parsed and parsed["_nonce"] != ctx.nonce:
                continue
        found.append((block.open.start, block.close.end, call))

    dsml_strip: List[Tuple[int, int]] = []
    if dsml is not None:
        dsml_strip = dsml[1]
        for d in dsml[0]:
            found.append((d.start, d.end, (d.name, to_arguments_string(d.args))))
    if not found:
        return text, None  # tags present but nothing parsed: never fall back to looser parsing

    found.sort(key=lambda f: f[0])
    tool_calls: List[Dict[str, Any]] = []
    accepted: List[Tuple[int, int]] = []
    for start, end, (name, arguments) in found:
        tool_calls.append({"id": f"{id_seed}_{len(tool_calls)}", "type": "function", "function": {"name": name, "arguments": arguments}})
        accepted.append((start, end))

    def within(tag: _Tag) -> bool:
        return any(tag.start >= s and tag.end <= e for s, e in accepted)

    ranges = accepted + list(dsml_strip) + [(t.start, t.end) for t in tokens if not within(t)]
    content = strip_ranges(text, ranges)
    if tool_calls:
        content = re.sub(r"<parameter\b[^>]*>[\s\S]*?(?:</parameter>|\Z)", "", content, flags=re.I)
        content = re.sub(r"</?(?:parameter|invoke|tool|calls|tool_calls|function_calls)\b[^>]*>", "", content, flags=re.I)
        content = _js_trim(re.sub(r"\n{3,}", "\n\n", content))
    return finalize_bound_calls(text, (content, tool_calls), ctx)


# ── JSON Schema validation (stands in for OmniRoute's Ajv 2020-12, strict: false) ──


class _SchemaError(Exception):
    pass


def _json_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _type_matches(type_name: Any, value: Any) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    raise _SchemaError(f"unknown type {type_name!r}")


def _js_pattern(pattern: str) -> Optional["re.Pattern[str]"]:
    try:
        return re.compile(re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", pattern))
    except (re.error, TypeError):
        return None  # a JavaScript-only construct: don't block the call over it


class _SchemaValidator:
    def __init__(self, root: Any):
        self.root = root

    def _resolve(self, ref: str) -> Any:
        if ref == "#":
            return self.root
        if not ref.startswith("#/"):
            return True  # remote refs can't be resolved offline: accept
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = urllib.parse.unquote(part).replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise _SchemaError(f"unresolvable $ref {ref}")
        return node

    def valid(self, schema: Any, value: Any, depth: int = 0) -> bool:
        if depth > 48:
            raise _SchemaError("schema nesting too deep")
        if schema is True:
            return True
        if schema is False:
            return False
        if not isinstance(schema, dict):
            raise _SchemaError("schema must be an object or boolean")
        if isinstance(schema.get("$ref"), str) and not self.valid(self._resolve(schema["$ref"]), value, depth + 1):
            return False
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_type_matches(t, value) for t in types) and not (schema.get("nullable") is True and value is None):
                return False
        if "enum" in schema and isinstance(schema["enum"], list):
            if not any(_json_equal(value, option) for option in schema["enum"]):
                return False
        if "const" in schema and not _json_equal(value, schema["const"]):
            return False
        if isinstance(value, dict) and not self._object_ok(schema, value, depth):
            return False
        if isinstance(value, list) and not self._array_ok(schema, value, depth):
            return False
        if isinstance(value, str) and not self._string_ok(schema, value):
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not self._number_ok(schema, value):
            return False
        for sub in schema.get("allOf") or [] if isinstance(schema.get("allOf"), list) else []:
            if not self.valid(sub, value, depth + 1):
                return False
        if isinstance(schema.get("anyOf"), list) and not any(self.valid(s, value, depth + 1) for s in schema["anyOf"]):
            return False
        if isinstance(schema.get("oneOf"), list) and sum(1 for s in schema["oneOf"] if self.valid(s, value, depth + 1)) != 1:
            return False
        if "not" in schema and self.valid(schema["not"], value, depth + 1):
            return False
        if "if" in schema:
            branch = "then" if self.valid(schema["if"], value, depth + 1) else "else"
            if branch in schema and not self.valid(schema[branch], value, depth + 1):
                return False
        return True

    def _object_ok(self, schema: Dict[str, Any], value: Dict[str, Any], depth: int) -> bool:
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        patterns = schema.get("patternProperties") if isinstance(schema.get("patternProperties"), dict) else {}
        required = schema.get("required")
        if isinstance(required, list) and any(isinstance(k, str) and k not in value for k in required):
            return False
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            return False
        if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
            return False
        compiled = [(p, _js_pattern(p), s) for p, s in patterns.items()]
        for key, item in value.items():
            matched = False
            if key in props:
                matched = True
                if not self.valid(props[key], item, depth + 1):
                    return False
            for _, regex, sub in compiled:
                if regex is not None and regex.search(key):
                    matched = True
                    if not self.valid(sub, item, depth + 1):
                        return False
            if not matched and "additionalProperties" in schema:
                if not self.valid(schema["additionalProperties"], item, depth + 1):
                    return False
            if "propertyNames" in schema and not self.valid(schema["propertyNames"], key, depth + 1):
                return False
        for key, needed in (schema.get("dependentRequired") or {}).items() if isinstance(schema.get("dependentRequired"), dict) else []:
            if key in value and isinstance(needed, list) and any(n not in value for n in needed):
                return False
        for key, sub in (schema.get("dependentSchemas") or {}).items() if isinstance(schema.get("dependentSchemas"), dict) else []:
            if key in value and not self.valid(sub, value, depth + 1):
                return False
        return True

    def _array_ok(self, schema: Dict[str, Any], value: List[Any], depth: int) -> bool:
        prefix = schema.get("prefixItems")
        items = schema.get("items", _MISSING)
        if isinstance(items, list):  # draft-07 tuple form
            prefix, items = items, schema.get("additionalItems", _MISSING)
        prefix = prefix if isinstance(prefix, list) else []
        for index, item in enumerate(value):
            if index < len(prefix):
                if not self.valid(prefix[index], item, depth + 1):
                    return False
            elif items is not _MISSING and not self.valid(items, item, depth + 1):
                return False
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return False
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return False
        if schema.get("uniqueItems") is True:
            for i in range(len(value)):
                if any(_json_equal(value[i], value[j]) for j in range(i + 1, len(value))):
                    return False
        if "contains" in schema:
            hits = sum(1 for item in value if self.valid(schema["contains"], item, depth + 1))
            low = schema.get("minContains", 1) if isinstance(schema.get("minContains"), int) else 1
            if hits < low:
                return False
            if isinstance(schema.get("maxContains"), int) and hits > schema["maxContains"]:
                return False
        return True

    @staticmethod
    def _string_ok(schema: Dict[str, Any], value: str) -> bool:
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return False
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return False
        if isinstance(schema.get("pattern"), str):
            regex = _js_pattern(schema["pattern"])
            if regex is not None and not regex.search(value):
                return False
        return True

    @staticmethod
    def _number_ok(schema: Dict[str, Any], value: float) -> bool:
        def num(key: str) -> Optional[float]:
            v = schema.get(key)
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        if num("minimum") is not None:
            if schema.get("exclusiveMinimum") is True and value <= num("minimum"):
                return False
            if value < num("minimum"):
                return False
        if num("maximum") is not None:
            if schema.get("exclusiveMaximum") is True and value >= num("maximum"):
                return False
            if value > num("maximum"):
                return False
        if num("exclusiveMinimum") is not None and value <= num("exclusiveMinimum"):
            return False
        if num("exclusiveMaximum") is not None and value >= num("exclusiveMaximum"):
            return False
        step = num("multipleOf")
        if step:
            quotient = value / step
            if abs(quotient - round(quotient)) > 1e-9:
                return False
        return True


def json_schema_valid(schema: Any, value: Any) -> bool:
    try:
        return _SchemaValidator(schema).valid(schema, value)
    except (_SchemaError, RecursionError, TypeError):
        return False  # Ajv fails to compile invalid schemas, which rejects the call


# ════════════════════════════════════════════════════════════════════════════
# One chat completion request
# ════════════════════════════════════════════════════════════════════════════

REPAIR_INSTRUCTIONS = [
    "Your previous response used an invalid tool envelope and was rejected.",
    "Retry the requested next action from scratch. Follow the client tool contract exactly, "
    "including its _nonce binding and argument schema. Do not emit bare JSON or unbound DSML.",
]
INCOMPLETE_STREAM_MESSAGE = (
    "DeepSeek web session ended before completion (no FINISHED signal received) - likely a "
    "dropped session or a network interruption upstream. Retry the request."
)


class ChatJob:
    """Runs one /v1/chat/completions request against chat.deepseek.com.

    run() yields ("start", None) once the upstream stream is open, then ("reasoning", text),
    ("content", text), ("tool_calls", calls), ("ping", None) and finally ("end", info).
    Errors before "start" are raised (so the server can still answer with an HTTP error);
    later ones arrive as ("end", {"error": ...})."""

    def __init__(self, app: "App", body: Any):
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BadRequest("'messages' must be a non-empty array")
        self.app = app
        self.client = app.client
        self.body = body
        model = body.get("model")
        self.model = model.strip() if isinstance(model, str) and model.strip() else "deepseek-web"
        self.messages = [m for m in messages if isinstance(m, dict)]
        self.stream = body.get("stream") is True
        options = body.get("stream_options")
        self.include_usage = isinstance(options, dict) and options.get("include_usage") is True
        tools = body.get("tools")
        self.tool_choice = body.get("tool_choice")
        has_tools = isinstance(tools, list) and len(tools) > 0 and self.tool_choice != "none"
        self.tool_ctx = ToolContext(tools) if has_tools else None
        self.model_type, self.thinking, self.search = resolve_model_options(self.model, body)
        if self.search:
            self.citation_mode = "strip" if "search-silent" in self.model.lower() else "number"
        else:
            self.citation_mode = "off"
        refs = body.get("ref_file_ids")
        self.extra_file_ids = [r for r in refs if isinstance(r, str) and r] if isinstance(refs, list) else []
        self.ref_file_ids: List[str] = []
        self.prompt = ""

    def build_prompt(self, has_images: bool = False) -> str:
        if self.tool_ctx is not None:
            tool_prompt = serialize_deepseek_tool_prompt(self.tool_ctx.tools, self.tool_ctx.nonce)
            prompt = build_tool_conversation_prompt(self.messages, tool_prompt)
            choice = self.tool_choice
            if choice == "required":
                prompt += "\n\nYou MUST call at least one of the available tools now."
            elif isinstance(choice, dict):
                fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
                if isinstance(fn.get("name"), str) and fn["name"]:
                    prompt += f'\n\nYou MUST call the tool "{fn["name"]}" now.'
        else:
            prompt = messages_to_prompt(self.messages, self.app.history_window)
        response_format = self.body.get("response_format")
        if isinstance(response_format, dict):
            if response_format.get("type") == "json_object":
                prompt += "\n\nRespond with a single valid JSON object and nothing else."
            elif response_format.get("type") == "json_schema":
                spec = response_format.get("json_schema")
                schema = spec.get("schema") if isinstance(spec, dict) else None
                prompt += (
                    "\n\nRespond with a single valid JSON value that matches this JSON Schema, and nothing else:\n"
                    + _js_stringify(schema if schema is not None else {})
                )
        if not prompt.strip() and not has_images:
            raise BadRequest("The messages contain no text to send")
        return prompt

    def _open(self, access: str, prompt: str) -> Tuple[str, Any]:
        sid = self.client.create_session(access)
        payload = {
            "chat_session_id": sid,
            "parent_message_id": None,
            "model_type": self.model_type,
            "prompt": prompt,
            "ref_file_ids": self.ref_file_ids,
            "thinking_enabled": self.thinking,
            "search_enabled": self.search,
            "action": None,
            "preempt": False,
        }
        try:
            resp = self.client.open_stream(access, COMPLETION_PATH, payload)
        except BaseException:
            self.client.delete_session_async(access, sid)
            raise
        return sid, resp

    def _upstream_events(self, access: str, sid: str, resp: Any, parser: DeepSeekStreamParser) -> Iterator[Tuple[str, str]]:
        """Parsed deltas from DeepSeek; follows up with /chat/continue when an answer is cut off."""
        continues = 0
        while True:
            parser.begin_stream()
            try:
                for raw in resp:
                    for item in parser.feed(raw.decode("utf-8", "replace")):
                        yield item
                    if parser.terminal:
                        break  # don't wait for trailing events; some streams stay open (#6777)
            except (OSError, http.client.HTTPException) as exc:
                raise UpstreamError(f"Lost the connection to DeepSeek mid-answer: {exc}", 502) from None
            finally:
                close_quietly(resp)
            if (
                parser.status == "INCOMPLETE"
                and self.app.auto_continue
                and continues < MAX_CONTINUES
                and parser.response_message_id is not None
            ):
                continues += 1
                debug(f"DeepSeek cut the answer off; continuing ({continues}/{MAX_CONTINUES})")
                resp = self.client.open_stream(
                    access,
                    CONTINUE_PATH,
                    {"chat_session_id": sid, "message_id": parser.response_message_id, "fallback_to_resume": True},
                )
                continue
            return

    def _end_info(self, parser: DeepSeekStreamParser, content: str, reasoning: str) -> Dict[str, Any]:
        status = parser.status
        if not status or status == "WIP":
            return {"error": INCOMPLETE_STREAM_MESSAGE, "code": "incomplete_stream"}
        if status == "CONTEXT_LENGTH_EXCEEDED" and not content.strip():
            return {
                "error": "The conversation is too long for DeepSeek web (context length exceeded).",
                "code": "context_length_exceeded",
            }
        if parser.model_type and parser.model_type != self.model_type:
            debug(f"DeepSeek answered with model_type={parser.model_type} (asked for {self.model_type})")
        completion = estimate_tokens(content + reasoning)
        total = parser.token_usage
        if isinstance(total, int) and total > 0:
            completion = min(completion, total)
            prompt_tokens = total - completion
        else:
            prompt_tokens = estimate_tokens(self.prompt)
            total = prompt_tokens + completion
        return {
            "finish_reason": FINISH_REASONS.get(status, "stop"),
            "status": status,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": total},
        }

    def _tool_round(self, access: str, sid: str, resp: Any, parser: DeepSeekStreamParser, cites: CitationFormatter):
        """Reasoning streams live; the answer is buffered because tool blocks need the full text."""
        content: List[str] = []
        reasoning: List[str] = []
        last_output = time.monotonic()
        for kind, text in self._upstream_events(access, sid, resp, parser):
            if kind == "reasoning":
                reasoning.append(text)
                last_output = time.monotonic()
                yield "reasoning", text
            else:
                content.append(cites.feed(text))
                if time.monotonic() - last_output > 10:
                    last_output = time.monotonic()
                    yield "ping", None
        content.append(cites.flush())
        content.append(cites.references(parser.search_results))
        return "".join(content), "".join(reasoning)

    def run(self) -> Iterator[Tuple[str, Any]]:
        client = self.client
        images = load_images(self.messages)  # local validation before any network call

        def start() -> Tuple[str, str, Any]:
            access = client.access_token()
            if self.model_type not in client.enabled_model_types(access):
                self.app.note_fallback(self.model_type)
                self.model_type = "default"
            uploaded = client.upload_images(access, images, self.model_type, self.thinking) if images else []
            self.ref_file_ids = list(dict.fromkeys(self.extra_file_ids + uploaded))
            self.prompt = self.build_prompt(bool(images))
            sid, resp = self._open(access, self.prompt)
            return access, sid, resp

        access, sid, resp = self.app.with_auth_retry(start)
        sessions = [sid]
        try:
            yield "start", None
            parser = DeepSeekStreamParser()
            cites = CitationFormatter(self.citation_mode)

            if self.tool_ctx is None:
                content: List[str] = []
                reasoning: List[str] = []
                for kind, text in self._upstream_events(access, sid, resp, parser):
                    if kind == "content":
                        text = cites.feed(text)
                        if not text:
                            continue
                        content.append(text)
                    else:
                        reasoning.append(text)
                    yield kind, text
                tail = cites.flush() + cites.references(parser.search_results)
                if tail:
                    content.append(tail)
                    yield "content", tail
                yield "end", self._end_info(parser, "".join(content), "".join(reasoning))
                return

            # Tool-calling turn (OmniRoute #2820): parse the buffered answer into tool_calls.
            ctx = self.tool_ctx
            text, reasoning_text = yield from self._tool_round(access, sid, resp, parser, cites)
            end = self._end_info(parser, text, reasoning_text)
            if end.get("error"):
                yield "end", end
                return
            content_text, calls = parse_deepseek_tool_calls(text, f"call-{int(time.time() * 1000)}", ctx)
            if has_malformed_deepseek_tool_intent(content_text, ctx):
                # Explicit tool intent in a broken envelope: retry once in a fresh session.
                log("DeepSeek emitted a malformed tool call; retrying once in a fresh session", "WARN")
                client.delete_session_async(access, sid)
                sessions.remove(sid)
                sid, resp = self._open(access, "\n\n".join([self.prompt] + REPAIR_INSTRUCTIONS))
                sessions.append(sid)
                parser = DeepSeekStreamParser()
                cites = CitationFormatter(self.citation_mode)
                if reasoning_text:
                    yield "reasoning", "\n\n"
                text, more_reasoning = yield from self._tool_round(access, sid, resp, parser, cites)
                reasoning_text += more_reasoning
                end = self._end_info(parser, text, reasoning_text)
                if end.get("error"):
                    yield "end", end
                    return
                content_text, calls = parse_deepseek_tool_calls(text, f"call-{int(time.time() * 1000)}", ctx)
                if has_malformed_deepseek_tool_markup(content_text):
                    yield "end", {"error": "DeepSeek emitted an invalid tool call after repair.", "code": "invalid_tool_call"}
                    return
            if content_text:
                yield "content", content_text
            if calls:
                yield "tool_calls", calls
                end["finish_reason"] = "tool_calls"
            yield "end", end
        except UpstreamError as exc:
            yield "end", {"error": str(exc), "code": exc.code or exc.error_type}
        finally:
            for session_id in sessions:
                client.delete_session_async(access, session_id)


# ════════════════════════════════════════════════════════════════════════════
# Application state shared by the server threads
# ════════════════════════════════════════════════════════════════════════════


class App:
    def __init__(
        self,
        store: CredentialStore,
        *,
        keep_sessions: bool = False,
        history_window: int = 0,
        auto_continue: bool = True,
        auto_refresh: bool = True,
        api_key: Optional[str] = None,
        cors: bool = False,
        check_host: bool = True,
        max_concurrency: int = 4,
    ):
        self.store = store
        self.client = DeepSeekClient(store, keep_sessions=keep_sessions)
        self.history_window = max(0, history_window)
        self.auto_continue = auto_continue
        self.auto_refresh = auto_refresh
        self.api_key = api_key or None
        self.cors = cors
        self.check_host = check_host
        self.slots = threading.BoundedSemaphore(max(1, max_concurrency))
        self._refresh_lock = threading.Lock()
        self._refresh_ok_at = -1e9
        self._refresh_failed_at = -1e9
        self._fallbacks_noted: set = set()

    def note_fallback(self, model_type: str) -> None:
        if model_type not in self._fallbacks_noted:
            self._fallbacks_noted.add(model_type)
            log(
                f"The '{model_type}' model type is not enabled for this DeepSeek account; "
                "using 'default' (Instant) instead.",
                "WARN",
            )

    def with_auth_retry(self, fn: Callable[[], Any]) -> Any:
        """On a rejected token, look for a newer one in the browser profile it came from."""
        try:
            return fn()
        except AuthError:
            if not self.auto_refresh or not self.refresh_credentials():
                raise
            log("Retrying with the refreshed login")
            return fn()

    def refresh_credentials(self) -> bool:
        with self._refresh_lock:
            now = time.monotonic()
            if now - self._refresh_ok_at < 30:
                return True  # another request refreshed a moment ago
            if now - self._refresh_failed_at < 30:
                return False
            try:
                ok = refresh_from_browser_profile(self.store)
            except Exception as exc:  # noqa: BLE001 - never let a refresh attempt crash a request
                debug(f"Login refresh failed: {exc!r}")
                ok = False
            if ok:
                self._refresh_ok_at = time.monotonic()
                self.client.invalidate()
            else:
                self._refresh_failed_at = time.monotonic()
            return ok


# ════════════════════════════════════════════════════════════════════════════
# HTTP server (OpenAI-compatible)
# ════════════════════════════════════════════════════════════════════════════

_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _wire_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")


def model_card(model_id: str) -> Dict[str, Any]:
    return {"id": model_id, "object": "model", "created": 1767225600, "owned_by": "deepseek-web"}


def _is_local_host_header(host: str) -> bool:
    host = host.strip().lower()
    if host.startswith("["):
        name = host[1 : host.find("]")] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in ("", "127.0.0.1", "localhost", "::1") or name.endswith(".localhost")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second server silently share the port.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: Tuple[str, int], handler: Any, app: App):
        self.app = app
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind() does a reverse DNS lookup (getfqdn) that can stall for
        # seconds on Windows; nothing here needs it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients dropping idle keep-alive connections is routine (WinError 10054 on Windows);
        # the stdlib default prints a full traceback for it.
        exc = sys.exc_info()[1]
        if isinstance(exc, _DISCONNECTS + (socket.timeout, TimeoutError)):
            debug(f"Client {client_address[0]} disconnected: {exc}")
            return
        log(f"Error while handling a request from {client_address[0]}: {exc!r}", "ERROR")
        debug(traceback.format_exc())


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"DeepSeekWebAPI/{__version__}"
    timeout = 600
    disable_nagle_algorithm = True

    def log_message(self, fmt: str, *args: Any) -> None:  # route http.server's noise to --verbose
        debug(f"{self.address_string()} {fmt % args}")

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ── plumbing ────────────────────────────────────────────────────────────

    def _cors(self) -> None:
        if self.app.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Type")

    def _send_json(self, status: int, obj: Any, close: bool = False) -> None:
        data = _wire_json(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str, error_type: str, code: Any = None, close: bool = False) -> None:
        body = {"error": {"message": message, "type": error_type, "code": code if code is not None else error_type}}
        self._send_json(status, body, close=close)

    def _send_exception(self, exc: BaseException) -> int:
        if isinstance(exc, BadRequest):
            status, error_type, message, code = exc.status, "invalid_request_error", str(exc), None
        elif isinstance(exc, UpstreamError):
            status, error_type, message, code = exc.status, exc.error_type, str(exc), exc.code
        elif isinstance(exc, (socket.timeout, TimeoutError)):
            status, error_type, message, code = 504, "timeout", "Timed out waiting for DeepSeek", None
        else:
            log(f"Unexpected error: {exc!r}", "ERROR")
            debug(traceback.format_exc())
            status, error_type, code = 500, "internal_error", None
            message = "Internal error in deepseek_web_api.py (run with --verbose for details)"
        if status >= 400 and not isinstance(exc, BadRequest):
            log(message, "WARN")
        self._send_error(status, message, error_type, code)
        return status

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _sse(self, obj: Any) -> None:
        self._chunk(b"data: " + _wire_json(obj) + b"\n\n")

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _guard(self) -> bool:
        app = self.app
        if app.check_host and not _is_local_host_header(self.headers.get("Host") or ""):
            self._send_error(
                403,
                "Use http://127.0.0.1 or http://localhost to reach this server (DNS-rebinding protection)",
                "forbidden",
                close=True,
            )
            return False
        if app.api_key:
            auth = self.headers.get("Authorization") or ""
            supplied = auth[7:].strip() if auth[:7].lower() == "bearer " else (self.headers.get("x-api-key") or "").strip()
            if not hmac.compare_digest(supplied.encode("utf-8"), app.api_key.encode("utf-8")):
                self._send_error(401, "Invalid or missing API key", "authentication_error", close=True)
                return False
        return True

    def _route(self) -> str:
        path = urllib.parse.urlsplit(self.path).path
        return path.rstrip("/") or "/"

    def _read_json(self) -> Dict[str, Any]:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            raw = self._read_chunked()
        else:
            length = self.headers.get("Content-Length")
            if length is None:
                self.close_connection = True
                raise BadRequest("Content-Length header required", 411)
            try:
                size = int(length)
            except ValueError:
                self.close_connection = True
                raise BadRequest("Invalid Content-Length header") from None
            if size < 0 or size > MAX_REQUEST_BYTES:
                self.close_connection = True
                raise BadRequest("Request body too large", 413)
            raw = self.rfile.read(size)
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("Request body must be valid JSON") from None
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object")
        return body

    def _read_chunked(self) -> bytes:
        parts: List[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                self.close_connection = True
                raise BadRequest("Malformed chunked request body") from None
            if size == 0:
                while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(parts)
            total += size
            if total > MAX_REQUEST_BYTES:
                self.close_connection = True
                raise BadRequest("Request body too large", 413)
            parts.append(self.rfile.read(size))
            self.rfile.readline(4)

    # ── routes ──────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
        if not self.app.cors:
            self._send_error(405, "CORS is disabled; start the server with --cors", "method_not_allowed", close=True)
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = self._route()
        if path in ("/", "/health", "/v1", "/v1/health"):
            logged_in = bool(extract_user_token(self.app.store.get().get("token")))
            self._send_json(200, {"status": "ok", "service": "deepseek-web-api", "version": __version__, "logged_in": logged_in})
        elif path in ("/v1/models", "/models"):
            self._send_json(200, {"object": "list", "data": [model_card(m) for m in MODEL_IDS]})
        elif path.startswith(("/v1/models/", "/models/")):
            self._send_json(200, model_card(urllib.parse.unquote(path.split("/models/", 1)[1])))
        else:
            self._send_error(404, f"Unknown endpoint: {path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = self._route()
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error(404, f"Unknown endpoint: {path}", "not_found", close=True)
            return
        started = time.monotonic()
        try:
            job = ChatJob(self.app, self._read_json())
        except BadRequest as exc:
            self._send_error(exc.status, str(exc), "invalid_request_error")
            return
        if not self.app.slots.acquire(timeout=300):
            self._send_error(503, "Too many concurrent requests; try again shortly", "overloaded")
            return
        status = 200
        try:
            status = self._stream(job) if job.stream else self._complete(job)
        except _DISCONNECTS:
            self.close_connection = True
            status = 499
        finally:
            self.app.slots.release()
            log(f"POST {path} model={job.model} stream={job.stream} -> {status} ({time.monotonic() - started:.1f}s)")

    def _complete(self, job: ChatJob) -> int:
        content: List[str] = []
        reasoning: List[str] = []
        calls = None
        end: Dict[str, Any] = {}
        try:
            for kind, value in job.run():
                if kind == "content":
                    content.append(value)
                elif kind == "reasoning":
                    reasoning.append(value)
                elif kind == "tool_calls":
                    calls = value
                elif kind == "end":
                    end = value
        except _DISCONNECTS:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to an HTTP error
            return self._send_exception(exc)
        if end.get("error"):
            log(end["error"], "WARN")
            self._send_error(502, end["error"], "upstream_error", end.get("code"))
            return 502
        message: Dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = calls
            if not message["content"]:
                message["content"] = None
        self._send_json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": job.model,
                "choices": [{"index": 0, "message": message, "finish_reason": end.get("finish_reason", "stop")}],
                "usage": end.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )
        return 200

    def _stream(self, job: ChatJob) -> int:
        events = job.run()
        try:
            try:
                next(events)  # ("start", None): setup errors still get a real HTTP status
            except StopIteration:
                self._send_error(502, "DeepSeek returned nothing", "upstream_error")
                return 502
            except _DISCONNECTS:
                raise
            except Exception as exc:  # noqa: BLE001
                return self._send_exception(exc)
            self._start_sse()
            cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())

            def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> None:
                self._sse(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": job.model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                )

            chunk({"role": "assistant", "content": ""})
            status = 200
            try:
                for kind, value in events:
                    if kind == "reasoning":
                        chunk({"reasoning_content": value})
                    elif kind == "content":
                        chunk({"content": value})
                    elif kind == "tool_calls":
                        chunk(
                            {
                                "tool_calls": [
                                    {"index": i, "id": c["id"], "type": "function", "function": c["function"]}
                                    for i, c in enumerate(value)
                                ]
                            }
                        )
                    elif kind == "ping":
                        self._chunk(b": keep-alive\n\n")
                    elif kind == "end":
                        if value.get("error"):
                            status = 502
                            log(value["error"], "WARN")
                            code = value.get("code") or "upstream_error"
                            self._sse({"error": {"message": value["error"], "type": "upstream_error", "code": code}})
                        else:
                            chunk({}, value.get("finish_reason", "stop"))
                            if job.include_usage:
                                self._sse(
                                    {
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": job.model,
                                        "choices": [],
                                        "usage": value.get("usage"),
                                    }
                                )
            except _DISCONNECTS:
                raise
            except Exception as exc:  # noqa: BLE001 - after the headers, errors go in-stream
                status = getattr(exc, "status", 500)
                if isinstance(exc, (UpstreamError, BadRequest)):
                    message = str(exc)
                else:
                    log(f"Unexpected error: {exc!r}", "ERROR")
                    debug(traceback.format_exc())
                    message = "Internal error in deepseek_web_api.py (run with --verbose for details)"
                self._sse({"error": {"message": message, "type": "upstream_error", "code": "upstream_error"}})
            self._chunk(b"data: [DONE]\n\n")
            self._end_chunks()
            return status
        finally:
            events.close()


# ════════════════════════════════════════════════════════════════════════════
# Login: read the token from a browser profile on disk
#
# Nothing here remote-controls a browser. DeepSeek's sign-in captcha and "Sign in
# with Google" reject browsers that are driven through DevTools, so instead:
#   1. a login you already have in Chrome / Edge / Brave / ... is reused by reading
#      that profile's chat.deepseek.com localStorage files, or
#   2. a normal browser window opens with a dedicated profile, and the script watches
#      that profile's files until the userToken appears after you sign in.
# Chromium keeps localStorage in SQLite (<profile>/LocalStorage, newer profiles) or in
# LevelDB (<profile>/Local Storage/leveldb); both are read with the standard library.
# ════════════════════════════════════════════════════════════════════════════

LS_TOKEN_KEY = "userToken"
LS_DEVICE_KEY = "deepseek-device-id:chat"
LOGIN_PATH = "/sign_in"


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def snappy_decompress(data: bytes) -> bytes:
    """Raw Snappy, as used for LevelDB table blocks."""
    size, pos = _read_varint(data, 0)
    out = bytearray()
    end = len(data)
    while pos < end:
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                length = int.from_bytes(data[pos : pos + extra], "little")
                pos += extra
            length += 1
            if pos + length > end:
                raise ValueError("truncated snappy literal")
            out += data[pos : pos + length]
            pos += length
            continue
        if kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 2], "little")
            pos += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        if not 0 < offset <= len(out):
            raise ValueError("bad snappy copy offset")
        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:  # overlapping copy repeats the last `offset` bytes
            for i in range(length):
                out.append(out[start + i])
    if len(out) != size:
        raise ValueError("snappy length mismatch")
    return bytes(out)


def _leveldb_batch_entries(record: bytes) -> Iterator[Tuple[int, bytes, Optional[bytes]]]:
    sequence = int.from_bytes(record[:8], "little")
    count = int.from_bytes(record[8:12], "little")
    pos = 12
    for i in range(count):
        tag = record[pos]
        pos += 1
        key_len, pos = _read_varint(record, pos)
        key = record[pos : pos + key_len]
        pos += key_len
        if tag == 1:
            value_len, pos = _read_varint(record, pos)
            yield sequence + i, key, record[pos : pos + value_len]
            pos += value_len
        elif tag == 0:
            yield sequence + i, key, None
        else:
            return


def _leveldb_log_entries(data: bytes) -> Iterator[Tuple[int, bytes, Optional[bytes]]]:
    """(sequence, key, value or None for a delete) from a LevelDB write-ahead log."""
    block_size = 32768
    pos = 0
    pending: Optional[bytearray] = None
    while pos + 7 <= len(data):
        left = block_size - pos % block_size
        if left < 7:
            pos += left
            continue
        length = data[pos + 4] | (data[pos + 5] << 8)
        kind = data[pos + 6]
        start, end = pos + 7, pos + 7 + length
        if end > len(data):
            return  # torn write at the end of a log that is still being written
        if kind == 0 or length > left - 7:
            pos += left  # zero padding / corruption: continue at the next block
            continue
        fragment = data[start:end]
        pos = end
        if kind == 1:
            yield from _leveldb_batch_entries(fragment)
        elif kind == 2:
            pending = bytearray(fragment)
        elif kind == 3 and pending is not None:
            pending += fragment
        elif kind == 4 and pending is not None:
            pending += fragment
            record, pending = bytes(pending), None
            yield from _leveldb_batch_entries(record)


_LEVELDB_TABLE_MAGIC = (0xDB4775248B80FB57).to_bytes(8, "little")


def _leveldb_block(data: bytes, handle: bytes) -> bytes:
    offset, pos = _read_varint(handle, 0)
    size, _ = _read_varint(handle, pos)
    raw = data[offset : offset + size]
    compression = data[offset + size]
    if compression == 1:
        return snappy_decompress(raw)
    if compression == 0:
        return raw
    raise ValueError("unsupported LevelDB block compression")


def _leveldb_block_entries(block: bytes) -> Iterator[Tuple[bytes, bytes]]:
    restarts = int.from_bytes(block[-4:], "little")
    limit = len(block) - 4 - 4 * restarts
    pos = 0
    key = b""
    while pos < limit:
        shared, pos = _read_varint(block, pos)
        unshared, pos = _read_varint(block, pos)
        value_len, pos = _read_varint(block, pos)
        key = key[:shared] + block[pos : pos + unshared]
        pos += unshared
        yield key, block[pos : pos + value_len]
        pos += value_len


def _leveldb_table_entries(data: bytes, prefix: bytes) -> Iterator[Tuple[int, bytes, Optional[bytes]]]:
    """Entries of a LevelDB table (.ldb) whose user key may start with `prefix`."""
    if len(data) < 48 or data[-8:] != _LEVELDB_TABLE_MAGIC:
        return
    footer = data[-48:]
    _, pos = _read_varint(footer, 0)  # skip the metaindex handle
    _, pos = _read_varint(footer, pos)
    index = _leveldb_block(data, footer[pos:])
    upper_bound = prefix + b"0"  # wanted keys continue with "\x00" or "/", both below "0"
    previous = b""
    for separator, handle in _leveldb_block_entries(index):
        block_max = separator[:-8]  # index keys are internal keys: user key + 8-byte tag
        if block_max >= prefix and previous < upper_bound:
            for internal_key, value in _leveldb_block_entries(_leveldb_block(data, handle)):
                if len(internal_key) < 8:
                    continue
                tag = int.from_bytes(internal_key[-8:], "little")
                yield tag >> 8, internal_key[:-8], value if tag & 0xFF == 1 else None
        previous = block_max


def _storage_key_names(names: Tuple[str, ...]) -> Dict[bytes, str]:
    """localStorage keys are stored with an encoding byte: 1 = Latin-1, 0 = UTF-16LE."""
    wanted: Dict[bytes, str] = {}
    for name in names:
        wanted[b"\x01" + name.encode("latin-1")] = name
        wanted[b"\x00" + name.encode("utf-16-le")] = name
    return wanted


def _decode_storage_value(raw: Optional[bytes], compression: int = 0) -> Optional[str]:
    if raw is None:
        return None
    if compression == 1:  # zstd, used by the SQLite backend for larger values
        try:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+

            raw = zstd.decompress(raw)
        except Exception:  # noqa: BLE001 - tokens are small and stored uncompressed
            return None
    elif compression:
        return None
    if not raw:
        return ""
    try:
        if raw[0] == 1:
            return raw[1:].decode("latin-1")
        if raw[0] == 0:
            return raw[1:].decode("utf-16-le")
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_leveldb_storage(directory: Path, origin: str, names: Tuple[str, ...]) -> Dict[str, str]:
    prefix = b"_" + origin.encode("utf-8")
    wanted = _storage_key_names(names)
    newest: Dict[str, Tuple[int, Optional[bytes]]] = {}
    paths = sorted(directory.glob("*.ldb")) + sorted(directory.glob("*.sst")) + sorted(directory.glob("*.log"))
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        entries = _leveldb_log_entries(data) if path.suffix == ".log" else _leveldb_table_entries(data, prefix)
        try:
            for sequence, key, value in entries:
                if not key.startswith(prefix):
                    continue
                rest = key[len(prefix) :]
                if rest[:1] == b"/":
                    rest = rest[1:]
                name = wanted.get(rest[1:]) if rest[:1] == b"\x00" else None
                if name and (name not in newest or sequence >= newest[name][0]):
                    newest[name] = (sequence, value)
        except (ValueError, IndexError):
            continue  # torn or unexpected data: keep what was read so far
    out: Dict[str, str] = {}
    for name, (_, value) in newest.items():
        text = _decode_storage_value(value)
        if text is not None:
            out[name] = text
    return out


def _read_sqlite_storage(path: Path, origin: str, names: Tuple[str, ...]) -> Dict[str, str]:
    import sqlite3

    # Work on a copy: the browser keeps the database open (and locked) while it runs.
    tmp = Path(tempfile.mkdtemp(prefix="dswebapi-"))
    out: Dict[str, str] = {}
    try:
        for suffix in ("", "-wal", "-journal"):
            source = Path(str(path) + suffix)
            if source.is_file():
                shutil.copyfile(str(source), str(tmp / ("LocalStorage" + suffix)))
        con = sqlite3.connect(str(tmp / "LocalStorage"))
        try:
            keys = [(origin + "/").encode("utf-8"), origin.encode("utf-8")]
            row = con.execute("SELECT row_id FROM maps WHERE storage_key IN (?, ?)", keys).fetchone()
            if row:
                for encoded, name in _storage_key_names(names).items():
                    if name in out:
                        continue
                    found = con.execute(
                        "SELECT value_compression_type, value FROM map_entries WHERE map_id = ? AND key = ?",
                        (row[0], encoded),
                    ).fetchone()
                    if found:
                        text = _decode_storage_value(bytes(found[1]), int(found[0] or 0))
                        if text is not None:
                            out[name] = text
        finally:
            con.close()
    except (OSError, sqlite3.Error) as exc:
        debug(f"Could not read {path}: {exc}")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    return out


def read_profile_storage(profile: Path, origin: Optional[str] = None) -> Dict[str, str]:
    """chat.deepseek.com's userToken and device id from one Chromium profile folder."""
    origin = origin or DEEPSEEK_BASE
    names = (LS_TOKEN_KEY, LS_DEVICE_KEY)
    values: Dict[str, str] = {}
    if (profile / "LocalStorage").is_file():
        values = _read_sqlite_storage(profile / "LocalStorage", origin, names)
    leveldb = profile / "Local Storage" / "leveldb"
    if leveldb.is_dir() and LS_TOKEN_KEY not in values:  # older profiles; SQLite wins when both exist
        for name, value in _read_leveldb_storage(leveldb, origin, names).items():
            values.setdefault(name, value)
    return values


def _plain_value(raw: Any) -> Optional[str]:
    """localStorage values are either plain strings or {"value": ...} JSON wrappers."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        value = parsed.get("value") if isinstance(parsed, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None
    return text.strip('"') or None


def client_locale(preference: Optional[str], languages: List[Any]) -> Optional[str]:
    """x-client-locale: the app's language setting, or (when it is "system") the browser's,
    e.g. "tr" for tr-TR, "en_US" for English, "zh_CN" for Chinese."""
    if preference and re.fullmatch(r"[a-z]{2,3}(?:[_-][A-Za-z]{2,4})?", preference):
        return preference
    first = next((lang for lang in languages if isinstance(lang, str) and lang.strip()), "")
    primary = first.strip().split("-")[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", primary):
        return None
    return {"en": "en_US", "zh": "zh_CN"}.get(primary, primary)


def accept_language_header(languages: List[Any]) -> str:
    langs = [lang.strip() for lang in languages if isinstance(lang, str) and lang.strip()][:8]
    parts = [lang if i == 0 else f"{lang};q={max(0.1, 1 - i / 10):.1f}" for i, lang in enumerate(langs)]
    return ",".join(parts) or "en-US,en;q=0.9"


def _profile_languages(profile: Path) -> Dict[str, str]:
    """Accept-Language / x-client-locale from the profile's language settings."""
    try:
        prefs = json.loads((profile / "Preferences").read_text(encoding="utf-8"))
        languages = str(prefs["intl"]["accept_languages"]).split(",")
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {"accept_language": accept_language_header(languages)}
    locale = client_locale(None, languages)
    if locale:
        out["locale"] = locale
    return out


def _user_agent_for(browser: str, user_data_dir: Path) -> str:
    """A user agent matching the browser that holds the login (version from "Last Version")."""
    try:
        major = (user_data_dir / "Last Version").read_text(encoding="utf-8").strip().split(".")[0]
    except OSError:
        major = ""
    if not major.isdigit() or not browser.startswith(("Google Chrome", "Microsoft Edge", "Brave", "Chromium")):
        return DEFAULT_USER_AGENT
    if os.name == "nt":
        platform = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform = "X11; Linux x86_64"
    agent = f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    return agent + (f" Edg/{major}.0.0.0" if browser.startswith("Microsoft Edge") else "")


def browser_user_data_dirs() -> List[Tuple[str, Path]]:
    """(browser name, user data folder) for installed Chromium-based browsers."""
    home = Path.home()
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        candidates = [
            ("Google Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Google Chrome Beta", local / "Google" / "Chrome Beta" / "User Data"),
            ("Google Chrome Canary", local / "Google" / "Chrome SxS" / "User Data"),
            ("Microsoft Edge", local / "Microsoft" / "Edge" / "User Data"),
            ("Microsoft Edge Beta", local / "Microsoft" / "Edge Beta" / "User Data"),
            ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Chromium", local / "Chromium" / "User Data"),
            ("Vivaldi", local / "Vivaldi" / "User Data"),
            ("Opera", roaming / "Opera Software" / "Opera Stable"),
            ("Opera GX", roaming / "Opera Software" / "Opera GX Stable"),
        ]
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        candidates = [
            ("Google Chrome", base / "Google" / "Chrome"),
            ("Microsoft Edge", base / "Microsoft Edge"),
            ("Brave", base / "BraveSoftware" / "Brave-Browser"),
            ("Chromium", base / "Chromium"),
            ("Vivaldi", base / "Vivaldi"),
            ("Opera", base / "com.operasoftware.Opera"),
        ]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        candidates = [
            ("Google Chrome", config / "google-chrome"),
            ("Microsoft Edge", config / "microsoft-edge"),
            ("Brave", config / "BraveSoftware" / "Brave-Browser"),
            ("Chromium", config / "chromium"),
            ("Vivaldi", config / "vivaldi"),
            ("Opera", config / "opera"),
        ]
    return [(name, path) for name, path in candidates if path.is_dir()]


def browser_profiles(user_data_dir: Path) -> List[Path]:
    def has_storage(folder: Path) -> bool:
        return (folder / "LocalStorage").is_file() or (folder / "Local Storage").is_dir()

    profiles = [user_data_dir] if has_storage(user_data_dir) else []  # Opera keeps one at the root
    try:
        children = sorted(user_data_dir.iterdir())
    except OSError:
        return profiles
    for child in children:
        if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ")) and has_storage(child):
            profiles.append(child)
    return profiles


class _MemoryStore:
    """Stand-in for CredentialStore when checking a token before saving it."""

    env_token = None

    def __init__(self, creds: Dict[str, Any]):
        self._creds = creds

    def get(self) -> Dict[str, Any]:
        return dict(self._creds)


def probe_token(token: str, creds: Optional[Dict[str, Any]] = None) -> Optional[bool]:
    """True if DeepSeek accepts the token, False if it rejects it, None if it couldn't be checked."""
    client = DeepSeekClient(_MemoryStore({**(creds or {}), "token": token}))  # type: ignore[arg-type]
    try:
        client.access_token(force=True)
        return True
    except AuthError:
        return False
    except UpstreamError as exc:
        debug(f"Could not check a token: {exc}")
        return None


def find_browser_logins() -> List[Dict[str, Any]]:
    """DeepSeek logins found in the localStorage of installed browsers' profiles."""
    found: List[Dict[str, Any]] = []
    seen = set()
    for browser, user_data_dir in browser_user_data_dirs():
        for profile in browser_profiles(user_data_dir):
            try:
                values = read_profile_storage(profile)
            except Exception as exc:  # noqa: BLE001 - one unreadable profile shouldn't stop the search
                debug(f"Could not read {profile}: {exc!r}")
                continue
            token = extract_user_token(values.get(LS_TOKEN_KEY))
            if not token or token in seen:
                continue
            seen.add(token)
            creds: Dict[str, Any] = {
                "token": token,
                "device_id": _plain_value(values.get(LS_DEVICE_KEY)) or str(uuid.uuid4()),
                "user_agent": _user_agent_for(browser, user_data_dir),
                "source": {"type": "browser", "browser": browser, "profile": str(profile)},
                "saved_at": int(time.time()),
            }
            creds.update(_profile_languages(profile))
            found.append(creds)
    return found


def import_browser_login() -> Optional[Dict[str, Any]]:
    for creds in find_browser_logins():
        source = creds["source"]
        label = f"{source['browser']} (profile \"{Path(source['profile']).name}\")"
        verdict = probe_token(creds["token"], creds)
        if verdict:
            say(f"Found a working DeepSeek login in {label}.")
            return creds
        if verdict is False:
            say(f"Skipping the expired DeepSeek login in {label}.")
        else:
            say(f"Could not check the DeepSeek login in {label} (network problem); skipping it.")
    return None


def _windows_app_path(exe_name: str) -> Optional[str]:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}") as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        value = str(value).strip().strip('"')
        if value and os.path.isfile(value):
            return value
    return None


def browser_candidates() -> List[Tuple[str, str]]:
    """Installed Chromium-based browsers as (kind, executable). Real Chrome / Edge / Brave come
    first: captchas trust them more than plain Chromium builds."""
    found: List[Tuple[str, str]] = []

    def add(kind: str, path: Optional[str]) -> None:
        if path and os.path.isfile(path) and all(os.path.normcase(p) != os.path.normcase(path) for _, p in found):
            found.append((kind, path))

    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
        program_files_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        local = os.environ.get("LOCALAPPDATA") or ""
        bases = [b for b in (local, program_files, program_files_x86) if b]
        add("chrome", _windows_app_path("chrome.exe"))
        for base in bases:
            add("chrome", os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        add("edge", _windows_app_path("msedge.exe"))
        for base in bases:
            add("edge", os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for base in bases:
            add("brave", os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"))
        for base in bases:
            add("chromium", os.path.join(base, "Chromium", "Application", "chrome.exe"))
        playwright_root = os.path.join(local, "ms-playwright") if local else ""
        playwright_globs = ["chromium-*/*/chrome.exe"]
    elif sys.platform == "darwin":
        apps = (
            ("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("chromium", "Chromium.app/Contents/MacOS/Chromium"),
        )
        for kind, rel in apps:
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                add(kind, os.path.join(root, rel))
        playwright_root = os.path.expanduser("~/Library/Caches/ms-playwright")
        playwright_globs = ["chromium-*/*/*.app/Contents/MacOS/*"]
    else:
        names = (
            ("chrome", ("google-chrome", "google-chrome-stable")),
            ("edge", ("microsoft-edge", "microsoft-edge-stable")),
            ("brave", ("brave-browser", "brave")),
            ("chromium", ("chromium", "chromium-browser")),
        )
        for kind, executables in names:
            for name in executables:
                add(kind, shutil.which(name))
        playwright_root = os.path.expanduser("~/.cache/ms-playwright")
        playwright_globs = ["chromium-*/*/chrome"]
    # Chromium installed by Playwright (pip install playwright; python -m playwright install chromium)
    playwright_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or playwright_root
    if playwright_root:
        for pattern in playwright_globs:
            for path in sorted(glob.glob(os.path.join(playwright_root, pattern)), reverse=True):
                add("chromium", path)
    return found


def pick_browser(choice: Optional[str]) -> Optional[Tuple[str, str]]:
    if choice:
        if os.path.isfile(choice):
            return ("custom", choice)
        wanted = {"google-chrome": "chrome", "msedge": "edge", "microsoft-edge": "edge"}.get(choice.lower(), choice.lower())
        for kind, path in browser_candidates():
            if kind == wanted:
                return kind, path
        return None
    candidates = browser_candidates()
    return candidates[0] if candidates else None


def _profile_in_use(profile: Path) -> bool:
    if os.name == "nt":
        lock = profile / "lockfile"  # held open (no delete sharing) while the browser runs
        if not lock.exists():
            return False
        try:
            lock.unlink()
            return False
        except OSError:
            return True
    lock = profile / "SingletonLock"  # symlink to "<host>-<pid>"
    try:
        target = os.readlink(str(lock))
    except OSError:
        return False
    pid = target.rsplit("-", 1)[-1]
    if not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def launch_browser(
    exe: str, profile: Path, url: str, headless: bool = False, extra_args: Tuple[str, ...] = ()
) -> subprocess.Popen:
    """Start the browser like a desktop shortcut would: no DevTools port, no automation flags."""
    args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", *extra_args]
    if headless:  # only used by the tests
        args.append("--headless=new")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # Chromium refuses to run as root otherwise (Linux containers)
    args.append(url)
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise LoginError(f"Could not start the browser ({exe}): {exc}") from None


def close_browser(proc: subprocess.Popen, timeout: float = 10.0, force: bool = False) -> bool:
    """Ask the browser to quit the way closing its window would, so it saves its profile."""
    if proc.poll() is not None:
        return True
    try:
        if os.name == "nt":  # taskkill without /F sends the windows a normal close request
            subprocess.run(["taskkill", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            proc.terminate()  # SIGTERM: Chromium shuts down cleanly
        proc.wait(timeout=timeout)
        return True
    except (OSError, subprocess.SubprocessError):
        if force:
            proc.kill()
            return True
        return proc.poll() is not None


def window_login(
    exe: str,
    profile: Path,
    timeout: float = 300.0,
    *,
    headless: bool = False,
    extra_args: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Open DeepSeek's sign-in page in a normal browser window and wait for the token
    to land in the profile's localStorage files."""
    profile.mkdir(parents=True, exist_ok=True)
    storage = profile / "Default"
    checked: set = set()

    def captured() -> Optional[Dict[str, Any]]:
        values = read_profile_storage(storage)
        token = extract_user_token(values.get(LS_TOKEN_KEY))
        if not token or token in checked:
            return None
        verdict = probe_token(token)
        if verdict is not None:
            checked.add(token)
        if not verdict:
            return None
        creds: Dict[str, Any] = {
            "token": token,
            "device_id": _plain_value(values.get(LS_DEVICE_KEY)) or str(uuid.uuid4()),
            "user_agent": _user_agent_for(_browser_name(exe), profile),
            "source": {"type": "window", "browser": exe, "profile": str(storage)},
            "saved_at": int(time.time()),
        }
        creds.update(_profile_languages(storage))
        return creds

    already_open = _profile_in_use(profile)
    proc = launch_browser(exe, profile, DEEPSEEK_BASE + LOGIN_PATH, headless=headless, extra_args=extra_args)
    deadline = time.monotonic() + timeout
    creds = None
    try:
        while True:
            time.sleep(2)
            creds = captured()
            if creds:
                return creds
            if proc.poll() is not None and not _profile_in_use(profile):
                creds = captured()  # the browser writes its storage out when it closes
                if creds:
                    return creds
                raise LoginError("The browser window was closed before the sign-in finished.")
            if time.monotonic() > deadline:
                raise LoginError("Timed out waiting for the DeepSeek sign-in.")
    finally:
        closed = close_browser(proc, force=headless)
        if creds and (already_open or not closed):
            say("Signed in. You can close the browser window now.")


def _browser_name(exe: str) -> str:
    lowered = os.path.basename(exe).lower()
    if "msedge" in lowered or "microsoft edge" in lowered:
        return "Microsoft Edge"
    if "brave" in lowered:
        return "Brave"
    if "chromium" in lowered or "ms-playwright" in exe.lower():
        return "Chromium"
    return "Google Chrome" if "chrome" in lowered else "Chromium"


def refresh_from_browser_profile(store: CredentialStore) -> bool:
    """After DeepSeek rejects the saved token, pick up a newer one from the profile it came from."""
    if store.env_token:
        return False
    creds = store.get()
    source = creds.get("source") if isinstance(creds.get("source"), dict) else {}
    profile = Path(source["profile"]) if isinstance(source.get("profile"), str) else browser_profile_dir() / "Default"
    try:
        values = read_profile_storage(profile)
    except Exception as exc:  # noqa: BLE001
        debug(f"Could not read {profile}: {exc!r}")
        return False
    token = extract_user_token(values.get(LS_TOKEN_KEY))
    if not token or token == extract_user_token(creds.get("token")) or not probe_token(token, creds):
        return False
    store.update(token=token, saved_at=int(time.time()))
    log("Picked up a newer DeepSeek login from the browser profile.")
    return True


# ════════════════════════════════════════════════════════════════════════════
# Command line
# ════════════════════════════════════════════════════════════════════════════


def _finish_login(store: CredentialStore, creds: Dict[str, Any]) -> int:
    previous = store._file_creds()
    store.save(creds)
    client = DeepSeekClient(store)
    try:
        access = client.access_token(force=True)
        types = client.enabled_model_types(access)
    except AuthError as exc:
        if previous:  # don't replace a working login with a rejected token
            store.save(previous)
        else:
            store.delete()
        say(f"DeepSeek rejected that token, so it was not saved: {exc}")
        return 1
    except UpstreamError as exc:
        say(f"The token was saved, but checking it with DeepSeek failed: {exc}")
        return 0
    email = client.account.get("email")
    if email:
        store.update(email=email)
    say(f"Logged in{' as ' + email if email else ''}. Saved to {store.path}")
    say(f"Model types enabled for this account: {', '.join(sorted(types))}")
    return 0


def _manual_login(store: CredentialStore, token: Optional[str]) -> int:
    text = token
    if text is None:
        say("Paste your DeepSeek userToken and press Enter. Find it in the browser at")
        say("DevTools (F12) > Application > Local Storage > https://chat.deepseek.com > userToken.")
        say("A request copied with DevTools 'Copy as cURL' works too.")
        lines: List[str] = []
        try:
            while len(lines) < 400:
                line = input("> " if not lines else "")
                lines.append(line)
                # Read a multi-line cURL to its end (cmd ^, bash \, PowerShell ` continuations),
                # otherwise the leftover lines would run as commands in the console afterwards.
                if not line.rstrip().endswith(("^", "\\", "`")) and (line.strip() or len(lines) > 1):
                    break
        except (EOFError, KeyboardInterrupt):
            say()
        text = "\n".join(lines)
    parsed, extra = parse_pasted_login(text or "")
    if not parsed:
        say("That doesn't look like a DeepSeek token.")
        return 1
    previous = store.get() if not store.env_token else {}
    creds: Dict[str, Any] = {
        "token": parsed,
        "device_id": previous.get("device_id") or str(uuid.uuid4()),
        "saved_at": int(time.time()),
        **extra,
    }
    return _finish_login(store, creds)


def parse_pasted_login(text: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Token plus, for a pasted "Copy as cURL", the browser fingerprint it carries."""
    if '^"' in text:  # "Copy as cURL (cmd)" escapes characters with ^
        text = re.sub(r"\^(.)", r"\1", re.sub(r"\^\r?\n", "\n", text))
    extra: Dict[str, str] = {}
    for key, pattern in (
        ("user_agent", r"(?im)user-agent:\s*([^\"'\r\n]+)"),
        ("client_version", r"(?im)x-client-version:\s*([0-9.]+)"),
        ("locale", r"(?im)x-client-locale:\s*([A-Za-z_-]+)"),
    ):
        match = re.search(pattern, text)
        if match:
            extra[key] = match.group(1).strip()
    return extract_user_token(text), extra


def cmd_login(args: argparse.Namespace) -> int:
    store = CredentialStore(credentials_path())
    if args.token is not None or args.manual:
        return _manual_login(store, args.token)
    if not args.no_import and not args.browser:
        say("Looking for a DeepSeek login in your browsers (Chrome, Edge, Brave, ...)...")
        creds = import_browser_login()
        if creds:
            say("Using it. Signing out of DeepSeek in that browser also signs out this script.")
            return _finish_login(store, creds)
        say("None found, so a browser window will open for you to sign in.\n")
    picked = pick_browser(args.browser)
    if not picked:
        if args.browser:
            say(f"Browser not found: {args.browser}")
        else:
            say("No Chromium-based browser found (looked for Google Chrome, Microsoft Edge, Brave and Chromium).")
        say("Install one, pass --browser <path-to-browser.exe>, or paste a token with: login --manual")
        if sys.stdin is not None and sys.stdin.isatty():
            return _manual_login(store, None)
        return 1
    kind, exe = picked
    say(f"Opening {kind}: {exe}")
    say("Sign in to DeepSeek in that window (Google, email or phone). It is an ordinary browser")
    say("window that nothing controls, so the captcha behaves as usual. It closes by itself a few")
    say("seconds after you are signed in; if it doesn't, close it yourself and the login is picked up.")
    say(f"Waiting up to {int(args.timeout)} seconds; press Ctrl+C to cancel.")
    try:
        creds = window_login(exe, browser_profile_dir(), timeout=args.timeout)
    except LoginError as exc:
        say(f"Login failed: {exc}")
        say("If the captcha keeps failing: sign in to chat.deepseek.com in the browser you normally")
        say("use and run 'login' again (it reuses that login), or paste a token with 'login --manual'.")
        return 1
    except KeyboardInterrupt:
        say("Cancelled.")
        return 130
    return _finish_login(store, creds)


def _remove_tree(path: Path) -> None:
    def make_writable(func: Callable[..., Any], target: str, *_: Any) -> None:
        os.chmod(target, 0o700)  # Windows marks some profile files read-only
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def cmd_logout(args: argparse.Namespace) -> int:
    store = CredentialStore(credentials_path())
    say("Removed the saved token." if store.delete() else "No saved token to remove.")
    profile = browser_profile_dir()
    if args.all and profile.exists():
        try:
            _remove_tree(profile)
            say(f"Removed the browser profile ({profile}).")
        except OSError as exc:
            say(f"Could not remove {profile}: {exc} (close the login browser window first)")
            return 1
    elif profile.exists():
        say("The browser profile was kept (the next login is quicker); use 'logout --all' to remove it.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = CredentialStore(credentials_path(), env_token=os.environ.get("DEEPSEEK_USER_TOKEN"))
    creds = store.get()
    token = extract_user_token(creds.get("token"))
    source = creds.get("source") if isinstance(creds.get("source"), dict) else {}
    if source.get("type") == "browser":
        origin = f"{source.get('browser')} (profile \"{Path(str(source.get('profile'))).name}\")"
    elif source.get("type") == "window":
        origin = "the login browser window"
    else:
        origin = "pasted token" if token else "-"
    say(f"Data folder     : {app_dir()}")
    say(f"Credentials     : {store.path} ({'found' if store.path.exists() else 'not found'})")
    say(f"Token           : {mask_secret(token)}{' (from DEEPSEEK_USER_TOKEN)' if store.env_token else ''}")
    say(f"Login from      : {origin}")
    say(f"Client version  : {creds.get('client_version') or DEFAULT_CLIENT_VERSION}")
    if not token:
        say("Not logged in. Run: py deepseek_web_api.py login")
        return 1
    client = DeepSeekClient(store)
    try:
        access = client.access_token(force=True)
        types = client.enabled_model_types(access)
    except UpstreamError as exc:
        say(f"Check failed    : {exc}")
        return 1
    say(f"Account         : {client.account.get('email') or '(unknown)'}")
    say(f"Model types     : {', '.join(sorted(types))}")
    return 0


def _make_app(args: argparse.Namespace, store: CredentialStore) -> App:
    return App(
        store,
        keep_sessions=getattr(args, "keep_sessions", False),
        history_window=getattr(args, "history_window", 0) or 0,
        auto_continue=not getattr(args, "no_auto_continue", False),
        auto_refresh=not getattr(args, "no_auto_refresh", False),
        api_key=getattr(args, "api_key", None),
        cors=getattr(args, "cors", False),
        check_host=getattr(args, "host", DEFAULT_HOST) in ("127.0.0.1", "localhost", "::1"),
        max_concurrency=getattr(args, "max_concurrency", 4) or 4,
    )


def cmd_chat(args: argparse.Namespace) -> int:
    store = CredentialStore(credentials_path(), env_token=os.environ.get("DEEPSEEK_USER_TOKEN"))
    app = _make_app(args, store)
    body: Dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": " ".join(args.prompt)}],
        "stream": True,
    }
    if args.think:
        body["thinking_enabled"] = True
    if args.search:
        body["search_enabled"] = True
    out = sys.stdout
    in_reasoning = False
    events: Optional[Iterator[Tuple[str, Any]]] = None
    try:
        events = ChatJob(app, body).run()
        for kind, value in events:
            if kind == "reasoning":
                if not in_reasoning:
                    out.write("[thinking]\n")
                    in_reasoning = True
                out.write(value)
            elif kind == "content":
                if in_reasoning:
                    out.write("\n[/thinking]\n\n")
                    in_reasoning = False
                out.write(value)
            elif kind == "end":
                out.write("\n")
                if value.get("error"):
                    say(f"Error: {value['error']}")
                    return 1
            out.flush()
    except (UpstreamError, BadRequest) as exc:
        say(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 130
    finally:
        if events is not None:
            events.close()  # schedules the deletion of the temporary chat
        app.client.wait_for_cleanup()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    store = CredentialStore(credentials_path(), env_token=args.token or os.environ.get("DEEPSEEK_USER_TOKEN"))
    if not extract_user_token(store.get().get("token")):
        if args.no_auto_login:
            say("Not logged in. Run: py deepseek_web_api.py login")
            return 1
        say("No saved DeepSeek login yet - starting the browser login first.\n")
        login_args = argparse.Namespace(browser=args.browser, timeout=300.0, manual=False, token=None, no_import=False)
        code = cmd_login(login_args)
        if code != 0:
            return code
        say()
    app = _make_app(args, store)
    account = ""
    try:
        app.client.access_token(force=True)
        account = app.client.account.get("email") or ""
    except AuthError as exc:
        log(f"{exc}", "WARN")
    except UpstreamError as exc:
        log(f"Could not reach DeepSeek yet: {exc}", "WARN")
    try:
        server = ApiServer((args.host, args.port), ApiHandler, app)
    except OSError as exc:
        say(f"Could not listen on {args.host}:{args.port}: {exc}")
        say(f"Another program may be using the port; try --port {args.port + 1}.")
        return 1
    shown_host = args.host if args.host not in ("0.0.0.0", "::") else "127.0.0.1"
    if ":" in shown_host:
        shown_host = f"[{shown_host}]"
    say("DeepSeek Web API is running")
    say(f"  Base URL : http://{shown_host}:{args.port}/v1")
    say(f"  API key  : {'required (--api-key)' if app.api_key else 'any value works (use --api-key to require one)'}")
    if account:
        say(f"  Account  : {account}")
    say(f"  Models   : {', '.join(MODEL_IDS[:4])}, ... (GET /v1/models)")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not app.api_key:
        say("  WARNING  : listening beyond localhost without --api-key; anyone on your network can use your account.")
    say("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        say("\nStopping.")
    finally:
        server.server_close()
        app.client.wait_for_cleanup()
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    common.add_argument("--proxy", help="HTTP(S) proxy for DeepSeek requests (default: system proxy)")

    parser = argparse.ArgumentParser(
        prog="deepseek_web_api.py",
        description="DeepSeek Web (chat.deepseek.com) as an OpenAI-compatible API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  py deepseek_web_api.py login          reuse your browser's DeepSeek login or sign in\n"
            "  py deepseek_web_api.py                start the API on http://127.0.0.1:20129/v1\n"
            "  py deepseek_web_api.py serve --port 8080 --api-key secret\n"
            '  py deepseek_web_api.py chat "Hello!" --think\n'
            "  py deepseek_web_api.py status | logout [--all]"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", parents=[common], help="run the OpenAI-compatible server (default)")
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"interface to listen on (default {DEFAULT_HOST})")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    serve.add_argument("--api-key", default=os.environ.get("DEEPSEEK_WEB_API_KEY"), help="require this API key from clients")
    serve.add_argument("--token", help="use this DeepSeek userToken instead of the saved login")
    serve.add_argument("--browser", help="browser for the automatic first login")
    serve.add_argument("--no-auto-login", action="store_true", help="don't open the browser login when not logged in")
    serve.add_argument("--no-auto-refresh", action="store_true", help="don't refresh a rejected login from the browser profile")
    serve.add_argument("--no-auto-continue", action="store_true", help="don't auto-continue answers DeepSeek cuts off")
    serve.add_argument("--history-window", type=int, default=0, help="turns replayed for multi-turn chats (default: auto, 20)")
    serve.add_argument("--keep-sessions", action="store_true", help="keep the temporary chats in your DeepSeek history")
    serve.add_argument("--cors", action="store_true", help="allow browser apps on other origins (Access-Control-Allow-Origin: *)")
    serve.add_argument("--max-concurrency", type=int, default=4, help="parallel upstream requests (default 4)")

    login = sub.add_parser("login", parents=[common], help="reuse your browser's DeepSeek login, or sign in in a window")
    login.add_argument("--browser", help="open this browser to sign in: chrome, edge, brave, chromium or a path")
    login.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the sign-in (default 300)")
    login.add_argument("--manual", action="store_true", help="paste a token instead of using a browser")
    login.add_argument("--token", help="save this token (or 'Copy as cURL' text) directly")
    login.add_argument("--no-import", action="store_true", help="don't reuse a DeepSeek login found in your browsers")

    logout = sub.add_parser("logout", parents=[common], help="delete the saved token")
    logout.add_argument("--all", action="store_true", help="also delete the browser profile")

    sub.add_parser("status", parents=[common], help="show the login state and check the token")

    chat = sub.add_parser("chat", parents=[common], help="send one prompt from the terminal")
    chat.add_argument("prompt", nargs="+", help="the message to send")
    chat.add_argument("--model", default="deepseek-v4-flash", help="model name (default deepseek-v4-flash)")
    chat.add_argument("--think", action="store_true", help="enable DeepThink reasoning")
    chat.add_argument("--search", action="store_true", help="enable web search")
    return parser


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _owns_console_window() -> bool:
    """True on Windows when the script was double-clicked (no cmd/PowerShell around it),
    so the window would close before an error can be read."""
    if os.name != "nt" or "PROMPT" in os.environ:  # cmd.exe sets PROMPT
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 8)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ImportError):
        return False
    return 0 < count <= 2  # python.exe, plus py.exe when launched through the py launcher


def main(argv: Optional[List[str]] = None) -> int:
    global _verbose
    _setup_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"serve", "login", "logout", "status", "chat"}
    if not any(arg in commands for arg in argv) and not {"-h", "--help", "--version"} & set(argv):
        argv.insert(0, "serve")  # double-click / no arguments -> run the server
    args = build_parser().parse_args(argv)
    _verbose = bool(getattr(args, "verbose", False))
    configure_proxy(getattr(args, "proxy", None))
    handlers = {"serve": cmd_serve, "login": cmd_login, "logout": cmd_logout, "status": cmd_status, "chat": cmd_chat}
    try:
        code = handlers[args.command](args)
    except KeyboardInterrupt:
        say("\nCancelled.")
        return 130
    except Exception:  # noqa: BLE001 - show the error before a double-clicked window closes
        traceback.print_exc()
        code = 1
    if code not in (0, 130) and _owns_console_window():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
