#!/usr/bin/env python3
"""media_linux.py - Linux media control layer for the CRT media widget.

The Linux twin of ``media.py``: same frozen ``MediaController`` surface (see
CONTRACT.md), same 19-key state object, but speaking MPRIS over the session
D-Bus instead of Windows.Media.Control / SMTC, and reading system volume from
PipeWire/WirePlumber (``wpctl``) or PulseAudio (``pactl``) instead of pycaw.

Frozen interface (identical names to the contract)::

    get_state() -> dict
    get_art(art_key: str) -> str | None
    play_pause() -> bool
    next_track() -> bool
    previous_track() -> bool
    seek_fraction(fraction: float) -> bool
    set_volume(level: float) -> bool          # 0.0 .. 1.0, SYSTEM default sink
    toggle_mute() -> bool
    select_session(app_id: str) -> bool       # "" == follow the most recent player
    shutdown() -> None

plus module-level ``STATE_KEYS``, ``SESSION_KEYS`` (copied verbatim from
``media.py`` so the two platforms cannot drift) and three helpers the window
layer / environment self-check call: ``bus_ok()``, ``volume_backend()``,
``probe()``.

Architecture (mirrors ``media.py``)
-----------------------------------
ONE dedicated worker thread owns an asyncio event loop and the D-Bus
connection.  Public methods marshal onto that thread through
``asyncio.run_coroutine_threadsafe`` with a hard timeout of ~2 s, and
``get_state()`` returns a plain dict that never raises and never blocks past
that budget.  Per-player property reads are issued concurrently, each with its
own timeout, so one hung player yields ``"unknown"`` for that session instead of
failing the whole tick.

Lazy D-Bus import
-----------------
``dbus_fast`` is imported ONLY inside :class:`MprisTransport`, never at module
import time.  That keeps this module importable (and therefore unit-testable)
on a machine without a session bus - including Windows - which is how
``media_linux_selftest.py`` proves the mapping logic without a Linux box.

Transport contract (duck-typed, injectable)
-------------------------------------------
``MediaController`` talks to a *transport* object with this async surface::

    async def connect() -> None
    async def close() -> None
    async def list_names() -> list[str]                # MPRIS bus names
    async def read_player(bus_name, timeout=...) -> dict
    async def invoke(bus_name, member, *args, signature="", timeout=...) -> None

``read_player`` returns a normalised dict with exactly these keys (raw MPRIS
values, not yet converted to the contract's units)::

    identity, title, artist, album, art_url, length_us, trackid,
    status, position_us, can_seek, can_next, can_previous

``raise ServiceUnknownError`` from ``read_player`` / ``invoke`` when the bus
name has no owner (the player vanished).  The real transport raises it after
translating a D-Bus ``ServiceUnknown`` / ``NameHasNoOwner`` error; the fake one
in the self-test raises it directly.

MPRIS mapping
-------------
* session bus, object path ``/org/mpris/MediaPlayer2``, interfaces
  ``org.mpris.MediaPlayer2`` and ``org.mpris.MediaPlayer2.Player``;
* ``has_session``: ``org.freedesktop.DBus.ListNames`` filtered on names
  starting ``org.mpris.MediaPlayer2.``;
* ``app_id``: the MPRIS bus name itself (also the key ``select_session`` takes);
* ``app_name``: ``Identity``, falling back to the bus name's tail;
* ``title``: ``Metadata["xesam:title"]``; ``artist``:
  ``Metadata["xesam:artist"]`` - type ``as``, a LIST of strings, joined with
  ", " (never assumed to be a bare string); ``album``:
  ``Metadata["xesam:album"]``;
* ``status``: ``PlaybackStatus`` ``Playing``/``Paused``/``Stopped`` ->
  ``playing``/``paused``/``stopped``; ``closed`` when the selected player's
  name vanishes mid-tick (``ServiceUnknown`` / ``NameOwnerChanged``);
  ``unknown`` otherwise;
* ``position``: the ``Position`` property is in MICROSECONDS and is NOT
  signalled, so it is polled every tick; ``duration``:
  ``Metadata["mpris:length"]``, also microseconds.  Both are converted to the
  same unit ``media.py`` emits - float seconds - and clamped exactly the way
  ``media.py`` clamps them.  Absent, zero or absurd length means ``null``
  duration and ``can_seek`` false;
* ``can_seek``/``can_next``/``can_previous``: ``CanSeek``/``CanGoNext``/
  ``CanGoPrevious`` (``can_seek`` additionally requires a real duration);
* ``art_key``: first 8 hex chars of a SHA-1 of ``Metadata["mpris:artUrl"]``,
  ``null`` when there is no art.  ``get_art`` resolves the cached URL:
  ``file://`` read from disk, ``http(s)`` fetched, downscaled to at most
  160x160 with Pillow, returned as ``data:<mime>;base64,...``; the LRU keeps
  at least the last 8;
* ``volume``/``muted``: NOT MPRIS (a player's own ``Volume`` must never feed
  these) - they come from the system default sink via ``wpctl`` (preferred) or
  ``pactl`` (fallback), picked once at startup and re-probed if it starts
  failing;
* ``sessions``: one entry per MPRIS bus name, most recently ``Playing`` first,
  capped at 8;
* ``t_ms``: ``time.monotonic() * 1000`` at the moment of the read;
* ``error``: the last exception as a short string, cleared on the next success.

Fallback behaviour copied from ``media.py`` (a bug that was fixed once already)
------------------------------------------------------------------------------
A pinned session whose player has disappeared must FALL BACK to the current
player rather than leaving the widget blank.  ``select_session("")`` returns to
following the most recently playing player.  A mid-tick ``ServiceUnknown`` on
the selected player reports ``status: "closed"`` for that tick; the next tick
the name is gone from ``ListNames`` and the fallback takes over.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import concurrent.futures
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:  # Pillow is a provisioned dependency; artwork is best-effort without it.
    from PIL import Image
except Exception:  # pragma: no cover - only on a broken environment
    Image = None  # type: ignore[assignment]

__all__ = [
    "MediaController",
    "MprisTransport",
    "ServiceUnknownError",
    "VolumeBackend",
    "STATE_KEYS",
    "SESSION_KEYS",
    "MPRIS_PREFIX",
    "bus_ok",
    "volume_backend",
    "probe",
]

# -- MPRIS / D-Bus constants ------------------------------------------------
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_BUS_NAME = "org.mpris.MediaPlayer2"
PLAYER_PATH = "/org/mpris/MediaPlayer2"
IFACE_BASE = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"

# -- budgets (keep the whole tick comfortably inside the ~2 s contract) ------
DEFAULT_TIMEOUT = 2.0          # hard timeout for every marshalled call, seconds
CONNECT_TIMEOUT = 1.2          # session-bus connect
CONNECT_RETRY_S = 5.0          # re-attempt a failed connect after this long
LIST_TIMEOUT = 0.5             # ListNames
OWNER_TIMEOUT = 0.15           # GetNameOwner, per bus name (issued concurrently)
PER_PLAYER_TIMEOUT = 0.35      # one player's property read
CALL_TIMEOUT = 0.8             # one player method call
VOLUME_TIMEOUT = 1.0           # one wpctl/pactl invocation

ART_MAX_PX = 160               # longest side of the downscaled artwork
ART_CACHE_MAX = 8              # minimum number of cached artworks (LRU)
ART_MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 3.0
SESSIONS_MAX = 8
MAX_SANE_DURATION = 86400.0    # seconds; guards absurd / sentinel lengths
VANISH_GRACE_S = 3.0           # how long a vanished player still shows "closed"
US_PER_SECOND = 1_000_000

# One-line diagnostics for otherwise-silent best-effort failures, only when a
# debug/verbose flag is on: `--debug` or the CRT_DEBUG environment variable.
_DEBUG = bool(os.environ.get("CRT_DEBUG")) or ("--debug" in sys.argv)


def _debug(message: str) -> None:
    """Print a one-line diagnostic, but only in debug/verbose mode."""
    if not _DEBUG:
        return
    try:
        print("[crt-media-linux] %s" % message, file=sys.stderr, flush=True)
    except Exception:
        pass


# Exact top-level key set of get_state(); order matches CONTRACT.md.
# Copied verbatim from media.py - the two platforms must not drift.
STATE_KEYS = (
    "ok",
    "has_session",
    "app_id",
    "app_name",
    "title",
    "artist",
    "album",
    "status",
    "position",
    "duration",
    "can_seek",
    "can_next",
    "can_previous",
    "art_key",
    "volume",
    "muted",
    "sessions",
    "t_ms",
    "error",
)

SESSION_KEYS = ("app_id", "app_name", "title", "artist", "status")

# Bus-name tail -> friendly name, for players that publish no ``Identity``.
APP_NAME_OVERRIDES = {
    "spotify": "Spotify",
    "vlc": "VLC",
    "mpv": "mpv",
    "chromium": "Chromium",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "brave": "Brave",
    "rhythmbox": "Rhythmbox",
    "audacious": "Audacious",
    "clementine": "Clementine",
}

# MPRIS PlaybackStatus -> contract status.
_STATUS_MAP = {
    "playing": "playing",
    "paused": "paused",
    "stopped": "stopped",
    "closed": "closed",
}


class ServiceUnknownError(RuntimeError):
    """The D-Bus name has no owner (the player vanished mid-tick).

    The real transport raises this after translating a D-Bus
    ``org.freedesktop.DBus.Error.ServiceUnknown`` / ``NameHasNoOwner`` error.
    """


def _looks_like_service_unknown(exc: BaseException) -> bool:
    """True when a transport exception means 'that bus name is gone'."""
    if isinstance(exc, ServiceUnknownError):
        return True
    text = "%s %s" % (type(exc).__name__, exc)
    lowered = text.lower()
    return (
        "serviceunknown" in lowered
        or "namehasnoowner" in lowered
        or "unknownobject" in lowered
        or "not provided by any .service files" in lowered
    )


# ---------------------------------------------------------------------------
# small conversions
# ---------------------------------------------------------------------------

def _now_ms() -> float:
    """t_ms for the contract: time.monotonic() * 1000 at the moment of the read."""
    return time.monotonic() * 1000.0


def _short_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        msg = type(exc).__name__
    else:
        msg = "%s: %s" % (type(exc).__name__, msg)
    return msg[:300]


def _unwrap_dbus(value):
    """Recursively unwrap ``dbus_fast.signature.Variant`` layers.

    ``Properties.Get`` / ``GetAll`` return Variant-boxed values.  Without this,
    Identity becomes the debug string ``<dbus_fast.signature.Variant ('s',
    Brave)>``, PlaybackStatus fails ``isinstance(..., str)`` so status stays
    ``unknown``, and Metadata stays a Variant so title/artist/album are lost.
    Duck-typed on class name so this module stays importable without dbus_fast.
    """
    while True:
        cls = type(value)
        if cls.__module__ == "dbus_fast.signature" and cls.__name__ == "Variant":
            value = value.value
            continue
        if cls.__name__ == "Variant" and hasattr(value, "value"):
            # Broader fallback for renamed modules / vendored copies.
            try:
                value = value.value
            except Exception:
                break
            continue
        break
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key) if not isinstance(key, str) else key] = _unwrap_dbus(item)
        return out
    if isinstance(value, list):
        return [_unwrap_dbus(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_unwrap_dbus(item) for item in value)
    return value


def _us_to_seconds(value) -> "float | None":
    """MPRIS microsecond int -> float seconds, or None when unusable.

    Matches the unit ``media.py`` emits for position/duration (float seconds).
    """
    value = _unwrap_dbus(value)
    if value is None:
        return None
    try:
        us = float(value)
    except Exception:
        return None
    if us != us or us in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return us / float(US_PER_SECOND)


def _sane_duration(us_value) -> "float | None":
    """Duration in seconds, or None for 'no usable timeline'."""
    secs = _us_to_seconds(us_value)
    if secs is None or secs <= 0.0 or secs > MAX_SANE_DURATION:
        return None
    return secs


def _text(value) -> "str | None":
    """Normalize a D-Bus string-ish value to a stripped str or None."""
    value = _unwrap_dbus(value)
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    # Never surface a leaked Variant repr as a display string.
    if text.startswith("<dbus_fast.signature.Variant"):
        return None
    return text or None


def _as_int(value) -> "int | None":
    value = _unwrap_dbus(value)
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _as_bool(value) -> bool:
    value = _unwrap_dbus(value)
    return bool(value) if value is not None else False


def join_artists(value) -> "str | None":
    """``Metadata["xesam:artist"]`` -> display string.

    MPRIS types ``xesam:artist`` as ``as`` - a LIST of strings - so a bare
    string must never be assumed.  Lists are joined with ", "; a single string
    is passed through; an empty result is None.
    """
    value = _unwrap_dbus(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (bytes, bytearray, dict)):
        return None
    try:
        parts = [str(_unwrap_dbus(item)).strip() for item in value]
    except Exception:
        return None
    parts = [part for part in parts if part]
    return ", ".join(parts) or None


def map_status(value) -> str:
    """MPRIS PlaybackStatus -> contract status (never raises)."""
    value = _unwrap_dbus(value)
    if not isinstance(value, str):
        return "unknown"
    return _STATUS_MAP.get(value.strip().lower(), "unknown")


def keep_owned(names, owner_results) -> "list[str]":
    """Filter MPRIS bus names by whether their owner resolved.

    ``owner_results`` is one entry per name: ``True`` (owner found), ``False``
    (explicitly no owner) or an exception (timeout / error).  Fail OPEN - a
    name is kept unless it explicitly resolved to no owner.
    """
    keep = []
    for name, result in zip(names, owner_results):
        if result is True or isinstance(result, BaseException):
            keep.append(name)
    return keep


def art_key_from_url(url) -> "str | None":
    """First 8 hex chars of a SHA-1 of the art URL; None when there is no art."""
    if url is None:
        return None
    try:
        text = str(url).strip()
    except Exception:
        return None
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def app_name_from_bus(bus_name) -> "str | None":
    """``org.mpris.MediaPlayer2.spotify`` -> ``Spotify`` (fallback only)."""
    name = _text(bus_name)
    if not name:
        return None
    tail = name[len(MPRIS_PREFIX):] if name.startswith(MPRIS_PREFIX) else name
    tail = tail.split(".", 1)[0]
    if tail.lower().endswith(".instance"):
        tail = tail[: -len(".instance")]
    if not tail:
        return None
    if tail.lower() in APP_NAME_OVERRIDES:
        return APP_NAME_OVERRIDES[tail.lower()]
    if tail.islower() or tail.isupper():
        return tail.title()
    return tail


# ---------------------------------------------------------------------------
# artwork helpers
# ---------------------------------------------------------------------------

def _file_url_to_path(url: str) -> str:
    """``file:///home/x/a.png`` -> a local filesystem path."""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = "//%s%s" % (parsed.netloc, path)
    return urllib.request.url2pathname(path)


