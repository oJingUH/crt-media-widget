#!/usr/bin/env python3
"""media_linux_selftest.py - proof that the Linux media layer maps MPRIS correctly.

Runs on WINDOWS with the project interpreter:

    .venv/Scripts/python.exe linux/media_linux_selftest.py

There is no Linux, no WSL, no Docker and no session D-Bus on the machine this
was written on, so NOTHING here touches a real MPRIS player or a real bus.
Instead it drives ``linux/media_linux.py`` against:

* a **fake transport** implementing the same async duck-typed surface the real
  ``MprisTransport`` implements (``connect``/``close``/``list_names``/
  ``read_player``/``invoke``), so the mapping logic, the fallback behaviour and
  every command route can be exercised deterministically; and
* a **stateful fake audio sink** (level + muted) behind an injected command
  runner, so ``wpctl``/``pactl`` parsing AND the volume/mute round-trip are
  proven without PipeWire or PulseAudio.

``dbus_fast`` is imported lazily inside the real transport, which is what makes
this possible: it is asserted absent from ``sys.modules`` after importing the
module under test.

One JSON report is printed on stdout; the exit code is 0 when every hard check
passed.  Every claim in the report is backed by a value measured here.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import pathlib
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (HERE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import media_linux as M  # noqa: E402  (path set up above)

SCRATCH = os.environ.get("TMPDIR") or os.environ.get("TEMP") or HERE

STATE_KEYS = tuple(M.STATE_KEYS)
SESSION_KEYS = tuple(M.SESSION_KEYS)
STATUSES = {"playing", "paused", "stopped", "closed", "unknown"}
HEX = set("0123456789abcdef")
TIMING_ITERATIONS = 25
SPOTIFY = "org.mpris.MediaPlayer2.spotify"
VLC = "org.mpris.MediaPlayer2.vlc"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    """Stand-in for MprisTransport: same async surface, canned players."""

    def __init__(self, players=None, connect_error=None, list_error=None,
                 list_delay=0.0, read_delay=None):
        self.players = dict(players or {})
        self.connect_error = connect_error
        self.list_error = list_error
        self.list_delay = list_delay
        self.read_delay = dict(read_delay or {})   # bus name -> seconds
        self.calls = []                            # (name, member, args, signature)
        self.connects = 0
        self.closes = 0
        self.list_calls = 0
        self.read_calls = []                       # bus names, in order

    async def connect(self):
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def close(self):
        self.closes += 1

    async def list_names(self, timeout=None):
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        if self.list_delay:
            import asyncio
            await asyncio.sleep(self.list_delay)
        return list(self.players)

    async def read_player(self, bus_name, timeout=None):
        self.read_calls.append(bus_name)
        if bus_name in self.read_delay:
            import asyncio
            await asyncio.sleep(self.read_delay[bus_name])
        if bus_name not in self.players:
            raise M.ServiceUnknownError("%s has no owner" % bus_name)
        value = self.players[bus_name]
        if isinstance(value, BaseException):
            raise value
        return dict(value)

    async def invoke(self, bus_name, member, *args, signature="", timeout=None):
        self.calls.append((bus_name, member, tuple(args), signature))
        if bus_name not in self.players:
            raise M.ServiceUnknownError("%s has no owner" % bus_name)
        value = self.players[bus_name]
        if isinstance(value, BaseException):
            raise value
        return None


class NullVolume:
    """Injected volume backend that always reports a known, boring state."""

    def __init__(self, level=0.0, muted=False):
        self.level = level
        self.muted = muted
        self.sets = []
        self.toggles = 0

    def read(self):
        return (self.level, self.muted)

    def set(self, level):
        self.sets.append(level)
        self.level = max(0.0, min(1.0, float(level)))
        return True

    def toggle_mute(self):
        self.toggles += 1
        self.muted = not self.muted
        return True


class FakeSink:
    """A stateful default-sink the way wpctl / pactl would report it."""

    def __init__(self, level=0.62, muted=False, backend="wpctl"):
        self.level = float(level)
        self.muted = bool(muted)
        self.backend = backend
        self.calls = []

    def which(self, name):
        if name == "wpctl":
            return "/usr/bin/wpctl" if self.backend == "wpctl" else None
        if name == "pactl":
            return "/usr/bin/pactl" if self.backend == "pactl" else None
        return None

    def run(self, cmd):
        self.calls.append(list(cmd))
        program = cmd[0]
        verb = cmd[1] if len(cmd) > 1 else ""
        if program == "wpctl":
            if verb == "get-volume":
                text = "Volume: %.2f%s\n" % (self.level, " [MUTED]" if self.muted else "")
                return (0, text, "")
            if verb == "set-volume":
                self.level = max(0.0, min(1.0, float(cmd[3])))
                return (0, "", "")
            if verb == "set-mute":
                self.muted = not self.muted
                return (0, "", "")
        if program == "pactl":
            if verb == "get-sink-volume":
                return (0, "Volume: front-left: 65536 / %d%% / 0.00 dB\n"
                        % int(round(self.level * 100)), "")
            if verb == "get-sink-mute":
                return (0, "Mute: %s\n" % ("yes" if self.muted else "no"), "")
            if verb == "set-sink-volume":
                self.level = int(cmd[3].rstrip("%")) / 100.0
                return (0, "", "")
            if verb == "set-sink-mute":
                self.muted = not self.muted
                return (0, "", "")
        return (1, "", "unknown command %r" % (cmd,))


def static_runner(responses):
    """A command runner that answers from a fixed table (never stateful)."""
    def run(cmd):
        for prefix, reply in responses:
            if tuple(cmd[: len(prefix)]) == tuple(prefix):
                return reply
        return (1, "", "no canned reply for %r" % (cmd,))
    return run


def player(identity="Spotify", title="Voulez-Vous", artist=("ABBA",),
           album="Voulez-Vous", art_url=None, length_us=297_892_000,
           trackid="/org/mpris/MediaPlayer2/TrackList/1", status="Playing",
           position_us=212_036_000, can_seek=True, can_next=True,
           can_previous=True):
    """Build a normalised raw-player dict the way a real transport would."""
    return {
        "identity": identity,
        "title": title,
        "artist": list(artist) if isinstance(artist, (list, tuple)) else artist,
        "album": album,
        "art_url": art_url,
        "length_us": length_us,
        "trackid": trackid,
        "status": status,
        "position_us": position_us,
        "can_seek": can_seek,
        "can_next": can_next,
        "can_previous": can_previous,
    }


# ---------------------------------------------------------------------------
# contract-shape validation (same rules as media_selftest.py)
# ---------------------------------------------------------------------------

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_state(state, where: str):
    dev = []

    def bad(msg):
        dev.append("%s: %s" % (where, msg))

    if not isinstance(state, dict):
        return ["%s: not a dict (%s)" % (where, type(state).__name__)]
    for key in sorted(set(STATE_KEYS) - set(state)):
        bad("missing key %r" % key)
    for key in sorted(set(state) - set(STATE_KEYS)):
        bad("extra key %r" % key)
    for key in ("ok", "has_session", "can_seek", "can_next", "can_previous", "muted"):
        if not isinstance(state.get(key), bool):
            bad("%s is %s, expected bool" % (key, type(state.get(key)).__name__))
    for key in ("app_id", "app_name", "title", "artist", "album", "error"):
        value = state.get(key)
        if value is not None and not isinstance(value, str):
            bad("%s is %s, expected str or None" % (key, type(value).__name__))
    if state.get("status") not in STATUSES:
        bad("status %r not one of %s" % (state.get("status"), sorted(STATUSES)))
    for key in ("position", "duration"):
        value = state.get(key)
        if value is None:
            continue
        if not _is_number(value):
            bad("%s is %s, expected float or None" % (key, type(value).__name__))
        elif value != value or value in (float("inf"), float("-inf")):
            bad("%s is not finite" % key)
        elif value < -0.001:
            bad("%s is negative (%r) - sentinel not clamped" % (key, value))
    art_key = state.get("art_key")
    if art_key is not None:
        if not isinstance(art_key, str) or len(art_key) != 8 or not set(art_key) <= HEX:
            bad("art_key %r is not 8 lowercase hex chars" % (art_key,))
    volume = state.get("volume")
    if not _is_number(volume):
        bad("volume is %s, expected float" % type(volume).__name__)
    elif not 0.0 <= volume <= 1.0:
        bad("volume %r outside 0.0..1.0" % (volume,))
    if not _is_number(state.get("t_ms")):
        bad("t_ms is %s, expected float" % type(state.get("t_ms")).__name__)
    sessions = state.get("sessions")
    if not isinstance(sessions, list):
        bad("sessions is %s, expected list" % type(sessions).__name__)
    else:
        if len(sessions) > 8:
            bad("sessions has %d entries, cap is 8" % len(sessions))
        for index, row in enumerate(sessions):
            tag = "sessions[%d]" % index
            if not isinstance(row, dict):
                bad("%s is not a dict" % tag)
                continue
            if tuple(row.keys()) != SESSION_KEYS:
                bad("%s keys are %s, expected %s" % (tag, list(row), list(SESSION_KEYS)))
            for key in ("app_id", "app_name", "title", "artist"):
                value = row.get(key)
                if value is not None and not isinstance(value, str):
                    bad("%s.%s is not str or None" % (tag, key))
            if row.get("status") not in STATUSES:
                bad("%s.status %r invalid" % (tag, row.get("status")))
    if state.get("ok") is False and not (isinstance(state.get("error"), str) and state["error"]):
        bad("ok is False but error is not a non-empty string")
    if state.get("ok") is True and state.get("error") is not None:
        bad("ok is True but error is %r" % (state.get("error"),))
    return dev


def controller(transport, volume=None, **kwargs):
    return M.MediaController(transport=transport, volume=volume or NullVolume(), **kwargs)


def brief(state):
    if not isinstance(state, dict):
        return None
    return {key: state.get(key) for key in (
        "ok", "has_session", "app_id", "app_name", "title", "artist", "status",
        "position", "duration", "art_key", "volume", "muted", "t_ms", "error",
    )}


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def check_imports_and_keys(report, failures):
    section = report["checks"]["imports_and_keys"] = {}
    section["media_linux_imported"] = True
    section["dbus_fast_imported_lazily"] = "dbus_fast" not in sys.modules
    if not section["dbus_fast_imported_lazily"]:
        failures.append("dbus_fast was imported at module import time")

    ok, message = M.bus_ok()
    section["bus_ok_on_windows"] = {"ok": ok, "message": message}
    section["bus_ok_is_boolean_tuple"] = isinstance(ok, bool) and isinstance(message, str)
    if not section["bus_ok_is_boolean_tuple"]:
        failures.append("bus_ok() did not return (bool, str)")

    # The two platforms must not drift: compare the key tuples against media.py.
    try:
        import media as win_media  # read-only import of the Windows layer
        section["media_py_imported"] = True
        section["state_keys_match_media_py"] = tuple(win_media.STATE_KEYS) == STATE_KEYS
        section["session_keys_match_media_py"] = tuple(win_media.SESSION_KEYS) == SESSION_KEYS
    except Exception as exc:
        section["media_py_imported"] = False
        section["media_py_import_error"] = "%s: %s" % (type(exc).__name__, exc)
        section["state_keys_match_media_py"] = None
        section["session_keys_match_media_py"] = None
    section["state_keys"] = list(STATE_KEYS)
    section["session_keys"] = list(SESSION_KEYS)
    section["state_key_count"] = len(STATE_KEYS)
    if len(STATE_KEYS) != 19:
        failures.append("STATE_KEYS has %d entries, contract says 19" % len(STATE_KEYS))
    if section.get("state_keys_match_media_py") is False:
        failures.append("STATE_KEYS differ from media.py")
    if section.get("session_keys_match_media_py") is False:
        failures.append("SESSION_KEYS differ from media.py")


def check_conversions(report, failures):
    """Microsecond -> second conversion, for position and for duration."""
    section = report["checks"]["conversions"] = {}
    cases = [
        ("position_spotify", "position_us -> seconds", M._us_to_seconds(212_036_000), 212.036),
        ("duration_spotify", "length_us -> seconds", M._sane_duration(297_892_000), 297.892),
        ("position_zero", "0 us -> 0.0 s", M._us_to_seconds(0), 0.0),
        ("position_absent", "None -> None", M._us_to_seconds(None), None),
        ("duration_zero", "0 us duration -> None (no timeline)", M._sane_duration(0), None),
        ("duration_absent", "None duration -> None", M._sane_duration(None), None),
        ("duration_negative", "-1 s -> None (sentinel clamped)", M._sane_duration(-1_000_000), None),
        ("duration_absurd", "2 days -> None (> MAX_SANE_DURATION)",
         M._sane_duration(2 * 86_400 * 1_000_000), None),
        ("duration_short", "1.5 s -> 1.5", M._sane_duration(1_500_000), 1.5),
        ("position_float_us", "1500000 us -> 1.5 s", M._us_to_seconds(1_500_000), 1.5),
    ]
    rows = []
    for name, description, got, expected in cases:
        passed = got == expected
        rows.append({"case": name, "what": description,
                     "got": got, "expected": expected, "pass": passed})
        if not passed:
            failures.append("conversion %s: got %r, expected %r" % (name, got, expected))
    section["cases"] = rows
    section["all_pass"] = all(row["pass"] for row in rows)


def check_helpers(report, failures):
    section = report["checks"]["helpers"] = {}

    artist_cases = [
        (["ABBA"], "ABBA"),
        (["ABBA", "Bjorn Ulvaeus"], "ABBA, Bjorn Ulvaeus"),
        ("ABBA", "ABBA"),
        ([], None),
        (None, None),
        (["  "], None),
        (("A", "B", "C"), "A, B, C"),
    ]
    rows = []
    for value, expected in artist_cases:
        got = M.join_artists(value)
        rows.append({"artist_value": value if not isinstance(value, tuple) else list(value),
                     "got": got, "expected": expected, "pass": got == expected})
        if got != expected:
            failures.append("join_artists(%r) = %r, expected %r" % (value, got, expected))
    section["artist_list_join"] = rows

    status_cases = [
        ("Playing", "playing"), ("Paused", "paused"), ("Stopped", "stopped"),
        ("playing", "playing"), ("Closed", "closed"), (None, "unknown"),
        ("", "unknown"), (42, "unknown"), ("Buffering", "unknown"),
    ]
    rows = []
    for value, expected in status_cases:
        got = M.map_status(value)
        rows.append({"playback_status": value, "got": got,
                     "expected": expected, "pass": got == expected})
        if got != expected:
            failures.append("map_status(%r) = %r, expected %r" % (value, got, expected))
    section["status_mapping"] = rows

    key_a = M.art_key_from_url("file:///music/a.png")
    key_a2 = M.art_key_from_url("file:///music/a.png")
    key_b = M.art_key_from_url("file:///music/b.png")
    rows = {
        "same_url_same_key": key_a == key_a2,
        "url_change_changes_key": key_a != key_b,
        "none_url_is_none": M.art_key_from_url(None) is None,
        "empty_url_is_none": M.art_key_from_url("") is None,
        "key_a": key_a,
        "key_b": key_b,
        "shape_is_8_hex": isinstance(key_a, str) and len(key_a) == 8 and set(key_a) <= HEX,
    }
    section["art_key_stability"] = rows
    for name in ("same_url_same_key", "url_change_changes_key", "none_url_is_none",
                 "empty_url_is_none", "shape_is_8_hex"):
        if not rows[name]:
            failures.append("art_key check %s failed" % name)

    app = {
        "spotify": M.app_name_from_bus("org.mpris.MediaPlayer2.spotify"),
        "vlc": M.app_name_from_bus("org.mpris.MediaPlayer2.vlc"),
        "chromium_instance": M.app_name_from_bus(
            "org.mpris.MediaPlayer2.chromium.instance1234"),
        "empty": M.app_name_from_bus(""),
    }
    section["app_name_from_bus"] = app
    if app["spotify"] != "Spotify" or app["vlc"] != "VLC":
        failures.append("app_name_from_bus override map misbehaved: %r" % app)

    owned = M.keep_owned(
        ["a", "b", "c", "d"],
        [True, False, TimeoutError("owner lookup timed out"), RuntimeError("bus hiccup")],
    )
    section["keep_owned_filter"] = {
        "kept": owned,
        "drops_only_explicitly_ownerless": owned == ["a", "c", "d"],
    }
    if not section["keep_owned_filter"]["drops_only_explicitly_ownerless"]:
        failures.append("keep_owned() did not fail open: %r" % owned)

    path_round_trip = None
    try:
        probe_path = os.path.join(SCRATCH, "retro-art-probe.png")
        url = pathlib.Path(probe_path).as_uri()
        path_round_trip = {
            "url": url,
            "resolved": M._file_url_to_path(url),
            "matches": os.path.normcase(os.path.abspath(M._file_url_to_path(url)))
            == os.path.normcase(os.path.abspath(probe_path)),
        }
    except Exception as exc:
        path_round_trip = {"error": "%s: %s" % (type(exc).__name__, exc)}
    section["file_url_round_trip"] = path_round_trip
    if path_round_trip and path_round_trip.get("matches") is False:
        failures.append("file:// URL did not round-trip to the same path")


def check_snapshot_mapping(report, failures):
    """The controller's state object from a fake MPRIS player."""
    section = report["checks"]["snapshot_mapping"] = {}
    transport = FakeTransport({
        SPOTIFY: player(artist=["ABBA", "Frida"], art_url="file:///music/cover.png",
                        can_seek=True, can_next=True, can_previous=True),
        VLC: player(identity="VLC", title="Idle", artist="Nobody",
                    status="Paused", length_us=0, position_us=None,
                    can_seek=False, can_next=False, can_previous=False),
    })
    control = controller(transport, NullVolume(level=0.41, muted=True))
    try:
        state = control.get_state()
        section["state"] = state
        section["key_order_matches_contract"] = tuple(state.keys()) == STATE_KEYS
        section["shape_deviations"] = validate_state(state, "fake snapshot")
        section["shape_clean"] = not section["shape_deviations"]
        section["picked_playing_player"] = state["app_id"] == SPOTIFY
        section["artist_joined" ] = state["artist"] == "ABBA, Frida"
        section["position_seconds"] = state["position"]
        section["duration_seconds"] = state["duration"]
        section["position_is_float"] = isinstance(state["position"], float)
        section["duration_is_float"] = isinstance(state["duration"], float)
        section["volume_from_sink_not_mpris"] = (
            state["volume"] == 0.41 and state["muted"] is True
        )
        # A second, deliberately different player supplies its own row.
        # (state["sessions"] above IS the row list; not echoed twice.)
        rows = {row["app_id"]: row for row in state["sessions"]}
        section["vlc_row_shape_clean"] = tuple(rows.get(VLC, {}).keys()) == SESSION_KEYS
        section["vlc_zero_length_gives_null_duration"] = None  # filled below
    finally:
        control.shutdown()

    # A stopped/no-timeline player: null duration and can_seek false.
    transport2 = FakeTransport({
        VLC: player(identity="VLC", status="Stopped", length_us=0, position_us=0),
    })
    control2 = controller(transport2)
    try:
        state2 = control2.get_state()
        section["no_timeline_state"] = brief(state2)
        section["no_timeline_duration_is_null"] = state2["duration"] is None
        section["no_timeline_can_seek_false"] = state2["can_seek"] is False
        section["no_timeline_status"] = state2["status"]
        section["no_timeline_shape_clean"] = not validate_state(state2, "no timeline")
        section["vlc_zero_length_gives_null_duration"] = state2["duration"] is None
    finally:
        control2.shutdown()

    for name in ("key_order_matches_contract", "shape_clean", "picked_playing_player",
                 "artist_joined", "position_is_float", "duration_is_float",
                 "volume_from_sink_not_mpris", "no_timeline_duration_is_null",
                 "no_timeline_can_seek_false", "no_timeline_shape_clean"):
        if section.get(name) is not True:
            failures.append("snapshot mapping check %s failed" % name)
    if section.get("position_seconds") != 212.036 or section.get("duration_seconds") != 297.892:
        failures.append("controller did not convert microseconds to seconds")


