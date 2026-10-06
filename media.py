#!/usr/bin/env python3
"""media.py - Windows media control layer for the CRT media widget.

Frozen interface (see CONTRACT.md): the public surface of ``MediaController`` is
exactly::

    get_state() -> dict
    get_art(art_key: str) -> str | None
    play_pause() -> bool
    next_track() -> bool
    previous_track() -> bool
    seek_fraction(fraction: float) -> bool
    set_volume(level: float) -> bool          # 0.0 .. 1.0, SYSTEM endpoint volume
    toggle_mute() -> bool
    select_session(app_id: str) -> bool       # "" == follow the system's current session
    shutdown() -> None

Concurrency design
------------------
Every Windows Runtime and Core Audio call happens on ONE dedicated worker
thread that owns the WinRT session manager and the pycaw endpoint-volume
interface.  Public methods marshal onto that thread through
``asyncio.run_coroutine_threadsafe`` (a thread-safe queue) with a hard timeout
of ~2 s, so a caller is never blocked by an apartment-sensitive call and the UI
can poll ``get_state()`` from any thread.

The worker thread (and only it) initializes the COM/WinRT apartment, and it
does so as MTA -- ``comtypes`` is imported *after* ``sys.coinit_flags`` is
temporarily set to COINIT_MULTITHREADED, because comtypes otherwise initializes
the importing thread as STA at import time and an STA/MTA clash produces
``RPC_E_CHANGED_MODE`` ("Cannot change thread mode after it is set") for
whichever library loses the race.  The previous value of ``sys.coinit_flags``
is restored immediately, so importing this module has no lasting side effect on
the process or on the main thread (the UI thread must stay free to be STA).

Artwork
-------
``get_state()`` must never extract artwork: it only reports ``art_key`` (the
first 8 hex chars of a SHA-1 of the raw artwork bytes).  When the track
fingerprint (app_id/title/artist/album) changes, ``get_state()`` schedules a
*background* job on the worker and reports ``art_key: null`` for that one poll;
the next poll (600 ms later in the UI) reports the real key.  The downscaled
data URL is cached by key, LRU, at least the last 8 artworks.
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
import sys
import threading
import time

try:  # Pillow is a provisioned dependency; artwork is best-effort without it.
    from PIL import Image
except Exception:  # pragma: no cover - only on a broken environment
    Image = None  # type: ignore[assignment]

__all__ = ["MediaController", "app_name_from_aumid", "STATE_KEYS"]

DEFAULT_TIMEOUT = 2.0          # hard timeout for every marshalled call, seconds
ART_MAX_PX = 160               # longest side of the downscaled artwork
ART_CACHE_MAX = 8              # minimum number of cached artworks (LRU)
ART_MAX_BYTES = 8 * 1024 * 1024
SESSIONS_MAX = 8
VOLUME_REFRESH_S = 3.0         # how often we re-check which endpoint is default
SESSION_PROP_TTL = 2.0         # max staleness of title/artist for non-selected sessions
SESS_PROPS_MAX = 64            # hard cap on remembered per-app title/artist/album
SESS_PROPS_PRUNE_AGE = 30.0    # when over the cap, drop entries older than this, seconds
MAX_SANE_DURATION = 86400.0    # seconds; guards the Chromium "-1 day" style sentinels
TICKS_PER_SECOND = 10_000_000  # TimeSpan is 100 ns ticks

# One-line diagnostics for otherwise-silent best-effort failures, only when a
# debug/verbose flag is on: `--debug` (as `run.cmd --debug` passes it) or the
# CRT_DEBUG environment variable.  No logging framework, no new dependency.
_DEBUG = bool(os.environ.get("CRT_DEBUG")) or ("--debug" in sys.argv)


def _debug(message: str) -> None:
    """Print a one-line diagnostic, but only in debug/verbose mode."""
    if not _DEBUG:
        return
    try:
        print("[crt-media] %s" % message, file=sys.stderr, flush=True)
    except Exception:
        pass

# Exact top-level key set of get_state(); order matches CONTRACT.md.
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

# AUMID -> friendly name.  Keys are lowercase, ".exe" stripped, last dotted
# segment of the part after "!".
APP_NAME_OVERRIDES = {
    "spotify": "Spotify",
    "spotifymusic": "Spotify",
    "brave": "Brave",
    "bravebrowser": "Brave",
    "chrome": "Chrome",
    "googlechrome": "Chrome",
    "msedge": "Edge",
    "microsoftedge": "Edge",
    "vlc": "VLC",
    "foobar2000": "foobar2000",
    "musicbee": "MusicBee",
    "zune": "Groove",
    "groovemusic": "Groove",
    "wmplayer": "Windows Media Player",
    "applicationframehost": "System",
    "firefox": "Firefox",
    "opera": "Opera",
    "vivaldi": "Vivaldi",
}

_STATUS_NAMES = {
    "PLAYING": "playing",
    "PAUSED": "paused",
    "STOPPED": "stopped",
    "CLOSED": "closed",
    "OPENED": "unknown",
    "CHANGING": "unknown",
}
# Fallback if the enum cannot be introspected (values verified on this machine).
_STATUS_FALLBACK = {4: "playing", 5: "paused", 3: "stopped", 0: "closed", 1: "unknown", 2: "unknown"}


def _now_ms() -> float:
    """t_ms for the contract: time.monotonic() * 1000 at the moment of the read."""
    return time.monotonic() * 1000.0


def _seconds(value) -> "float | None":
    """timedelta/TimeSpan -> float seconds, or None when unusable."""
    try:
        if value is None:
            return None
        secs = value.total_seconds()
        secs = float(secs)
    except AttributeError:
        try:
            secs = float(value) / TICKS_PER_SECOND
        except Exception:
            return None
    except Exception:
        return None
    if secs != secs or secs in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return secs


def _sane_duration(value) -> "float | None":
    """Duration in seconds, or None for 'no usable timeline' (sentinels clamped)."""
    secs = _seconds(value)
    if secs is None or secs <= 0.0 or secs > MAX_SANE_DURATION:
        return None
    return secs


def _text(value) -> "str | None":
    """Normalize a WinRT string to a stripped str or None."""
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def app_name_from_aumid(aumid) -> "str | None":
    """'SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify' -> 'Spotify'.

    Rules: strip after '!', take the last dotted segment, clean it up, then
    title-case it, with a small override map for the common players/browsers.
    Unknown AUMIDs fall back to the cleaned last segment.
    """
    if aumid is None:
        return None
    try:
        raw = str(aumid).strip()
    except Exception:
        return None
    if not raw:
        return None

    seg = raw.split("!")[-1]
    seg = seg.replace("\\", "/")
    if "/" in seg:
        seg = seg.rsplit("/", 1)[-1]
    if seg.lower().endswith(".exe"):
        seg = seg[:-4]
    if "." in seg:
        seg = seg.split(".")[-1]
    seg = seg.strip().strip(" _-")
    if not seg:
        return None

    key = seg.lower()
    if key in APP_NAME_OVERRIDES:
        return APP_NAME_OVERRIDES[key]
    if seg.islower() or seg.isupper():
        return seg.title()
    return seg


def _blank_state() -> dict:
    """Every contract key present, with safe fallbacks."""
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


class _Unavailable(RuntimeError):
    """The worker thread is not running."""


def _short_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    if not msg:
        msg = type(exc).__name__
    elif not isinstance(exc, _Unavailable):
        msg = "%s: %s" % (type(exc).__name__, msg)
    return msg[:300]


def _encode_art(raw: bytes) -> "str | None":
    """Raw image bytes -> 'data:image/png;base64,...' downscaled to <=160x160."""
    if Image is None or not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return None
    try:
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
    except Exception:
        try:
            img = img.convert("RGB")
        except Exception:
            return None
    try:
        img.thumbnail((ART_MAX_PX, ART_MAX_PX), Image.LANCZOS)
    except Exception:
        try:
            img.thumbnail((ART_MAX_PX, ART_MAX_PX))
        except Exception:
            return None
    buf = io.BytesIO()
    try:
        img.save(buf, format="PNG")
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _art_key_and_url(raw: bytes):
    """Raw artwork bytes -> (art_key, data URL).  Pure CPU, runs off the loop."""
    key = hashlib.sha1(raw).hexdigest()[:8]
    return key, _encode_art(raw)


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------

class _Worker(threading.Thread):
    """Owns the WinRT session manager, the pycaw endpoint and the event loop."""

    def __init__(self, owner: "MediaController") -> None:
        super().__init__(name="crt-media-worker", daemon=True)
        self._owner = owner
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self._ready = threading.Event()
        self.boot_error: "str | None" = None

        # --- everything below is touched only by the worker thread ----------
        self.manager = None
        self.winrt = None            # winrt.windows.media.control module
        self.streams = None          # winrt.windows.storage.streams module
        self._audio_utilities = None
        self._status_map = dict(_STATUS_FALLBACK)
        self._sess_props: "dict[str, tuple[float, str | None, str | None, str | None]]" = {}
        self._vol_iface = None
        self._vol_dev_id: "str | None" = None
        self._vol_checked = 0.0
        self._vol_last = (0.0, False)
        self._art_fingerprint = None       # fingerprint whose extraction completed
        self._art_inflight = None          # fingerprint being extracted right now

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - Thread.run
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._boot(loop)
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
            try:
                if self.winrt is not None:
                    import winrt.runtime as _wrt  # noqa: PLC0415
                    _wrt.uninit_apartment()
            except Exception:
                pass

    def _boot(self, loop) -> None:
        """Runs on the worker thread; initializes the apartment it will own."""
        import winrt.runtime as wrt  # noqa: PLC0415

        # comtypes reads sys.coinit_flags at import time.  Force MTA (0) so it
        # agrees with WinRT, then restore whatever was there before: the UI
        # thread may legitimately want STA (WinForms).
        prev = getattr(sys, "coinit_flags", None)
        had_prev = hasattr(sys, "coinit_flags")
        sys.coinit_flags = 0
        try:
            self._boot_apartment(wrt, loop)
        finally:
            if had_prev:
                sys.coinit_flags = prev
            else:
                try:
                    del sys.coinit_flags
                except AttributeError:
                    pass

    def _boot_apartment(self, wrt, loop) -> None:
        try:
            wrt.init_apartment(wrt.ApartmentType.MULTI_THREADED)
        except Exception:
            # Already initialized (S_FALSE) or an STA process: WinRT objects
            # are still usable, so carry on rather than failing the layer.
            pass

        try:
            import comtypes  # noqa: F401, PLC0415
            from pycaw.pycaw import AudioUtilities  # noqa: PLC0415
            self._audio_utilities = AudioUtilities
        except Exception as exc:
            self._audio_utilities = None
            self.boot_error = "audio endpoint unavailable: %s" % _short_error(exc)

        import winrt.windows.media.control as wmc  # noqa: PLC0415
        import winrt.windows.storage.streams as wss  # noqa: PLC0415
        self.winrt = wmc
        self.streams = wss
        self._status_map = self._build_status_map(wmc)

        manager = loop.run_until_complete(
            wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        )
        self.manager = manager

    @staticmethod
    def _build_status_map(wmc) -> dict:
        enum = getattr(wmc, "GlobalSystemMediaTransportControlsSessionPlaybackStatus", None)
        out = dict(_STATUS_FALLBACK)
        if enum is None:
            return out
        for name, label in _STATUS_NAMES.items():
            member = getattr(enum, name, None)
            if member is None:
                continue
            try:
                out[int(member)] = label
            except Exception:
                continue
        return out

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

    # -- session plumbing --------------------------------------------------

    def _desired_app_id(self) -> str:
        return self._owner._desired_app_id  # owner guards it with its own lock

    def _sorted_sessions(self):
        """Every live session, newest first (by app-pushed update time)."""
        try:
            sessions = list(self.manager.get_sessions())
        except Exception:
            return []
        keyed = []
        now = time.time()
        for index, session in enumerate(sessions):
            stamp = -1.0
            try:
                stamp = float(session.get_timeline_properties().last_updated_time.timestamp())
            except Exception:
                stamp = now - index
            keyed.append((-stamp, index, session))
        keyed.sort()
        return [item[2] for item in keyed][:SESSIONS_MAX]

    def _select(self, sessions):
        """Apply the current selection; "" follows the system's current session.

        A pinned app id that has no live session must never black the widget
        out while other media is playing: fall back to the manager's current
        session, then to the newest live session.  The pin is kept, so the
        pinned app re-takes over if it comes back.
        """
        desired = self._desired_app_id()
        if desired:
            for session in sessions:
                if self._aumid(session) == desired:
                    return session
        try:
            current = self.manager.get_current_session()
        except Exception:
            current = None
        if current is not None:
            return current
        return sessions[0] if sessions else None

    @staticmethod
    def _aumid(session) -> "str | None":
        try:
            return _text(session.source_app_user_model_id)
        except Exception:
            return None

    def _playback(self, session):
        try:
            return session.get_playback_info()
        except Exception:
            return None

    async def _props(self, session, fresh: bool):
        """(title, artist, album) for a session; cached briefly unless `fresh`."""
        app_id = self._aumid(session) or ""
        now = time.monotonic()
        if not fresh:
            cached = self._sess_props.get(app_id)
            if cached is not None and (now - cached[0]) < SESSION_PROP_TTL:
                return cached[1], cached[2], cached[3]
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            return None, None, None
        title = _text(getattr(props, "title", None))
        artist = _text(getattr(props, "artist", None))
        album = _text(getattr(props, "album_title", None))
        self._sess_props[app_id] = (now, title, artist, album)
        self._prune_sess_props(now)
        return title, artist, album

    def _prune_sess_props(self, now: float) -> None:
        """Bound the per-app props map (TTL semantics below are unchanged).

        The map is keyed by app id and grows with every distinct app seen in a
        run, so once it exceeds the cap drop entries that have not been
        refreshed recently, then fall back to evicting the oldest.
        """
        if len(self._sess_props) <= SESS_PROPS_MAX:
            return
        cutoff = now - SESS_PROPS_PRUNE_AGE
        for app_id in [k for k, v in self._sess_props.items() if v[0] < cutoff]:
            del self._sess_props[app_id]
        while len(self._sess_props) > SESS_PROPS_MAX:
            oldest = min(self._sess_props, key=lambda k: self._sess_props[k][0])
            del self._sess_props[oldest]

    def _status_of(self, playback) -> str:
        if playback is None:
            return "unknown"
        try:
            return self._status_map.get(int(playback.playback_status), "unknown")
        except Exception:
            return "unknown"

    # -- artwork (background; never on the get_state critical path) --------

    def _want_art(self, session, fingerprint) -> None:
        if fingerprint == self._art_fingerprint or fingerprint == self._art_inflight:
            return
        self._art_inflight = fingerprint
        self._owner._set_art_key(None)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._art_job(session, fingerprint))
        except Exception:
            self._art_inflight = None

    async def _art_job(self, session, fingerprint) -> None:
        key = None
        try:
            key = await self._extract_art(session)
        except Exception as exc:
            _debug("artwork extraction failed: %s" % _short_error(exc))
            key = None
        if self._art_inflight == fingerprint:
            self._art_fingerprint = fingerprint
            self._art_inflight = None
            self._owner._set_art_key(key)

    async def _extract_art(self, session) -> "str | None":
        props = await session.try_get_media_properties_async()
        thumb = getattr(props, "thumbnail", None)
        if thumb is None:
            return None
        stream = await thumb.open_read_async()
        size = int(stream.size)
        if size <= 0 or size > ART_MAX_BYTES:
            return None
        reader = self.streams.DataReader(stream)
        await reader.load_async(size)
        raw = bytes(reader.read_buffer(size))
        if not raw:
            return None
        # Hash + downscale are pure CPU: keep them off the worker's event loop
        # so a concurrent get_state() still returns immediately.
        key, url = await asyncio.to_thread(_art_key_and_url, raw)
        if not key or url is None:
            return None
        self._owner._cache_art(key, url)
        return key

    # -- audio endpoint (pycaw) -------------------------------------------

    def _volume_iface(self):
        utilities = getattr(self, "_audio_utilities", None)
        if utilities is None:
            return None
        now = time.monotonic()
        if self._vol_iface is not None and (now - self._vol_checked) < VOLUME_REFRESH_S:
            return self._vol_iface
        self._vol_checked = now
        default_id = None
        try:
            from pycaw.pycaw import EDataFlow, ERole  # noqa: PLC0415
            enumerator = utilities.GetDeviceEnumerator()
            device = enumerator.GetDefaultAudioEndpoint(
                EDataFlow.eRender.value, ERole.eMultimedia.value
            )
            default_id = device.GetId()
        except Exception:
            default_id = None
        if self._vol_iface is None or (default_id is not None and default_id != self._vol_dev_id):
            audio_device = utilities.GetSpeakers()
            if audio_device is None:
                return self._vol_iface
            self._vol_iface = audio_device.EndpointVolume
            self._vol_dev_id = audio_device.id
        return self._vol_iface

    def _volume_read(self):
        try:
            iface = self._volume_iface()
            if iface is None:
                return None
            level = float(iface.GetMasterVolumeLevelScalar())
            muted = bool(iface.GetMute())
            level = max(0.0, min(1.0, level))
            self._vol_last = (level, muted)
            return (level, muted)
        except Exception:
            return None

    def _volume_set(self, level: float) -> bool:
        iface = self._volume_iface()
        if iface is None:
            raise _Unavailable("no audio endpoint")
        iface.SetMasterVolumeLevelScalar(max(0.0, min(1.0, float(level))), None)
        return True

    def _mute_toggle(self) -> bool:
        iface = self._volume_iface()
        if iface is None:
            raise _Unavailable("no audio endpoint")
        iface.SetMute(0 if bool(iface.GetMute()) else 1, None)
        return True

    async def _set_volume_async(self, level: float) -> bool:
        return self._volume_set(level)

    async def _mute_toggle_async(self) -> bool:
        return self._mute_toggle()

    # -- async operations --------------------------------------------------

    def _current_session(self):
        if self.manager is None:
            return None
        return self._select(self._sorted_sessions())

    async def snapshot(self) -> dict:
        """The contract state object.  Never raises out of here."""
        state = _blank_state()
        volume = self._volume_read()
        if volume is None:
            state["volume"], state["muted"] = self._vol_last
        else:
            state["volume"], state["muted"] = volume

        if self.manager is None:
            state["ok"] = False
            state["error"] = self.boot_error or "session manager unavailable"
            state["t_ms"] = _now_ms()
            return state

        try:
            sessions = self._sorted_sessions()
        except Exception as exc:
            state["ok"] = False
            state["error"] = _short_error(exc)
            state["t_ms"] = _now_ms()
            return state

        rows = []
        for session in sessions:
            playback = self._playback(session)
            title, artist, _album = await self._props(session, fresh=False)
            rows.append(
                {
                    "app_id": self._aumid(session),
                    "app_name": app_name_from_aumid(self._aumid(session)),
                    "title": title,
                    "artist": artist,
                    "status": self._status_of(playback),
                }
            )
        state["sessions"] = rows

        session = self._select(sessions)
        if session is None:
            state["t_ms"] = _now_ms()
            return state

        state["has_session"] = True
        state["app_id"] = self._aumid(session)
        state["app_name"] = app_name_from_aumid(state["app_id"])

        playback = self._playback(session)
        state["status"] = self._status_of(playback)
        controls = getattr(playback, "controls", None) if playback is not None else None
        if controls is not None:
            try:
                state["can_next"] = bool(controls.is_next_enabled)
            except Exception:
                state["can_next"] = False
            try:
                state["can_previous"] = bool(controls.is_previous_enabled)
            except Exception:
                state["can_previous"] = False

        title, artist, album = await self._props(session, fresh=True)
        if title is None and artist is None:
            # The selected session's own snapshot is the better fallback.
            for row in rows:
                if row["app_id"] == state["app_id"]:
                    title, artist = row["title"], row["artist"]
                    break
        state["title"] = title
        state["artist"] = artist
        state["album"] = album

        duration = None
        position = None
        try:
            timeline = session.get_timeline_properties()
            duration = _sane_duration(timeline.end_time)
            position = _seconds(timeline.position)
        except Exception:
            duration = None
            position = None
        if position is not None:
            if position < 0.0:
                position = 0.0
            if duration is not None and position > duration:
                position = duration
        state["duration"] = duration
        state["position"] = position
        # Anchor t_ms to the timeline read so the UI extrapolates from the
        # same instant the position was sampled.
        state["t_ms"] = _now_ms()
        if controls is not None and duration is not None:
            try:
                state["can_seek"] = bool(controls.is_playback_position_enabled)
            except Exception:
                state["can_seek"] = False

        fingerprint = (
            state["app_id"],
            state["title"],
            state["artist"],
            state["album"],
        )
        self._want_art(session, fingerprint)
        state["art_key"] = self._owner._current_art_key()
        return state

    async def op_play_pause(self) -> bool:
        session = self._current_session()
        if session is None:
            return False
        return bool(await session.try_toggle_play_pause_async())

    async def op_next(self) -> bool:
        session = self._current_session()
        if session is None:
            return False
        return bool(await session.try_skip_next_async())

    async def op_previous(self) -> bool:
        session = self._current_session()
        if session is None:
            return False
        return bool(await session.try_skip_previous_async())

    async def op_seek_fraction(self, fraction: float) -> bool:
        session = self._current_session()
        if session is None:
            return False
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except Exception:
            return False
        try:
            duration = _sane_duration(session.get_timeline_properties().end_time)
        except Exception:
            duration = None
        if duration is None:
            return False
        seconds = fraction * duration
        try:
            return bool(
                await session.try_change_playback_position_async(
                    int(round(seconds * TICKS_PER_SECOND))
                )
            )
        except TypeError:
            # Older/newer pywinrt signatures take a timedelta instead of ticks.
            import datetime  # noqa: PLC0415

            return bool(
                await session.try_change_playback_position_async(
                    datetime.timedelta(seconds=seconds)
                )
            )

    async def op_select_session(self, app_id: str) -> bool:
        session = self._current_session()
        if session is None:
            return False
        if not app_id:
            return True
        return self._aumid(session) == app_id


# ---------------------------------------------------------------------------
# public controller
# ---------------------------------------------------------------------------

class MediaController:
    """Thread-safe facade over the Windows media session manager.

    Every method is safe to call from any thread, never raises, and returns a
    safe fallback (``False`` / ``None`` / ``ok: false``) instead of hanging or
    propagating an error.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = float(timeout)
        self._art_lock = threading.Lock()
        self._art_cache: "collections.OrderedDict[str, str]" = collections.OrderedDict()
        self._art_key: "str | None" = None
        self._sel_lock = threading.Lock()
        self._desired_app_id = ""
        self._worker = _Worker(self)
        self._worker.start()
        self._worker.wait_ready(5.0)

    # -- artwork cache (shared between the worker and callers) -------------

    def _cache_art(self, key: str, url: str) -> None:
        with self._art_lock:
            self._art_cache[key] = url
            self._art_cache.move_to_end(key)
            while len(self._art_cache) > max(ART_CACHE_MAX, 1):
                self._art_cache.popitem(last=False)

    def _set_art_key(self, key: "str | None") -> None:
        """Publish the art_key that matches the currently selected track."""
        with self._art_lock:
            self._art_key = key

    def _current_art_key(self) -> "str | None":
        with self._art_lock:
            return self._art_key

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
        with self._art_lock:
            state["art_key"] = None
        return state

    # -- contract surface --------------------------------------------------

    def get_state(self) -> dict:
        """Current media state in the exact CONTRACT.md shape.  Never raises."""
        try:
            return self._call(self._worker.snapshot)
        except Exception as exc:
            return self._fail_state(_short_error(exc))

    def get_art(self, art_key: str) -> "str | None":
        """'data:image/png;base64,...' for a key previously reported, else None."""
        try:
            if not isinstance(art_key, str) or not art_key:
                return None
            with self._art_lock:
                url = self._art_cache.get(art_key)
                if url is not None:
                    self._art_cache.move_to_end(art_key)
            return url
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
            return bool(self._call(lambda: self._worker._set_volume_async(level)))
        except Exception:
            return False

    def toggle_mute(self) -> bool:
        """Toggle system mute.  Returns True when the toggle call succeeded."""
        try:
            return bool(self._call(self._worker._mute_toggle_async))
        except Exception:
            return False

    def select_session(self, app_id: str) -> bool:
        """'' follows the system's current session; otherwise pick by AUMID."""
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
    """`python media.py` prints one get_state() snapshot (handy smoke test)."""
    controller = MediaController()
    try:
        print(json.dumps(controller.get_state(), indent=2))
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