_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _sniff_mime(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return ""


def _fetch_url(url: str) -> "tuple[bytes, str] | None":
    """Fetch raw artwork bytes + mime from a file:// or http(s) URL."""
    try:
        if url.startswith("file://"):
            path = _file_url_to_path(url)
            with open(path, "rb") as handle:
                raw = handle.read(ART_MAX_BYTES + 1)
            if not raw or len(raw) > ART_MAX_BYTES:
                return None
            mime = _MIME_BY_SUFFIX.get(os.path.splitext(path)[1].lower(), "")
            return raw, (mime or _sniff_mime(raw))
        if url.startswith(("http://", "https://")):
            request = urllib.request.Request(
                url, headers={"User-Agent": "crt-media-widget/1.0"}
            )
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                raw = response.read(ART_MAX_BYTES + 1)
                mime = ""
                try:
                    mime = (response.headers.get_content_type() or "").strip()
                except Exception:
                    mime = ""
            if not raw or len(raw) > ART_MAX_BYTES:
                return None
            return raw, (mime if mime.startswith("image/") else _sniff_mime(raw))
        return None
    except Exception as exc:
        _debug("art fetch failed for %r: %s" % (url, _short_error(exc)))
        return None


def _encode_art(raw: bytes, mime: str = "") -> "str | None":
    """Raw image bytes -> 'data:<mime>;base64,...' downscaled to <=160x160.

    With Pillow available the image is re-encoded to PNG (matching what
    ``media.py`` emits); without it the original bytes are passed through with
    whatever mime we could determine.
    """
    if not raw:
        return None
    if Image is None:
        if len(raw) > ART_MAX_BYTES:
            return None
        return "data:%s;base64,%s" % (
            (mime or _sniff_mime(raw) or "application/octet-stream"),
            base64.b64encode(raw).decode("ascii"),
        )
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        return None
    try:
        image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
    except Exception:
        try:
            image = image.convert("RGB")
        except Exception:
            return None
    try:
        image.thumbnail((ART_MAX_PX, ART_MAX_PX), Image.LANCZOS)
    except Exception:
        try:
            image.thumbnail((ART_MAX_PX, ART_MAX_PX))
        except Exception:
            return None
    buf = io.BytesIO()
    try:
        image.save(buf, format="PNG")
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _blank_state() -> dict:
    """Every contract key present, with safe fallbacks (identical to media.py)."""
    return {
        "ok": True,
        "has_session": False,
        "app_id": None,
        "app_name": None,
        "title": None,
        "artist": None,
        "album": None,
        "status": "unknown",
        "position": None,
        "duration": None,
        "can_seek": False,
        "can_next": False,
        "can_previous": False,
        "art_key": None,
        "volume": 0.0,
        "muted": False,
        "sessions": [],
        "t_ms": _now_ms(),
        "error": None,
    }


# ---------------------------------------------------------------------------
# real transport: dbus-fast (imported lazily, never at module import time)
# ---------------------------------------------------------------------------

class MprisTransport:
    """Owns the session D-Bus connection and the MPRIS property/call plumbing.

    ``dbus_fast`` is imported inside :meth:`connect`, so this module can be
    imported - and its mapping logic exercised against a fake transport - on a
    machine with no D-Bus at all.
    """

    def __init__(self, bus_kind: str = "session") -> None:
        self.bus_kind = bus_kind
        self._bus = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._bus is not None

    async def connect(self) -> None:
        try:
            from dbus_fast import BusType, Message, MessageType  # noqa: F401,PLC0415
            from dbus_fast.aio import MessageBus  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("dbus-fast is not installed: %s" % _short_error(exc))
        kind = BusType.SYSTEM if self.bus_kind == "system" else BusType.SESSION
        self._bus = await MessageBus(bus_type=kind).connect()

    async def close(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is None:
            return
        try:
            bus.disconnect()
        except Exception:
            pass

    # -- plumbing ----------------------------------------------------------

    def _require(self):
        if self._bus is None:
            raise RuntimeError("transport is not connected")
        return self._bus

    def _check(self, reply) -> None:
        from dbus_fast import MessageType  # noqa: PLC0415

        if reply is None:
            raise RuntimeError("no reply from the bus")
        if reply.message_type != MessageType.ERROR:
            return
        error_name = str(getattr(reply, "error_name", "") or "")
        detail = ""
        try:
            if reply.body:
                detail = str(reply.body[0])
        except Exception:
            detail = ""
        if _looks_like_service_unknown(RuntimeError(error_name)):
            raise ServiceUnknownError("%s: %s" % (error_name, detail))
        raise RuntimeError("%s: %s" % (error_name, detail))

    async def list_names(self, timeout: float = LIST_TIMEOUT) -> "list[str]":
        """``ListNames`` filtered on the MPRIS prefix and on a resolvable owner.

        Only a name that explicitly resolves to no owner
        (``NameHasNoOwner``) is dropped - any other failure fails OPEN, because
        losing a live widget is worse than briefly showing a name that the
        per-player read will itself report as ``closed``/``unknown``.
        """
        from dbus_fast import Message  # noqa: PLC0415

        bus = self._require()
        message = Message(
            destination=DBUS_NAME,
            path=DBUS_PATH,
            interface=DBUS_IFACE,
            member="ListNames",
        )
        reply = await asyncio.wait_for(bus.call(message), timeout)
        self._check(reply)
        names = reply.body[0] if reply.body else []
        candidates = [str(name) for name in names if str(name).startswith(MPRIS_PREFIX)]
        if not candidates:
            return []
        results = await asyncio.gather(
            *[self._has_owner(bus, name, OWNER_TIMEOUT) for name in candidates],
            return_exceptions=True,
        )
        return keep_owned(candidates, results)

    async def _get_all(self, dest: str, iface: str, timeout: float) -> dict:
        from dbus_fast import Message  # noqa: PLC0415

        bus = self._require()
        message = Message(
            destination=dest,
            path=PLAYER_PATH,
            interface=IFACE_PROPS,
            member="GetAll",
            signature="s",
            body=[iface],
        )
        reply = await asyncio.wait_for(bus.call(message), timeout)
        self._check(reply)
        if not reply.body:
            return {}
        value = _unwrap_dbus(reply.body[0])
        if not value:
            return {}
        try:
            return dict(value)
        except Exception:
            return {}

    async def _get_prop(self, dest: str, iface: str, name: str, timeout: float):
        from dbus_fast import Message  # noqa: PLC0415

        bus = self._require()
        message = Message(
            destination=dest,
            path=PLAYER_PATH,
            interface=IFACE_PROPS,
            member="Get",
            signature="ss",
            body=[iface, name],
        )
        reply = await asyncio.wait_for(bus.call(message), timeout)
        self._check(reply)
        if not reply.body:
            return None
        return _unwrap_dbus(reply.body[0])

    async def _has_owner(self, bus, bus_name: str, timeout: float) -> bool:
        """True when the well-known name currently has an owner."""
        from dbus_fast import Message, MessageType  # noqa: PLC0415

        message = Message(
            destination=DBUS_NAME,
            path=DBUS_PATH,
            interface=DBUS_IFACE,
            member="GetNameOwner",
            signature="s",
            body=[bus_name],
        )
        reply = await asyncio.wait_for(bus.call(message), timeout)
        if reply is None:
            return True
        return reply.message_type != MessageType.ERROR

    async def read_player(self, bus_name: str, timeout: float = PER_PLAYER_TIMEOUT) -> dict:
        """Read every MPRIS property this layer needs, normalised.

        Raises :class:`ServiceUnknownError` when the name has no owner.
        """
        base: dict = {}
        try:
            base = await self._get_all(bus_name, IFACE_BASE, timeout)
        except ServiceUnknownError:
            raise
        except Exception as exc:
            # A few players (mpv, some browser bridges) only implement the
            # Player interface; that is not fatal as long as Player works.
            _debug("Identity read failed for %s: %s" % (bus_name, _short_error(exc)))
            base = {}

        player = await self._get_all(bus_name, IFACE_PLAYER, timeout)
        # GetAll already unwraps Variants, but keep this defensive for injected
        # transports / partial failures.
        metadata = _unwrap_dbus(player.get("Metadata")) or {}
        try:
            metadata = dict(metadata)
        except Exception:
            metadata = {}
        metadata = _unwrap_dbus(metadata)
        if not isinstance(metadata, dict):
            metadata = {}

        position_us = player.get("Position")
        if position_us is None:
            try:
                position_us = await self._get_prop(
                    bus_name, IFACE_PLAYER, "Position", timeout
                )
            except Exception:
                position_us = None

        return {
            "identity": base.get("Identity"),
            "title": metadata.get("xesam:title"),
            "artist": metadata.get("xesam:artist"),
            "album": metadata.get("xesam:album"),
            "art_url": metadata.get("mpris:artUrl"),
            "length_us": metadata.get("mpris:length"),
            "trackid": metadata.get("mpris:trackid"),
            "status": player.get("PlaybackStatus"),
            "position_us": position_us,
            "can_seek": player.get("CanSeek"),
            "can_next": player.get("CanGoNext"),
            "can_previous": player.get("CanGoPrevious"),
        }

    async def invoke(
        self,
        bus_name: str,
        member: str,
        *args,
        signature: str = "",
        timeout: float = CALL_TIMEOUT,
    ) -> None:
        """Call a method on ``org.mpris.MediaPlayer2.Player``."""
        from dbus_fast import Message  # noqa: PLC0415

        bus = self._require()
        body = list(args)
        # Object paths: dbus-fast 5.x dropped the ObjectPath wrapper and wants a
        # plain str with signature "o".  Older releases exported ObjectPath from
        # dbus_fast / dbus_fast.signature.  Import it ONLY when needed so a
        # missing ObjectPath cannot break no-arg calls (Play/Pause/Next/...).
        if "o" in (signature or ""):
            object_path_ctor = None
            for import_path in (
                ("dbus_fast", "ObjectPath"),
                ("dbus_fast.signature", "ObjectPath"),
            ):
                try:
                    mod = __import__(import_path[0], fromlist=[import_path[1]])
                    object_path_ctor = getattr(mod, import_path[1])
                    break
                except Exception:
                    continue
            if object_path_ctor is not None:
                body = [
                    object_path_ctor(str(item)) if isinstance(item, str) else item
                    for item in body
                ]
            else:
                body = [str(item) if isinstance(item, str) else item for item in body]
        message = Message(
            destination=bus_name,
            path=PLAYER_PATH,
            interface=IFACE_PLAYER,
            member=member,
            signature=signature,
            body=body,
        )
        reply = await asyncio.wait_for(bus.call(message), timeout)
        self._check(reply)


# ---------------------------------------------------------------------------
# system volume: wpctl (PipeWire) preferred, pactl (PulseAudio) fallback
# ---------------------------------------------------------------------------

Wpctl_SINK = "@DEFAULT_AUDIO_SINK@"
Pactl_SINK = "@DEFAULT_SINK@"

_WPCTL_VOLUME_RE = re.compile(r"[Vv]olume:\s*([0-9]*\.?[0-9]+)")
_PACTL_PERCENT_RE = re.compile(r"([0-9]+)%")
_PACTL_MUTE_RE = re.compile(r"[Mm]ute:\s*(yes|no)", re.IGNORECASE)


def _subprocess_runner(cmd, timeout: float = VOLUME_TIMEOUT):
    """Default command runner: -> (returncode, stdout, stderr)."""
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class VolumeBackend:
    """Reads/writes the system default sink volume and mute state.

    The binary is picked ONCE at startup by probing what exists, cached, and
    re-probed if it starts failing.  Both the runner and the ``which`` lookup
    are injectable so the parsing can be proven without PipeWire or PulseAudio.
    """

    def __init__(self, runner=None, which=None) -> None:
        self._runner = runner or _subprocess_runner
        self._which = which or shutil.which
        self._name: "str | None" = None
        self._last = (0.0, False)
        self.last_error: "str | None" = None

    # -- backend selection -------------------------------------------------

    def probe(self, force: bool = False) -> str:
        """Return 'wpctl', 'pactl' or 'none'; cached unless `force`."""
        if self._name is not None and not force:
            return self._name
        for name in ("wpctl", "pactl"):
            try:
                if self._which(name):
                    self._name = name
                    return name
            except Exception:
                continue
        self._name = "none"
        return "none"

    def _force_reprobe(self) -> None:
        self._name = None
        self.probe(force=True)

    # -- runs --------------------------------------------------------------

    def _run(self, cmd):
        """Run one command; returns (rc, stdout, stderr) or None on failure."""
        try:
            out = self._runner(list(cmd))
        except Exception as exc:
            self.last_error = _short_error(exc)
            return None
        try:
            rc, stdout, stderr = out
        except Exception:
            self.last_error = "runner returned %r" % (out,)
            return None
        return int(rc), str(stdout or ""), str(stderr or "")

    # -- read --------------------------------------------------------------

    def read(self):
        """-> (level 0..1, muted) for the default sink, or None on failure."""
        name = self.probe()
        if name == "none":
            self.last_error = "no wpctl or pactl on PATH"
            return None
        try:
            if name == "wpctl":
                pair = self._read_wpctl()
            else:
                pair = self._read_pactl()
        except Exception as exc:
            self.last_error = _short_error(exc)
            pair = None
        if pair is None:
            # The cached backend just failed: re-probe next time in case the
            # session switched between PipeWire and PulseAudio.
            self._force_reprobe()
            return None
        level, muted = pair
        level = max(0.0, min(1.0, float(level)))
        self._last = (level, bool(muted))
        self.last_error = None
        return self._last

    def _read_wpctl(self):
        result = self._run(["wpctl", "get-volume", Wpctl_SINK])
        if result is None:
            return None
        rc, stdout, _stderr = result
        if rc != 0:
            self.last_error = "wpctl get-volume rc=%d" % rc
            return None
        match = _WPCTL_VOLUME_RE.search(stdout)
        if not match:
            self.last_error = "unparsed wpctl output: %r" % stdout.strip()[:120]
            return None
        muted = "[MUTED]" in stdout.upper() or "muted" in stdout.lower()
        return float(match.group(1)), muted

    def _read_pactl(self):
        volume_result = self._run(["pactl", "get-sink-volume", Pactl_SINK])
        if volume_result is None:
            return None
        rc, stdout, _stderr = volume_result
        if rc != 0:
            self.last_error = "pactl get-sink-volume rc=%d" % rc
            return None
        match = _PACTL_PERCENT_RE.search(stdout)
        if not match:
            self.last_error = "unparsed pactl volume: %r" % stdout.strip()[:120]
            return None
        level = float(match.group(1)) / 100.0
        muted = False
        mute_result = self._run(["pactl", "get-sink-mute", Pactl_SINK])
        if mute_result is not None:
            mrc, mout, _merr = mute_result
            if mrc == 0:
                mmatch = _PACTL_MUTE_RE.search(mout)
                if mmatch:
                    muted = mmatch.group(1).strip().lower() == "yes"
        return level, muted

    # -- write -------------------------------------------------------------

    def set(self, level: float) -> bool:
        """Set the default sink volume; True when the command succeeded."""
        try:
            level = max(0.0, min(1.0, float(level)))
        except Exception:
            return False
        name = self.probe()
        if name == "none":
            self.last_error = "no wpctl or pactl on PATH"
            return False
        if name == "wpctl":
            cmd = ["wpctl", "set-volume", Wpctl_SINK, "%.4f" % level]
        else:
            cmd = ["pactl", "set-sink-volume", Pactl_SINK, "%d%%" % int(round(level * 100))]
        result = self._run(cmd)
        if result is None or result[0] != 0:
            if result is not None:
                self.last_error = "%s rc=%d: %s" % (
                    cmd[0], result[0], (result[2] or "").strip()[:120]
                )
            self._force_reprobe()
            return False
        self._last = (level, self._last[1])
        self.last_error = None
        return True

    def toggle_mute(self) -> bool:
        """Toggle the default sink mute; True when the command succeeded."""
        name = self.probe()
        if name == "none":
            self.last_error = "no wpctl or pactl on PATH"
            return False
        if name == "wpctl":
            cmd = ["wpctl", "set-mute", Wpctl_SINK, "toggle"]
        else:
            cmd = ["pactl", "set-sink-mute", Pactl_SINK, "toggle"]
        result = self._run(cmd)
        if result is None or result[0] != 0:
            if result is not None:
                self.last_error = "%s rc=%d: %s" % (
                    cmd[0], result[0], (result[2] or "").strip()[:120]
                )
            self._force_reprobe()
            return False
        self._last = (self._last[0], not self._last[1])
        self.last_error = None
        return True

    @property
    def last(self):
        return self._last


_DEFAULT_VOLUME = None
_DEFAULT_VOLUME_LOCK = threading.Lock()


def _default_volume() -> VolumeBackend:
    """One process-wide backend so ``volume_backend()`` and the controller agree."""
    global _DEFAULT_VOLUME
    with _DEFAULT_VOLUME_LOCK:
        if _DEFAULT_VOLUME is None:
            _DEFAULT_VOLUME = VolumeBackend()
        return _DEFAULT_VOLUME


def volume_backend() -> str:
    """The system volume backend in use: 'wpctl', 'pactl' or 'none'."""
    try:
        return _default_volume().probe()
    except Exception:
        return "none"


# ---------------------------------------------------------------------------
# environment diagnostics for the Linux self-check
# ---------------------------------------------------------------------------

def _run_sync(coro_factory, timeout: float):
    """Run a coroutine to completion from sync code, with or without a loop.

    ``probe()``/``bus_ok()`` are called from the environment self-check, which
    may or may not already own a running event loop; ``asyncio.run`` refuses in
    the latter case, so fall back to a short-lived private thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(asyncio.wait_for(coro_factory(), timeout))

    box: dict = {}

    def body():
        try:
            box["value"] = asyncio.run(asyncio.wait_for(coro_factory(), timeout))
        except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
            box["error"] = exc

    thread = threading.Thread(target=body, name="crt-bus-probe", daemon=True)
    thread.start()
    thread.join(timeout + 1.0)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise TimeoutError("bus probe timed out after %.1fs" % timeout)
    return box["value"]


def _sync_list_players(timeout: float = 2.0) -> "list[str]":
    """Connect, list MPRIS bus names, disconnect.  Used by probe() only."""
    async def _job():
        transport = MprisTransport()
        await transport.connect()
        try:
            return await transport.list_names(timeout=LIST_TIMEOUT)
        finally:
            await transport.close()

    return _run_sync(_job, timeout)


def bus_ok() -> "tuple[bool, str]":
    """(reachable, human message) for the session bus.

    Never raises.  On a machine with no ``dbus_fast`` (e.g. Windows) this is
    ``(False, 'dbus-fast is not installed: ...')`` rather than an exception,
    which is exactly what the environment self-check wants to print.
    """
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or ""
    try:
        import dbus_fast  # noqa: F401,PLC0415
    except Exception as exc:
        return False, "dbus-fast unavailable: %s" % _short_error(exc)

    async def _job():
        transport = MprisTransport()
        await transport.connect()
        try:
            names = await transport.list_names(timeout=LIST_TIMEOUT)
            return names
        finally:
            await transport.close()

    try:
        names = _run_sync(_job, CONNECT_TIMEOUT + LIST_TIMEOUT + 0.5)
    except Exception as exc:
        where = address or "autolaunch"
        return False, "session bus unreachable at %s: %s" % (where, _short_error(exc))
    where = address or "autolaunch"
    return True, "session bus ok at %s (%d MPRIS name(s))" % (where, len(names))


def probe() -> dict:
    """A diagnostic dict for the environment self-check.  Never raises."""
    out = {
        "bus_ok": False,
        "bus_address": os.environ.get("DBUS_SESSION_BUS_ADDRESS") or "",
        "bus_message": "",
        "players": [],
        "player_count": 0,
        "volume_backend": "none",
        "error": None,
    }
    try:
        ok, message = bus_ok()
        out["bus_ok"] = ok
        out["bus_message"] = message
        if not ok:
            out["error"] = message
    except Exception as exc:
        out["error"] = _short_error(exc)
    try:
        out["volume_backend"] = volume_backend()
    except Exception as exc:
        out["error"] = out["error"] or _short_error(exc)
    if out["bus_ok"]:
        try:
            out["players"] = _sync_list_players()
            out["player_count"] = len(out["players"])
        except Exception as exc:
            out["error"] = _short_error(exc)
    return out


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------

class _Unavailable(RuntimeError):
    """The worker thread is not running."""


class _PlayerReadError(RuntimeError):
    """A per-player read failed for a reason other than the player vanishing."""


class _Worker(threading.Thread):
    """Owns the asyncio loop, the D-Bus connection and the volume backend."""

    def __init__(self, owner: "MediaController", transport, volume: VolumeBackend) -> None:
        super().__init__(name="crt-media-linux-worker", daemon=True)
        self._owner = owner
        self._transport = transport
        self._volume = volume
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self._ready = threading.Event()
        self.boot_error: "str | None" = None

        # --- everything below is touched only by the worker thread ----------
        self._connected = False
        self._connect_next_try = 0.0
        self._vol_last = (0.0, False)
        self._seen_players: "set[str]" = set()
        self._vanished: "dict[str, float]" = {}
        self._playing_ts: "dict[str, float]" = {}

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - Thread.run
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._boot())
        except Exception as exc:  # pragma: no cover - environment dependent
            self.boot_error = _short_error(exc)
        finally:
            self._ready.set()
        try:
            loop.run_forever()
        except Exception:
            pass
        finally:
            try:
                loop.run_until_complete(self._transport.close())
            except Exception:
                pass
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    async def _boot(self) -> None:
        """Runs on the worker thread; establishes the D-Bus connection."""
        await self._ensure()

    async def _ensure(self) -> bool:
        """Connect (or reconnect) the transport.  Never raises."""
        if self._connected:
            return True
        now = time.monotonic()
        if self._connect_next_try > now:
            return False
        try:
            await asyncio.wait_for(self._transport.connect(), CONNECT_TIMEOUT)
        except Exception as exc:
            self.boot_error = _short_error(exc)
            self._connect_next_try = now + CONNECT_RETRY_S
            _debug("session bus connect failed: %s" % self.boot_error)
            return False
        self._connected = True
        self.boot_error = None
        return True

    def stop(self, timeout: float = 3.0) -> None:
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self.is_alive():
            self.join(timeout)

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    # -- plumbing ----------------------------------------------------------

    def _desired(self) -> str:
        try:
            value = getattr(self._owner, "_desired_app_id", "")
        except Exception:
            return ""
        return "" if value is None else str(value)

    def _busname(self, name) -> str:
        return str(name)

    async def _read_one(self, name: str):
        """Read one player; returns the raw dict or the exception object."""
        try:
            raw = await asyncio.wait_for(
                self._transport.read_player(name, timeout=PER_PLAYER_TIMEOUT),
                PER_PLAYER_TIMEOUT + 0.25,
            )
        except asyncio.TimeoutError:
            return _PlayerReadError("timeout reading %s" % name)
        except Exception as exc:
            return exc
        if isinstance(raw, dict):
            return raw
        return _PlayerReadError("transport returned %r" % (raw,))

    def _view(self, name: str, raw) -> dict:
        """Raw MPRIS props (or a failure) -> a normalised per-session view."""
        view = {
            "app_id": str(name),
            "app_name": None,
            "title": None,
            "artist": None,
            "album": None,
            "status": "unknown",
            "position": None,
            "duration": None,
            "can_seek": False,
            "can_next": False,
            "can_previous": False,
            "art_url": None,
            "art_key": None,
            "trackid": None,
        }
        if isinstance(raw, BaseException):
            if _looks_like_service_unknown(raw):
                # The player's name vanished mid-tick.
                view["status"] = "closed"
            else:
                view["status"] = "unknown"
            view["app_name"] = app_name_from_bus(name)
            return view

        view["app_name"] = _text(raw.get("identity")) or app_name_from_bus(name)
        view["title"] = _text(raw.get("title"))
        view["artist"] = join_artists(raw.get("artist"))
        view["album"] = _text(raw.get("album"))
        view["status"] = map_status(raw.get("status"))
        view["trackid"] = _text(raw.get("trackid"))
        view["art_url"] = _text(raw.get("art_url"))
        view["art_key"] = art_key_from_url(view["art_url"])

        duration = _sane_duration(raw.get("length_us"))
        position = _us_to_seconds(raw.get("position_us"))
        if position is not None:
            if position < 0.0:
                position = 0.0
            if duration is not None and position > duration:
                position = duration
        view["duration"] = duration
        view["position"] = position
        view["can_next"] = _as_bool(raw.get("can_next"))
        view["can_previous"] = _as_bool(raw.get("can_previous"))
        # can_seek only when the player enables position changes AND publishes
        # a real duration (same rule as media.py).
        view["can_seek"] = bool(_as_bool(raw.get("can_seek"))) and duration is not None
        return view

    @staticmethod
    def _session_row(view: dict) -> dict:
        return {key: view.get(key) for key in SESSION_KEYS}

    def _order_names(self, names) -> list:
        """Most recently Playing first, then everything else, capped at 8."""
        def key(name):
            stamp = self._playing_ts.get(name)
            return (
                -(stamp if stamp is not None else -1.0),
                0 if name in names else 1,
                str(name),
            )

        return sorted(names, key=key)[:SESSIONS_MAX]

    def _newest_live(self, names, reads):
        """The most recently playing live player with a readable property set."""
        live = [name for name in names if isinstance(reads.get(name), dict)]
        if not live:
            return None
        return self._order_names(live)[0]

    # -- snapshot ----------------------------------------------------------

    async def snapshot(self) -> dict:
        """The contract state object.  Never raises out of here."""
        state = _blank_state()

        volume = None
        try:
            volume = await asyncio.to_thread(self._volume.read)
        except Exception as exc:
            _debug("volume read failed: %s" % _short_error(exc))
            volume = None
        if volume is None:
            state["volume"], state["muted"] = self._vol_last
        else:
            state["volume"], state["muted"] = volume
            self._vol_last = volume

        if not await self._ensure():
            state["ok"] = False
            state["error"] = self.boot_error or "session bus unavailable"
            state["t_ms"] = _now_ms()
            return state

        try:
            names = await asyncio.wait_for(
                self._transport.list_names(timeout=LIST_TIMEOUT),
                LIST_TIMEOUT + 0.3,
            )
        except Exception as exc:
            state["ok"] = False
            state["error"] = _short_error(exc)
            state["t_ms"] = _now_ms()
            return state

        names = [str(n) for n in (names or []) if str(n).startswith(MPRIS_PREFIX)]

        # Track vanishes for the "closed" status and for the sessions list.
        now = time.monotonic()
        for gone in self._seen_players - set(names):
            self._vanished.setdefault(gone, now)
        for gone in [n for n, ts in self._vanished.items() if (now - ts) > VANISH_GRACE_S]:
            del self._vanished[gone]
        self._seen_players = set(names)

        read_names = list(names) + [n for n in self._vanished if n not in names]
        reads: "dict[str, object]" = {}
        if read_names:
            results = await asyncio.gather(
                *[self._read_one(name) for name in read_names],
                return_exceptions=False,
            )
            reads = dict(zip(read_names, results))

        # Build every view first, then stamp "last seen playing" with one tick
        # clock, then order - so a player that started playing THIS tick leads
        # immediately rather than one poll later.
        tick_now = time.monotonic()
        views = {}
        for name in read_names:
            view = self._view(name, reads.get(name))
            if view["status"] == "playing":
                self._playing_ts[name] = tick_now
            views[name] = view

        rows = [self._session_row(views[name]) for name in self._order_names(read_names)]
        state["sessions"] = rows

        # Selection: pin first, then the most recently playing live player.
        # A pinned player that has disappeared FALLS BACK to the current player
        # rather than leaving the widget blank (regression fixed in media.py).
        desired = self._desired()
        selected = None
        if desired and desired in names:
            selected = desired
        else:
            selected = self._newest_live(names, reads)

        if selected is None:
            state["t_ms"] = _now_ms()
            return state

        view = views[selected]
        state["has_session"] = True
        state["app_id"] = view["app_id"]
        state["app_name"] = view["app_name"]
        state["title"] = view["title"]
        state["artist"] = view["artist"]
        state["album"] = view["album"]
        state["status"] = view["status"]
        state["position"] = view["position"]
        state["duration"] = view["duration"]
        state["can_seek"] = view["can_seek"]
        state["can_next"] = view["can_next"]
        state["can_previous"] = view["can_previous"]
        state["art_key"] = view["art_key"]
        if view["art_url"]:
            self._owner._note_art(view["art_key"], view["art_url"])
        # Anchor t_ms to the timeline read so the UI extrapolates from the same
        # instant the position was sampled.
        state["t_ms"] = _now_ms()
        # Surface the last transport command failure (play/pause/next) so the
        # UI/debug path is not silent when Chromium no-ops or rejects a call.
        err = getattr(self._owner, "_last_transport_error", None)
        if err and not state.get("error"):
            state["error"] = err
        return state

    # -- commands ----------------------------------------------------------

    async def _selected_name(self):
        """Enumerate + select, exactly as snapshot() does.  None when empty."""
        if not await self._ensure():
            return None
        names = await asyncio.wait_for(
            self._transport.list_names(timeout=LIST_TIMEOUT), LIST_TIMEOUT + 0.3
        )
        names = [str(n) for n in (names or []) if str(n).startswith(MPRIS_PREFIX)]
        if not names:
            return None
        results = await asyncio.gather(
            *[self._read_one(name) for name in names], return_exceptions=False
        )
        reads = dict(zip(names, results))
        desired = self._desired()
        if desired and desired in names:
            return desired
        return self._newest_live(names, reads)

    async def _invoke(self, name: str, member: str, *args, signature: str = "") -> bool:
        try:
            await asyncio.wait_for(
                self._transport.invoke(
                    name, member, *args, signature=signature, timeout=CALL_TIMEOUT
                ),
                CALL_TIMEOUT + 0.3,
            )
            self._owner._last_transport_error = None
            return True
        except Exception as exc:
            err = _short_error(exc)
            self._owner._last_transport_error = "%s on %s failed: %s" % (member, name, err)
            _debug("%s on %s failed: %s" % (member, name, err))
            return False

    async def _status_of(self, name: str) -> str:
        """Best-effort PlaybackStatus for the selected player ('playing'/'paused'/...)."""
        try:
            raw = await asyncio.wait_for(
                self._transport.read_player(name, timeout=PER_PLAYER_TIMEOUT),
                PER_PLAYER_TIMEOUT + 0.25,
            )
        except Exception:
            return "unknown"
        if not isinstance(raw, dict):
            return "unknown"
        return map_status(raw.get("status"))

    async def op_play_pause(self) -> bool:
        name = await self._selected_name()
        if not name:
            return False
        # Chromium/Brave (and some other web players) often advertise MPRIS but
        # implement Play()/Pause() while PlayPause() is a silent no-op or
        # missing. Prefer the explicit methods from current status; fall back
        # to PlayPause for players that only implement the toggle.
        status = await self._status_of(name)
        if status == "playing":
            if await self._invoke(name, "Pause"):
                return True
            return await self._invoke(name, "PlayPause")
        if status in ("paused", "stopped"):
            if await self._invoke(name, "Play"):
                return True
            return await self._invoke(name, "PlayPause")
        # Unknown status: try toggle first, then both directions.
        if await self._invoke(name, "PlayPause"):
            return True
        if await self._invoke(name, "Pause"):
            return True
        return await self._invoke(name, "Play")

    async def op_next(self) -> bool:
        name = await self._selected_name()
        if not name:
            return False
        return await self._invoke(name, "Next")

    async def op_previous(self) -> bool:
        name = await self._selected_name()
        if not name:
            return False
        return await self._invoke(name, "Previous")

    async def op_seek_fraction(self, fraction: float) -> bool:
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except Exception:
            return False
        name = await self._selected_name()
        if not name:
            return False
        # Re-read the track id immediately before the call: MPRIS silently
        # ignores SetPosition with a stale TrackId.
        try:
            raw = await asyncio.wait_for(
                self._transport.read_player(name, timeout=PER_PLAYER_TIMEOUT),
                PER_PLAYER_TIMEOUT + 0.25,
            )
        except Exception as exc:
            _debug("seek: read failed for %s: %s" % (name, _short_error(exc)))
            return False
        if not isinstance(raw, dict):
            return False

        length_us = _as_int(raw.get("length_us"))
        if not length_us or length_us <= 0:
            return False
        target_us = int(round(fraction * float(length_us)))

        trackid = _text(raw.get("trackid"))
        if trackid:
            return await self._invoke(name, "SetPosition", trackid, target_us, signature="ox")

        # No TrackId: fall back to the relative Seek(offset) form.
        offset = target_us
        current_us = _as_int(raw.get("position_us"))
        if current_us is not None:
            offset = target_us - int(current_us)
        return await self._invoke(name, "Seek", int(offset), signature="x")

    async def op_select_session(self, app_id: str) -> bool:
        name = await self._selected_name()
        if not name:
            return False
        if not app_id:
            return True
        return str(name) == app_id

    # -- system volume (subprocess work stays off the caller's thread) ------

    async def op_set_volume(self, level: float) -> bool:
        try:
            return bool(await asyncio.to_thread(self._volume.set, level))
        except Exception as exc:
            _debug("set_volume failed: %s" % _short_error(exc))
            return False

    async def op_toggle_mute(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self._volume.toggle_mute))
        except Exception as exc:
            _debug("toggle_mute failed: %s" % _short_error(exc))
            return False


# ---------------------------------------------------------------------------
# public controller
# ---------------------------------------------------------------------------

class MediaController:
    """Thread-safe facade over MPRIS plus the system default sink.

    Every method is safe to call from any thread, never raises, and returns a
    safe fallback (``False`` / ``None`` / ``ok: false``) instead of hanging or
    propagating an error.  ``get_state()`` returns the exact CONTRACT.md shape
    and never blocks longer than about two seconds.

    ``transport`` and ``volume`` are injectable so the whole mapping can be
    exercised against a fake MPRIS player on a machine with no D-Bus.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, transport=None, volume=None) -> None:
        self.timeout = float(timeout)
        self._art_lock = threading.Lock()
        self._art_urls: "collections.OrderedDict[str, str]" = collections.OrderedDict()
        self._art_data: "collections.OrderedDict[str, str]" = collections.OrderedDict()
        self._fetcher = _fetch_url
        self._sel_lock = threading.Lock()
        self._desired_app_id = ""
        self._last_transport_error: "str | None" = None
        self._transport = transport if transport is not None else MprisTransport()
        self._volume = volume if volume is not None else _default_volume()
        self._worker = _Worker(self, self._transport, self._volume)
        self._worker.start()
        self._worker.wait_ready(5.0)

    # -- artwork (shared between the worker and callers) -------------------

    def _note_art(self, key: "str | None", url: "str | None") -> None:
        """Remember which source URL an art_key came from (LRU, >= 8)."""
        if not key or not url:
            return
        with self._art_lock:
            self._art_urls[key] = url
            self._art_urls.move_to_end(key)
            while len(self._art_urls) > max(ART_CACHE_MAX, 1):
                self._art_urls.popitem(last=False)

    def _cache_art_data(self, key: str, data_url: str) -> None:
        with self._art_lock:
            self._art_data[key] = data_url
            self._art_data.move_to_end(key)
            while len(self._art_data) > max(ART_CACHE_MAX, 1):
                self._art_data.popitem(last=False)

    # -- marshalling -------------------------------------------------------

    def _call(self, factory, timeout: "float | None" = None):
        """Run `factory()` (a coroutine factory) on the worker thread."""
        worker = self._worker
        loop = worker.loop
        if loop is None or loop.is_closed() or not worker.is_alive():
            raise _Unavailable("media worker thread is not running")
        coro = factory()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=self.timeout if timeout is None else timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("media worker call timed out after %.1fs" % self.timeout)

    def _fail_state(self, error: str) -> dict:
        state = _blank_state()
        state["ok"] = False
        state["error"] = error
        state["volume"] = self._worker._vol_last[0]
        state["muted"] = self._worker._vol_last[1]
        state["t_ms"] = _now_ms()
        return state

    # -- contract surface --------------------------------------------------

    def get_state(self) -> dict:
        """Current media state in the exact CONTRACT.md shape.  Never raises."""
        try:
            return self._call(self._worker.snapshot)
        except Exception as exc:
            return self._fail_state(_short_error(exc))

    def get_art(self, art_key: str) -> "str | None":
        """'data:<mime>;base64,...' for a key previously reported, else None."""
        try:
            if not isinstance(art_key, str) or not art_key:
                return None
            with self._art_lock:
                cached = self._art_data.get(art_key)
                if cached is not None:
                    self._art_data.move_to_end(art_key)
                    return cached
                url = self._art_urls.get(art_key)
                if url is not None:
                    self._art_urls.move_to_end(art_key)
            if not url:
                return None
            fetched = self._fetcher(url)
            if not fetched:
                return None
            raw, mime = fetched
            data_url = _encode_art(raw, mime)
            if not data_url:
                return None
            self._cache_art_data(art_key, data_url)
            return data_url
        except Exception:
            return None

    def play_pause(self) -> bool:
        try:
            return bool(self._call(self._worker.op_play_pause))
        except Exception:
            return False

    def next_track(self) -> bool:
        try:
            return bool(self._call(self._worker.op_next))
        except Exception:
            return False

    def previous_track(self) -> bool:
        try:
            return bool(self._call(self._worker.op_previous))
        except Exception:
            return False

    def seek_fraction(self, fraction: float) -> bool:
        try:
            return bool(self._call(lambda: self._worker.op_seek_fraction(fraction)))
        except Exception:
            return False

    def set_volume(self, level: float) -> bool:
        try:
            return bool(self._call(lambda: self._worker.op_set_volume(level)))
        except Exception:
            return False

    def toggle_mute(self) -> bool:
        try:
            return bool(self._call(self._worker.op_toggle_mute))
        except Exception:
            return False

    def select_session(self, app_id: str) -> bool:
        """'' follows the most recent player; otherwise pin by MPRIS bus name."""
        try:
            app_id = "" if app_id is None else str(app_id)
        except Exception:
            return False
        with self._sel_lock:
            self._desired_app_id = app_id
        try:
            return bool(self._call(lambda: self._worker.op_select_session(app_id)))
        except Exception:
            return False

    def shutdown(self) -> None:
        """Stop the worker thread.  Idempotent, never raises."""
        try:
            self._worker.stop()
        except Exception:
            pass

    # -- introspection helpers (not part of the contract) ------------------

    def __enter__(self) -> "MediaController":
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()


def _main() -> int:
    """`python media_linux.py` prints a probe plus one get_state() snapshot."""
    print(json.dumps({"probe": probe()}, indent=2))
    controller = MediaController()
    try:
        print(json.dumps(controller.get_state(), indent=2))
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