def check_sessions(report, failures):
    """sessions list: ordering (most recently Playing first), cap and shape."""
    section = report["checks"]["sessions"] = {}
    names = ["org.mpris.MediaPlayer2.p%02d" % index for index in range(1, 11)]
    players = {}
    for index, name in enumerate(names):
        players[name] = player(identity=name.rsplit(".", 1)[-1],
                               title="track-%02d" % (index + 1),
                               status="Paused" if index else "Playing",
                               length_us=1_000_000, position_us=0)
    transport = FakeTransport(players)
    control = controller(transport)
    try:
        first = control.get_state()
        section["cap_enforced"] = len(first["sessions"]) == 8
        section["cap_count"] = len(first["sessions"])
        section["first_tick_order"] = [row["app_id"].rsplit(".", 1)[-1] for row in first["sessions"]]
        section["playing_first"] = first["sessions"][0]["app_id"] == names[0]
        section["shape_clean"] = not validate_state(first, "sessions tick 1")

        # p07 starts playing: it must now lead, with p01 (previously playing)
        # second.
        transport.players[names[6]] = player(identity="p07", status="Playing",
                                             length_us=1_000_000, position_us=0)
        transport.players[names[0]] = player(identity="p01", status="Paused",
                                             length_us=1_000_000, position_us=0)
        second = control.get_state()
        order = [row["app_id"].rsplit(".", 1)[-1] for row in second["sessions"]]
        section["second_tick_order"] = order
        section["most_recent_playing_first"] = order[:2] == ["p07", "p01"]
        section["cap_enforced_tick_2"] = len(second["sessions"]) == 8
        section["row_keys"] = list(second["sessions"][0].keys())
        section["selected_is_playing"] = second["app_id"] == names[6]
        section["shape_clean_tick_2"] = not validate_state(second, "sessions tick 2")
    finally:
        control.shutdown()

    for name in ("cap_enforced", "playing_first", "shape_clean",
                 "most_recent_playing_first", "cap_enforced_tick_2",
                 "selected_is_playing", "shape_clean_tick_2"):
        if section.get(name) is not True:
            failures.append("sessions check %s failed" % name)
    if section.get("row_keys") != list(SESSION_KEYS):
        failures.append("session row keys are not the contract keys")


def check_commands(report, failures):
    """play_pause / next / previous route to the selected MPRIS player."""
    section = report["checks"]["commands"] = {}
    transport = FakeTransport({
        SPOTIFY: player(status="Playing"),
        VLC: player(identity="VLC", status="Paused"),
    })
    control = controller(transport)
    try:
        returns = {
            "play_pause": control.play_pause(),
            "next_track": control.next_track(),
            "previous_track": control.previous_track(),
        }
        section["returns"] = returns
        section["calls"] = [
            {"bus_name": name, "member": member, "args": list(args), "signature": signature}
            for name, member, args, signature in transport.calls
        ]
        members = [member for _name, member, _args, _sig in transport.calls]
        section["members_called"] = members
        section["all_returned_true"] = all(returns.values())
        section["routed_to_selected_player"] = all(
            name == SPOTIFY for name, _m, _a, _s in transport.calls
        )
        section["member_sequence"] = members == ["PlayPause", "Next", "Previous"]
    finally:
        control.shutdown()

    # No players at all: every command returns False, nothing raises.
    empty = FakeTransport({})
    control2 = controller(empty)
    try:
        section["no_session_returns"] = {
            "play_pause": control2.play_pause(),
            "next_track": control2.next_track(),
            "previous_track": control2.previous_track(),
            "seek_fraction": control2.seek_fraction(0.5),
        }
        section["no_session_all_false"] = not any(section["no_session_returns"].values())
        section["no_session_calls"] = len(empty.calls)
    finally:
        control2.shutdown()

    for name in ("all_returned_true", "routed_to_selected_player", "member_sequence",
                 "no_session_all_false"):
        if section.get(name) is not True:
            failures.append("command routing check %s failed" % name)


def check_seek(report, failures):
    """seek_fraction: SetPosition with the CURRENT trackid, in microseconds."""
    section = report["checks"]["seek"] = {}
    old_track = "/org/mpris/MediaPlayer2/TrackList/1"
    new_track = "/org/mpris/MediaPlayer2/TrackList/9"
    transport = FakeTransport({SPOTIFY: player(trackid=old_track, length_us=297_892_000)})
    control = controller(transport)
    try:
        # The player moves to a new track between the poll and the seek: the
        # layer must re-read the track id immediately before SetPosition.
        transport.players[SPOTIFY]["trackid"] = new_track
        ok = control.seek_fraction(0.5)
        section["seek_fraction_return"] = ok
        section["calls"] = [
            {"bus_name": name, "member": member, "args": list(args), "signature": signature}
            for name, member, args, signature in transport.calls
        ]
        section["used_current_trackid"] = bool(
            transport.calls and transport.calls[-1][2][0] == new_track
        )
        section["stale_trackid_never_used"] = old_track not in [
            args[0] for _n, _m, args, _s in transport.calls if args
        ]
        section["position_microseconds"] = transport.calls[-1][2][1] if transport.calls else None
        section["expected_microseconds"] = int(round(0.5 * 297_892_000))
        section["microseconds_exact"] = (
            section["position_microseconds"] == section["expected_microseconds"]
        )
        section["signature"] = transport.calls[-1][3] if transport.calls else None
        section["signature_is_object_path_and_int64"] = section["signature"] == "ox"

        # Clamping: 2.0 and -1.0 both stay inside the track.
        transport.calls.clear()
        control.seek_fraction(2.0)
        high = transport.calls[-1][2][1] if transport.calls else None
        transport.calls.clear()
        control.seek_fraction(-1.0)
        low = transport.calls[-1][2][1] if transport.calls else None
        section["clamped_high_us"] = high
        section["clamped_low_us"] = low
        section["clamped_to_track_bounds"] = high == 297_892_000 and low == 0
    finally:
        control.shutdown()

    # No trackid: relative Seek(offset) fallback.
    transport2 = FakeTransport({SPOTIFY: player(trackid=None, length_us=100_000_000,
                                                position_us=20_000_000)})
    control2 = controller(transport2)
    try:
        ok = control2.seek_fraction(0.5)
        calls = [
            {"member": member, "args": list(args), "signature": signature}
            for _n, member, args, signature in transport2.calls
        ]
        section["fallback_relative_seek"] = {
            "return": ok, "calls": calls,
            "member_is_seek": bool(calls) and calls[-1]["member"] == "Seek",
            "offset_is_delta": bool(calls) and calls[-1]["args"] == [50_000_000 - 20_000_000],
            "signature": calls[-1]["signature"] if calls else None,
        }
    finally:
        control2.shutdown()

    # No duration: refuse (can_seek is false, so seeking is meaningless).
    transport3 = FakeTransport({SPOTIFY: player(length_us=0, trackid=None)})
    control3 = controller(transport3)
    try:
        section["no_duration_return"] = control3.seek_fraction(0.5)
        section["no_duration_no_call"] = len(transport3.calls) == 0
    finally:
        control3.shutdown()

    for name in ("used_current_trackid", "stale_trackid_never_used", "microseconds_exact",
                 "signature_is_object_path_and_int64", "clamped_to_track_bounds"):
        if section.get(name) is not True:
            failures.append("seek check %s failed" % name)
    if not section.get("fallback_relative_seek", {}).get("member_is_seek"):
        failures.append("seek without a trackid did not fall back to Seek()")
    if not section.get("fallback_relative_seek", {}).get("offset_is_delta"):
        failures.append("seek fallback did not send a relative offset")
    if section.get("no_duration_return") is not False:
        failures.append("seek with no duration did not return False")


def check_selection(report, failures):
    """Pinning, the dead-pin fallback, and follow-current."""
    section = report["checks"]["selection"] = {}
    transport = FakeTransport({
        SPOTIFY: player(status="Playing", title="Spotify track"),
        VLC: player(identity="VLC", status="Paused", title="VLC track"),
    })
    control = controller(transport)
    try:
        section["follow_default"] = brief(control.get_state())

        pin_ok = control.select_session(VLC)
        pinned = control.get_state()
        section["pin_return"] = pin_ok
        section["pinned_app_id"] = pinned["app_id"]
        section["pin_takes_effect"] = pinned["app_id"] == VLC
        section["pinned_status"] = pinned["status"]

        # The pinned player disappears while another one is still live: the
        # widget must FALL BACK, not go blank (a bug fixed once in media.py).
        del transport.players[VLC]
        samples = []
        for _ in range(6):
            state = control.get_state()
            samples.append((bool(state["has_session"]), state["app_id"], state["status"]))
            time.sleep(0.05)
        section["dead_pin_polls"] = len(samples)
        section["dead_pin_distinct_samples"] = sorted(
            {"%s|%s|%s" % sample for sample in samples})
        section["dead_pin_never_blank"] = all(sample[0] for sample in samples)
        section["dead_pin_fell_back_to_live"] = all(sample[1] == SPOTIFY for sample in samples)
        closed_rows = [
            row for row in control.get_state()["sessions"] if row["status"] == "closed"
        ]
        section["vanished_row_reported_closed"] = any(
            row["app_id"] == VLC for row in closed_rows
        )
        section["closed_rows"] = closed_rows

        # select_session("") goes back to following the most recent player.
        section["follow_current_return"] = control.select_session("")
        followed = control.get_state()
        section["follow_current_app_id"] = followed["app_id"]
        section["follow_current_works"] = followed["app_id"] == SPOTIFY

        # The pinned player comes back: the pin re-takes over.
        transport.players[VLC] = player(identity="VLC", status="Playing",
                                        title="VLC track")
        control.select_session(VLC)
        came_back = control.get_state()
        section["pin_reattaches_app_id"] = came_back["app_id"]
        section["pin_reattaches"] = came_back["app_id"] == VLC

        # A pin that never existed also falls back instead of going blank.
        control.select_session("org.mpris.MediaPlayer2.nonexistent")
        bogus = control.get_state()
        section["bogus_pin_state"] = brief(bogus)
        section["bogus_pin_falls_back"] = (
            bogus["has_session"] is True and bogus["app_id"] in (SPOTIFY, VLC)
        )
    finally:
        control.shutdown()

    # A mid-tick ServiceUnknown on a still-listed player reads as "closed".
    dead = M.ServiceUnknownError("org.mpris.MediaPlayer2.spotify has no owner")
    transport2 = FakeTransport({
        SPOTIFY: dead,
        VLC: player(identity="VLC", status="Paused"),
    })
    control2 = controller(transport2)
    try:
        control2.select_session(SPOTIFY)
        state = control2.get_state()
        section["listed_but_dead_state"] = brief(state)
        section["listed_but_dead_status_closed"] = state["status"] == "closed"
        section["listed_but_dead_app_id"] = state["app_id"]
    finally:
        control2.shutdown()

    for name in ("pin_takes_effect", "dead_pin_never_blank", "dead_pin_fell_back_to_live",
                 "vanished_row_reported_closed", "follow_current_works",
                 "pin_reattaches", "bogus_pin_falls_back",
                 "listed_but_dead_status_closed"):
        if section.get(name) is not True:
            failures.append("selection check %s failed" % name)


def check_volume(report, failures):
    """wpctl / pactl parsing, round-trip through an injected fake sink."""
    section = report["checks"]["volume"] = {}

    # -- backend probing ---------------------------------------------------
    sink_wpctl = FakeSink(backend="wpctl")
    backend = M.VolumeBackend(runner=sink_wpctl.run, which=sink_wpctl.which)
    section["probe_wpctl_first"] = backend.probe()
    sink_pactl = FakeSink(backend="pactl")
    backend_p = M.VolumeBackend(runner=sink_pactl.run, which=sink_pactl.which)
    section["probe_pactl_when_no_wpctl"] = backend_p.probe()
    backend_none = M.VolumeBackend(runner=lambda cmd: (1, "", ""),
                                   which=lambda name: None)
    section["probe_none"] = backend_none.probe()
    section["real_volume_backend"] = M.volume_backend()
    section["real_volume_backend_allowed"] = M.volume_backend() in ("wpctl", "pactl", "none")
    if section["probe_wpctl_first"] != "wpctl":
        failures.append("wpctl was not preferred over pactl")
    if section["probe_pactl_when_no_wpctl"] != "pactl":
        failures.append("pactl was not used when wpctl is absent")
    if section["probe_none"] != "none":
        failures.append("missing backends did not probe as 'none'")

    # -- exact output parsing (the strings from the spec) ------------------
    parse_cases = {
        "wpctl_unmuted": (
            [(["wpctl", "get-volume"], (0, "Volume: 0.65\n", ""))], "wpctl", (0.65, False)),
        "wpctl_muted": (
            [(["wpctl", "get-volume"], (0, "Volume: 0.65 [MUTED]\n", ""))], "wpctl", (0.65, True)),
        "pactl_unmuted": (
            [(["pactl", "get-sink-volume"],
              (0, "Volume: front-left: 65536 / 75% / 0.00 dB,   front-right: 65536 / 75% / 0.00 dB\n", "")),
             (["pactl", "get-sink-mute"], (0, "Mute: no\n", ""))], "pactl", (0.75, False)),
        "pactl_muted": (
            [(["pactl", "get-sink-volume"],
              (0, "Volume: front-left: 32768 / 50% / 0.00 dB\n", "")),
             (["pactl", "get-sink-mute"], (0, "Mute: yes\n", ""))], "pactl", (0.50, True)),
        "wpctl_garbage": (
            [(["wpctl", "get-volume"], (0, "something else\n", ""))], "wpctl", None),
        "wpctl_nonzero_rc": (
            [(["wpctl", "get-volume"], (1, "Volume: 0.65\n", "boom"))], "wpctl", None),
    }
    rows = []
    for name, (responses, kind, expected) in parse_cases.items():
        which = (lambda n, k=kind: "/usr/bin/%s" % k if n == k else None)
        backend = M.VolumeBackend(runner=static_runner(responses), which=which)
        got = backend.read()
        rows.append({"case": name, "raw_output": responses[0][1][1].strip(),
                     "got": got, "expected": expected, "pass": got == expected})
        if got != expected:
            failures.append("volume parse %s: got %r, expected %r" % (name, got, expected))
    section["parsing"] = rows

    # -- round-trip through the controller ---------------------------------
    sink = FakeSink(level=0.62, muted=False, backend="wpctl")
    backend = M.VolumeBackend(runner=sink.run, which=sink.which)
    transport = FakeTransport({SPOTIFY: player()})
    control = controller(transport, volume=backend)
    try:
        before = control.get_state()
        section["volume_before"] = {"volume": before["volume"], "muted": before["muted"]}
        set_ok = control.set_volume(0.40)
        after = control.get_state()
        section["set_volume_return"] = set_ok
        section["volume_after_set"] = after["volume"]
        section["volume_round_trip"] = abs(after["volume"] - 0.40) < 1e-9

        toggle_ok = control.toggle_mute()
        muted = control.get_state()
        section["toggle_mute_return"] = toggle_ok
        section["muted_after_toggle"] = muted["muted"]
        section["mute_round_trip"] = muted["muted"] is True
        control.toggle_mute()
        section["mute_round_trip_back"] = control.get_state()["muted"] is False

        section["commands_run"] = [" ".join(cmd) for cmd in sink.calls]
        section["set_volume_command"] = " ".join(sink.calls[1]) if len(sink.calls) > 1 else None
        section["used_wpctl_for_set"] = any(
            cmd[:2] == ["wpctl", "set-volume"] for cmd in sink.calls
        )
        section["used_wpctl_toggle_mute"] = any(
            cmd[:2] == ["wpctl", "set-mute"] and cmd[-1] == "toggle" for cmd in sink.calls
        )
        # MPRIS must never feed volume: the fake player advertises none at all.
        section["mpris_volume_never_used"] = not any(
            name == SPOTIFY and member == "Volume" for name, member, _a, _s in transport.calls
        )
    finally:
        control.shutdown()

    # pactl round-trip through the controller as well.
    sink_p = FakeSink(level=0.30, muted=True, backend="pactl")
    backend_p = M.VolumeBackend(runner=sink_p.run, which=sink_p.which)
    control_p = controller(FakeTransport({SPOTIFY: player()}), volume=backend_p)
    try:
        state = control_p.get_state()
        section["pactl_read"] = {"volume": state["volume"], "muted": state["muted"]}
        control_p.set_volume(0.85)
        after = control_p.get_state()
        section["pactl_after_set"] = after["volume"]
        section["pactl_commands"] = [" ".join(cmd) for cmd in sink_p.calls]
        section["pactl_round_trip"] = (
            abs(state["volume"] - 0.30) < 1e-9 and state["muted"] is True
            and abs(after["volume"] - 0.85) < 1e-2
        )
    finally:
        control_p.shutdown()

    # A hostile runner: the layer must fall back to its last known value.
    def explode(_cmd):
        raise OSError("no such binary")

    backend_x = M.VolumeBackend(runner=explode, which=lambda name: "/usr/bin/" + name)
    control_x = controller(FakeTransport({SPOTIFY: player()}), volume=backend_x)
    try:
        state = control_x.get_state()
        section["hostile_runner_state"] = {"ok": state["ok"], "volume": state["volume"],
                                           "muted": state["muted"]}
        section["hostile_runner_survives"] = (
            state["ok"] is True and state["volume"] == 0.0 and state["muted"] is False
        )
        section["hostile_set_returns"] = control_x.set_volume(0.5)
        section["hostile_mute_returns"] = control_x.toggle_mute()
    finally:
        control_x.shutdown()

    for name in ("volume_round_trip", "mute_round_trip", "mute_round_trip_back",
                 "used_wpctl_for_set", "used_wpctl_toggle_mute",
                 "mpris_volume_never_used", "pactl_round_trip", "hostile_runner_survives"):
        if section.get(name) is not True:
            failures.append("volume check %s failed" % name)
    if section.get("hostile_set_returns") is not False:
        failures.append("set_volume with a broken runner did not return False")


def check_failures(report, failures):
    """The layer must never raise, and must degrade honestly."""
    section = report["checks"]["failure_handling"] = {}

    def safe(call, tag):
        try:
            return {"raised": False, "value": call()}
        except BaseException as exc:  # noqa: BLE001 - a raise here IS the failure
            section.setdefault("raises", []).append("%s: %s: %s" % (tag, type(exc).__name__, exc))
            return {"raised": True, "error": "%s: %s" % (type(exc).__name__, exc)}

    # connect() failing.
    t1 = FakeTransport({}, connect_error=RuntimeError("no session bus"))
    c1 = controller(t1)
    try:
        r = safe(c1.get_state, "connect failure get_state")
        section["connect_failure"] = {"ok": r["value"].get("ok"),
                                      "error": r["value"].get("error")}
        section["connect_failure_ok_false"] = r["value"].get("ok") is False
        section["connect_failure_shape_clean"] = not validate_state(
            r["value"], "connect failure")
        section["connect_failure_commands_return_false"] = not any([
            c1.play_pause(), c1.next_track(), c1.previous_track(),
            c1.seek_fraction(0.5), c1.select_session("x"),
        ])
    finally:
        c1.shutdown()

    # list_names() failing.
    t2 = FakeTransport({SPOTIFY: player()}, list_error=RuntimeError("bus exploded"))
    c2 = controller(t2)
    try:
        r = safe(c2.get_state, "list failure get_state")
        section["list_failure"] = {"ok": r["value"].get("ok"), "error": r["value"].get("error"),
                                   "volume": r["value"].get("volume")}
        section["list_failure_ok_false"] = r["value"].get("ok") is False
    finally:
        c2.shutdown()

    # A single player throwing a non-MPRIS error: the tick still succeeds.
    t3 = FakeTransport({
        SPOTIFY: player(status="Playing"),
        VLC: OSError("lp0 on fire"),
    })
    c3 = controller(t3)
    try:
        state = c3.get_state()
        rows = {row["app_id"]: row for row in state["sessions"]}
        section["broken_player"] = {
            "ok": state["ok"], "status_of_broken": rows[VLC]["status"],
            "status_of_healthy": rows[SPOTIFY]["status"],
            "session_count": len(state["sessions"]),
        }
        section["broken_player_tick_ok"] = state["ok"] is True
        section["broken_player_unknown"] = rows[VLC]["status"] == "unknown"
        section["broken_player_shape_clean"] = not validate_state(state, "broken player")
    finally:
        c3.shutdown()

    # A single player that HANGS: it reads as unknown, the tick still lands.
    t4 = FakeTransport({
        SPOTIFY: player(status="Playing"),
        VLC: player(identity="VLC", status="Playing"),
    }, read_delay={VLC: 5.0})
    c4 = controller(t4)
    try:
        started = time.perf_counter()
        state = c4.get_state()
        elapsed = time.perf_counter() - started
        rows = {row["app_id"]: row for row in state["sessions"]}
        section["hung_player"] = {
            "ok": state["ok"], "elapsed_s": round(elapsed, 3),
            "hung_status": rows[VLC]["status"], "selected": state["app_id"],
        }
        section["hung_player_tick_under_1s"] = elapsed < 1.0
        section["hung_player_unknown"] = rows[VLC]["status"] == "unknown"
        section["hung_player_ok"] = state["ok"] is True
    finally:
        c4.shutdown()

    # A transport that hangs for many seconds: get_state must still return,
    # well inside the ~2 s contract.
    t5 = FakeTransport({SPOTIFY: player()}, list_delay=5.0)
    c5 = controller(t5)
    try:
        started = time.perf_counter()
        state = c5.get_state()
        elapsed = time.perf_counter() - started
        section["hanging_transport"] = {"ok": state["ok"], "error": state["error"],
                                        "elapsed_s": round(elapsed, 3)}
        section["hanging_transport_bounded"] = elapsed < 2.5
        section["hanging_transport_ok_false"] = state["ok"] is False
        section["hanging_transport_never_raises"] = section.get("raises", []) == []
    finally:
        c5.shutdown()

    # get_state() with garbage in every field must not raise either.
    t6 = FakeTransport({
        SPOTIFY: {
            "identity": 42, "title": {"weird": "dict"}, "artist": {"not": "a list"},
            "album": None, "art_url": None, "length_us": "not a number",
            "trackid": None, "status": 99, "position_us": float("nan"),
            "can_seek": "yes", "can_next": None, "can_previous": 1,
        },
    })
    c6 = controller(t6)
    try:
        r = safe(c6.get_state, "garbage metadata get_state")
        section["garbage_metadata"] = brief(r["value"])
        section["garbage_metadata_ok"] = r["value"].get("ok") is True
        section["garbage_metadata_shape_clean"] = not validate_state(
            r["value"], "garbage metadata")
    finally:
        c6.shutdown()

    for name in ("connect_failure_ok_false", "connect_failure_shape_clean",
                 "connect_failure_commands_return_false", "list_failure_ok_false",
                 "broken_player_tick_ok", "broken_player_unknown",
                 "broken_player_shape_clean", "hung_player_tick_under_1s",
                 "hung_player_unknown", "hung_player_ok", "hanging_transport_bounded",
                 "hanging_transport_ok_false", "hanging_transport_never_raises",
                 "garbage_metadata_ok", "garbage_metadata_shape_clean"):
        if section.get(name) is not True:
            failures.append("failure-handling check %s failed" % name)
    if section.get("raises"):
        failures.append("get_state() raised: %s" % "; ".join(section["raises"]))


def check_timing(report, failures):
    """Worker-thread marshalling: it must be fast and must never run on the caller."""
    section = report["checks"]["timing"] = {}
    transport = FakeTransport({SPOTIFY: player()})
    control = controller(transport)
    try:
        box = {"ms": [], "states": [], "thread_name": None, "is_main": None, "errors": []}

        def body():
            me = threading.current_thread()
            box["thread_name"] = me.name
            box["is_main"] = me is threading.main_thread()
            for _ in range(TIMING_ITERATIONS):
                started = time.perf_counter()
                try:
                    state = control.get_state()
                except BaseException as exc:  # noqa: BLE001
                    box["errors"].append("%s: %s" % (type(exc).__name__, exc))
                    state = None
                box["ms"].append((time.perf_counter() - started) * 1000.0)
                if isinstance(state, dict):
                    box["states"].append(state)

        thread = threading.Thread(target=body, name="selftest-timing", daemon=True)
        thread.start()
        thread.join(60)

        deviations = []
        for index, state in enumerate(box["states"]):
            deviations.extend(validate_state(state, "timing call %d" % (index + 1)))
        t_ms = [state["t_ms"] for state in box["states"]]
        section["iterations"] = TIMING_ITERATIONS
        section["calls_completed"] = len(box["ms"])
        section["caller_thread"] = box["thread_name"]
        section["caller_was_main_thread"] = box["is_main"]
        section["errors"] = box["errors"]
        section["min_ms"] = round(min(box["ms"]), 4)
        section["mean_ms"] = round(statistics.fmean(box["ms"]), 4)
        section["max_ms"] = round(max(box["ms"]), 4)
        section["p95_ms"] = round(sorted(box["ms"])[max(0, int(len(box["ms"]) * 0.95) - 1)], 4)
        section["budget_under_50ms"] = max(box["ms"]) < 50.0
        section["shape_deviations"] = deviations
        section["shape_clean"] = not deviations
        section["t_ms_strictly_increasing"] = all(
            b > a for a, b in zip(t_ms, t_ms[1:])
        )

        parallel = {"threads": 4, "calls_per_thread": 10, "ms": [], "errors": [],
                    "states": []}
        lock = threading.Lock()

        def body_parallel():
            for _ in range(parallel["calls_per_thread"]):
                started = time.perf_counter()
                try:
                    state = control.get_state()
                except BaseException as exc:  # noqa: BLE001
                    with lock:
                        parallel["errors"].append("%s: %s" % (type(exc).__name__, exc))
                    state = None
                elapsed = (time.perf_counter() - started) * 1000.0
                with lock:
                    parallel["ms"].append(elapsed)
                    if isinstance(state, dict):
                        parallel["states"].append(state)

        threads = [threading.Thread(target=body_parallel, name="selftest-par-%d" % i,
                                    daemon=True) for i in range(parallel["threads"])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        parallel_deviations = []
        for index, state in enumerate(parallel["states"]):
            parallel_deviations.extend(validate_state(state, "parallel call %d" % (index + 1)))
        section["parallel"] = {
            "threads": parallel["threads"],
            "calls_per_thread": parallel["calls_per_thread"],
            "calls_completed": len(parallel["ms"]),
            "errors": parallel["errors"],
            "max_ms": round(max(parallel["ms"]), 4) if parallel["ms"] else None,
            "mean_ms": round(statistics.fmean(parallel["ms"]), 4) if parallel["ms"] else None,
            "shape_clean": not parallel_deviations,
            "shape_deviations": parallel_deviations,
        }
        section["worker_thread_seen"] = transport.read_calls[:1]
    finally:
        control.shutdown()

    if section.get("caller_was_main_thread") is not False:
        failures.append("the timing loop ran on the main thread")
    if section.get("calls_completed") != TIMING_ITERATIONS:
        failures.append("not every marshalled get_state() completed")
    if section.get("errors"):
        failures.append("get_state() raised inside the timing loop")
    if not section.get("shape_clean"):
        failures.append("contract shape deviated under repeated calls")
    if not section.get("budget_under_50ms"):
        failures.append("get_state() exceeded the 50 ms budget (max %.2f ms)"
                        % section.get("max_ms", -1))
    if not section.get("t_ms_strictly_increasing"):
        failures.append("t_ms was not strictly increasing")
    if not section.get("parallel", {}).get("shape_clean"):
        failures.append("get_state() failed under concurrent callers")


def check_shutdown(report, failures):
    section = report["checks"]["shutdown"] = {}
    control = controller(FakeTransport({SPOTIFY: player()}))
    control.shutdown()
    control.shutdown()  # idempotent
    state = control.get_state()
    section["state_after_shutdown"] = brief(state)
    section["get_state_ok_false"] = state.get("ok") is False
    section["commands_false_after_shutdown"] = not any([
        control.play_pause(), control.next_track(), control.previous_track(),
        control.seek_fraction(0.5), control.set_volume(0.5), control.toggle_mute(),
    ])
    section["get_art_none_after_shutdown"] = control.get_art("deadbeef") is None
    for name in ("get_state_ok_false", "commands_false_after_shutdown",
                 "get_art_none_after_shutdown"):
        if section.get(name) is not True:
            failures.append("shutdown check %s failed" % name)


def check_art(report, failures):
    """get_art: file:// source, 160x160 downscale, data URL, LRU, unknown keys."""
    from PIL import Image

    section = report["checks"]["art"] = {}
    workdir = os.path.join(SCRATCH, "retro-art-%d" % os.getpid())
    os.makedirs(workdir, exist_ok=True)

    def write_png(name, size, colour):
        path = os.path.join(workdir, name)
        Image.new("RGB", size, colour).save(path, format="PNG")
        return path

    big = write_png("big.png", (400, 300), (200, 30, 90))
    big_uri = pathlib.Path(big).as_uri()
    section["source_file"] = {"path": big, "uri": big_uri, "size": list(Image.open(big).size)}

    fetched = M._fetch_url(big_uri)
    section["direct_fetch_bytes"] = len(fetched[0]) if fetched else None
    section["direct_fetch_mime"] = fetched[1] if fetched else None
    section["direct_fetch_from_file_url"] = bool(fetched and fetched[0])

    transport = FakeTransport({SPOTIFY: player(art_url=big_uri)})
    control = controller(transport)
    try:
        first = control.get_state()
        section["art_key"] = first["art_key"]
        section["art_key_present"] = isinstance(first["art_key"], str)
        data_url = control.get_art(first["art_key"])
        section["data_url_prefix"] = data_url[:30] if isinstance(data_url, str) else data_url
        section["is_data_url"] = bool(isinstance(data_url, str) and data_url.startswith("data:image/png;base64,"))
        if section["is_data_url"]:
            raw = base64.b64decode(data_url.split(",", 1)[1])
            section["decoded_bytes"] = len(raw)
            section["png_magic"] = raw[:8].hex()
            image = Image.open(io.BytesIO(raw))
            section["downscaled_size"] = list(image.size)
            section["within_160px"] = max(image.size) <= 160
            section["downscaled_from_source"] = list(image.size) != [400, 300]
        section["unknown_key_none"] = control.get_art("ffffffff") is None
        section["none_key_none"] = control.get_art(None) is None
        section["empty_key_none"] = control.get_art("") is None

        # Stability across polls, and a change when the track changes.
        again = control.get_state()
        section["art_key_stable_across_polls"] = again["art_key"] == first["art_key"]
        transport.players[SPOTIFY]["art_url"] = pathlib.Path(
            write_png("other.png", (64, 64), (10, 200, 120))).as_uri()
        changed = control.get_state()
        section["art_key_changes_on_new_track"] = changed["art_key"] != first["art_key"]
        section["new_art_key"] = changed["art_key"]
        section["new_art_resolves"] = control.get_art(changed["art_key"]) is not None

        # Cache: a second get_art must not re-fetch; the LRU holds >= 8.
        counted_uri = pathlib.Path(
            write_png("counted.png", (48, 48), (5, 60, 120))).as_uri()
        transport.players[SPOTIFY]["art_url"] = counted_uri
        counted = control.get_state()
        fetches = []
        real_fetch = control._fetcher

        def counting_fetch(url):
            fetches.append(url)
            return real_fetch(url)

        control._fetcher = counting_fetch
        key = counted["art_key"]
        section["counted_art_key"] = key
        first_data = control.get_art(key)
        control.get_art(key)
        control.get_art(key)
        section["fetch_count_for_three_calls"] = len(fetches)
        section["fetched_url"] = fetches[0] if fetches else None
        section["fetched_exactly_the_reported_url"] = fetches == [counted_uri]
        section["data_url_cached"] = (len(fetches) == 1 and first_data is not None)

        for index in range(10):
            transport.players[SPOTIFY]["art_url"] = pathlib.Path(
                write_png("lru-%02d.png" % index, (32, 32), (index * 20 % 255, 40, 90))
            ).as_uri()
            state = control.get_state()
            control.get_art(state["art_key"])
        with control._art_lock:
            section["art_url_cache_size"] = len(control._art_urls)
            section["art_data_cache_size"] = len(control._art_data)
            last_eight = list(control._art_urls.keys())[-8:]
        section["lru_keeps_at_least_8"] = (
            section["art_url_cache_size"] >= 8 and section["art_data_cache_size"] >= 8
        )
        section["last_eight_still_resolve"] = all(
            control.get_art(k) is not None for k in last_eight
        )
    finally:
        control.shutdown()

    # A missing file must yield None, not an exception.
    transport2 = FakeTransport({SPOTIFY: player(art_url=pathlib.Path(
        os.path.join(workdir, "does-not-exist.png")).as_uri())})
    control2 = controller(transport2)
    try:
        state = control2.get_state()
        section["missing_file_art_key"] = state["art_key"]
        section["missing_file_get_art_none"] = control2.get_art(state["art_key"]) is None
        section["missing_file_shape_clean"] = not validate_state(state, "missing art file")
    finally:
        control2.shutdown()

    try:
        for name in os.listdir(workdir):
            os.remove(os.path.join(workdir, name))
        os.rmdir(workdir)
        section["temp_files_cleaned"] = True
    except Exception as exc:
        section["temp_files_cleaned"] = "%s: %s" % (type(exc).__name__, exc)

    for name in ("art_key_present", "is_data_url", "within_160px", "downscaled_from_source",
                 "unknown_key_none", "none_key_none", "empty_key_none",
                 "art_key_stable_across_polls", "art_key_changes_on_new_track",
                 "new_art_resolves", "data_url_cached", "lru_keeps_at_least_8",
                 "fetched_exactly_the_reported_url",
                 "last_eight_still_resolve", "missing_file_get_art_none",
                 "missing_file_shape_clean", "direct_fetch_from_file_url"):
        if section.get(name) is not True:
            failures.append("art check %s failed" % name)


def check_probe(report, failures):
    section = report["checks"]["probe"] = {}
    result = M.probe()
    section["probe"] = result
    section["probe_keys"] = sorted(result.keys())
    section["probe_shape_ok"] = set(result) == {
        "bus_ok", "bus_address", "bus_message", "players", "player_count",
        "volume_backend", "error",
    }
    section["bus_ok"] = list(M.bus_ok())
    section["volume_backend"] = M.volume_backend()
    section["volume_backend_allowed"] = M.volume_backend() in ("wpctl", "pactl", "none")

    # bus_ok() must not explode when called from a thread that owns a loop.
    box: dict = {}

    def foreign_thread():
        import asyncio as _asyncio

        async def job():
            return M.bus_ok()

        try:
            box["value"] = _asyncio.run(job())
        except BaseException as exc:  # noqa: BLE001
            box["error"] = "%s: %s" % (type(exc).__name__, exc)

    thread = threading.Thread(target=foreign_thread, name="selftest-busok", daemon=True)
    thread.start()
    thread.join(10)
    section["bus_ok_from_running_loop"] = box
    section["bus_ok_from_running_loop_ok"] = "error" not in box

    if not section["probe_shape_ok"]:
        failures.append("probe() returned the wrong key set: %s" % section["probe_keys"])
    if not section["volume_backend_allowed"]:
        failures.append("volume_backend() returned %r" % section["volume_backend"])
    if not section["bus_ok_from_running_loop_ok"]:
        failures.append("bus_ok() raised when called from a running event loop: %s"
                        % box.get("error"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    report = {
        "tool": "linux/media_linux_selftest.py",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "WINDOWS + fake MPRIS transport + fake audio sink; no real D-Bus",
        "checks": {},
        "pass": False,
        "failures": [],
    }
    failures = report["failures"]
    started = time.monotonic()

    check_imports_and_keys(report, failures)
    check_conversions(report, failures)
    check_helpers(report, failures)
    check_snapshot_mapping(report, failures)
    check_sessions(report, failures)
    check_commands(report, failures)
    check_seek(report, failures)
    check_selection(report, failures)
    check_volume(report, failures)
    check_failures(report, failures)
    check_timing(report, failures)
    check_shutdown(report, failures)
    check_art(report, failures)
    check_probe(report, failures)

    report["checks"]["elapsed_s"] = round(time.monotonic() - started, 2)
    report["pass"] = not failures
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
