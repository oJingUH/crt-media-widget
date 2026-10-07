#!/usr/bin/env python3
"""media_selftest.py - headless proof that the media layer actually works.

Run it from the project root with the project interpreter (Windows)
or the portable bundle's own interpreter::

    .venv/Scripts/python.exe media_selftest.py

It prints one JSON report on stdout and exits 0 when every hard check passed.
Nothing is asserted from belief: timings are measured, the contract shape is
validated field by field, and the transport/volume/mute exercises record the
raw boolean each call returned plus the state read back afterwards.

If a media session is live, this briefly pauses, skips and seeks the user's
music and then restores the original playback status, position, volume and
mute state; the restore is reported, not assumed.

Two app behaviours the harness has to work around (both measured on this
machine, both properties of the apps rather than of media.py):

* the app pushes playback-status changes asynchronously, so a state read taken
  immediately after a command can still show the previous status;
* the app only pushes timeline updates while it is actually playing, so a seek
  is only observable - and therefore only provable - during playback.

Every post-command observation below therefore polls with a timeout instead of
reading once.
"""

from __future__ import annotations

import base64
import io
import json
import statistics
import sys
import threading
import time

import media as media_mod

STATE_KEYS = set(media_mod.STATE_KEYS)
SESSION_KEYS = set(media_mod.SESSION_KEYS)
STATUSES = {"playing", "paused", "stopped", "closed", "unknown"}
TIMING_ITERATIONS = 25          # contract asks for at least 20
HEX = set("0123456789abcdef")


# ---------------------------------------------------------------------------
# contract-shape validation
# ---------------------------------------------------------------------------

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_duration(value) -> bool:
    return _is_number(value) and value > 0


def validate_state(state, where: str):
    """Return a list of human-readable contract deviations for one state dict."""
    dev = []

    def bad(msg):
        dev.append("%s: %s" % (where, msg))

    if not isinstance(state, dict):
        return ["%s: not a dict (%s)" % (where, type(state).__name__)]

    for key in sorted(STATE_KEYS - set(state)):
        bad("missing key %r" % key)
    for key in sorted(set(state) - STATE_KEYS):
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
            if set(row) != SESSION_KEYS:
                bad("%s keys are %s, expected %s" % (tag, sorted(row), sorted(SESSION_KEYS)))
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def poll(controller, predicate, timeout=5.0, interval=0.15):
    """Poll get_state() until `predicate(state)` holds; return (state, secs, hit)."""
    started = time.monotonic()
    state = controller.get_state()
    if predicate(state):
        return state, 0.0, True
    while time.monotonic() - started < timeout:
        time.sleep(interval)
        state = controller.get_state()
        if predicate(state):
            return state, time.monotonic() - started, True
    return state, time.monotonic() - started, False


def poll_status(controller, wanted, timeout=4.0):
    return poll(controller, lambda s: s.get("status") == wanted, timeout=timeout)


def brief(state):
    """The fields worth quoting in the report."""
    if not isinstance(state, dict):
        return None
    return {
        "ok": state.get("ok"),
        "has_session": state.get("has_session"),
        "app_id": state.get("app_id"),
        "app_name": state.get("app_name"),
        "title": state.get("title"),
        "artist": state.get("artist"),
        "status": state.get("status"),
        "position": state.get("position"),
        "duration": state.get("duration"),
        "art_key": state.get("art_key"),
        "volume": state.get("volume"),
        "muted": state.get("muted"),
        "t_ms": state.get("t_ms"),
        "error": state.get("error"),
    }


# ---------------------------------------------------------------------------
# 2. threaded timing / marshalling proof
# ---------------------------------------------------------------------------

def run_timing(controller, iterations=TIMING_ITERATIONS):
    box = {"ms": [], "states": [], "errors": [], "thread_name": None, "is_main": None}

    def body():
        me = threading.current_thread()
        box["thread_name"] = me.name
        box["is_main"] = me is threading.main_thread()
        for _ in range(iterations):
            started = time.perf_counter()
            try:
                state = controller.get_state()
            except BaseException as exc:  # noqa: BLE001 - a raise here is a failure
                box["errors"].append("%s: %s" % (type(exc).__name__, exc))
                state = None
            box["ms"].append((time.perf_counter() - started) * 1000.0)
            if isinstance(state, dict):
                box["states"].append(state)

    thread = threading.Thread(target=body, name="selftest-timing", daemon=True)
    thread.start()
    thread.join(120)

    samples = box["ms"]
    deviations = []
    for index, state in enumerate(box["states"]):
        deviations.extend(validate_state(state, "timing call %d" % (index + 1)))

    t_ms = [state["t_ms"] for state in box["states"] if _is_number(state.get("t_ms"))]
    slowest = max(range(len(samples)), key=lambda i: samples[i]) if samples else None

    # Concurrent callers: the production shape is the UI's 600 ms poll plus
    # whatever thread a button callback arrives on, so hammer it in parallel.
    concurrent = {"threads": 4, "calls_per_thread": 10, "ms": [], "states": [], "errors": []}
    lock = threading.Lock()

    def body_parallel():
        for _ in range(concurrent["calls_per_thread"]):
            started = time.perf_counter()
            try:
                state = controller.get_state()
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    concurrent["errors"].append("%s: %s" % (type(exc).__name__, exc))
                state = None
            elapsed = (time.perf_counter() - started) * 1000.0
            with lock:
                concurrent["ms"].append(elapsed)
                if isinstance(state, dict):
                    concurrent["states"].append(state)

    threads = [
        threading.Thread(target=body_parallel, name="selftest-parallel-%d" % i, daemon=True)
        for i in range(concurrent["threads"])
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    parallel_deviations = []
    for index, state in enumerate(concurrent["states"]):
        parallel_deviations.extend(validate_state(state, "parallel call %d" % (index + 1)))

    report = {
        "iterations": iterations,
        "calls_completed": len(samples),
        "caller_thread": box["thread_name"],
        "caller_was_main_thread": box["is_main"],
        "errors": box["errors"],
        "min_ms": round(min(samples), 3) if samples else None,
        "mean_ms": round(statistics.fmean(samples), 3) if samples else None,
        "max_ms": round(max(samples), 3) if samples else None,
        "max_call_index": slowest,
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3) if samples else None,
        "all_samples_ms": [round(x, 3) for x in samples],
        "first_call_ms": round(samples[0], 3) if samples else None,
        "steady_state_min_ms": round(min(samples[1:]), 3) if len(samples) > 1 else None,
        "steady_state_mean_ms": round(statistics.fmean(samples[1:]), 3) if len(samples) > 1 else None,
        "steady_state_max_ms": round(max(samples[1:]), 3) if len(samples) > 1 else None,
        "budget_under_50ms": bool(samples) and max(samples) < 50.0,
        "t_ms_strictly_increasing": bool(len(t_ms) > 1 and all(b > a for a, b in zip(t_ms, t_ms[1:]))),
        "shape_deviations": deviations,
        "shape_clean": not deviations,
        "last_state": brief(box["states"][-1]) if box["states"] else None,
        "has_session_states": sum(1 for s in box["states"] if s.get("has_session")),
        "parallel": {
            "threads": concurrent["threads"],
            "calls_per_thread": concurrent["calls_per_thread"],
            "calls_completed": len(concurrent["ms"]),
            "errors": concurrent["errors"],
            "max_ms": round(max(concurrent["ms"]), 3) if concurrent["ms"] else None,
            "mean_ms": round(statistics.fmean(concurrent["ms"]), 3) if concurrent["ms"] else None,
            "shape_deviations": parallel_deviations,
            "shape_clean": not parallel_deviations,
        },
    }
    return report, (box["states"][-1] if box["states"] else None)


# ---------------------------------------------------------------------------
# 4-7. control / artwork / volume exercises with restore
# ---------------------------------------------------------------------------

def run_controls(controller, base_state):
    result = {
        "attempted": True,
        "original": brief(base_state),
        "returns": {},
        "seek": {},
        "artwork": {},
        "volume": {},
        "restore": {},
        "notes": [],
        "shape_deviations": [],
    }
    notes = result["notes"]

    def read(tag):
        state = controller.get_state()
        result["shape_deviations"].extend(validate_state(state, tag))
        return state

    original_status = base_state.get("status")
    original_title = base_state.get("title")
    original_artist = base_state.get("artist")
    original_position = base_state.get("position")
    original_volume = base_state.get("volume")
    original_muted = base_state.get("muted")

    # -- select_session: pin an unknown app; the widget must NOT go dark -----
    # Regression (fix 1): a pinned id that has no live session must fall back
    # to the manager's current session (then the newest live one), never
    # report has_session false while another session is still playing.
    result["select"] = {}
    target_aumid = base_state.get("app_id")
    live_ids = [row.get("app_id") for row in (base_state.get("sessions") or [])]
    result["select"]["live_sessions"] = live_ids
    result["select"]["target_aumid"] = target_aumid
    if target_aumid:
        bogus = "crt-selftest-nonexistent-app!Nope"
        result["select"]["select_bogus_return"] = controller.select_session(bogus)

        fell_back, _waited, hit = poll(
            controller, lambda s: s.get("has_session") is True, timeout=3.0
        )
        result["select"]["has_session_true_after_bogus"] = bool(hit)
        result["select"]["app_id_after_bogus"] = fell_back.get("app_id")

        # ... and it must stay live on every subsequent poll, not flicker dark.
        dark = 0
        samples = []
        until = time.monotonic() + 1.2
        while time.monotonic() < until:
            sampled = controller.get_state()
            samples.append(bool(sampled.get("has_session")))
            if not sampled.get("has_session"):
                dark += 1
            time.sleep(0.15)
        result["select"]["samples_after_bogus"] = samples
        result["select"]["dark_samples_after_bogus"] = dark
        result["select"]["sessions_still_enumerated"] = len(fell_back.get("sessions") or [])
        result["select"]["state_after_bogus"] = brief(fell_back)
        result["select"]["fallback_kept_real_live_app"] = bool(
            fell_back.get("app_id") is not None and fell_back.get("app_id") in live_ids
        )
        result["shape_deviations"].extend(validate_state(fell_back, "after bogus pin"))

        result["select"]["reselect_return"] = controller.select_session(target_aumid)
        reselected, _waited2, hit2 = poll(
            controller,
            lambda s: s.get("has_session") and s.get("app_id") == target_aumid,
            timeout=3.0,
        )
        result["select"]["has_session_true_after_reselect"] = bool(hit2)
        result["select"]["app_id_after_reselect"] = reselected.get("app_id")
    else:
        result["select"]["status"] = "SKIPPED"
        result["select"]["reason"] = "session with no AUMID"

    # -- artwork (fetch while the app is still on the first track) ---------
    art_key = base_state.get("art_key")
    if art_key is None:
        state, waited, hit = poll(controller, lambda s: s.get("art_key"), timeout=4.0)
        art_key = state.get("art_key")
        result["artwork"]["art_key_wait_s"] = round(waited, 2)
    if art_key:
        result["artwork"]["art_key"] = art_key
        data_url = controller.get_art(art_key)
        if isinstance(data_url, str):
            result["artwork"]["data_url_length"] = len(data_url)
            result["artwork"]["prefix"] = data_url[:22]
            try:
                payload = data_url.split(",", 1)[1]
                raw = base64.b64decode(payload)
                result["artwork"]["decoded_bytes"] = len(raw)
                result["artwork"]["png_magic"] = raw[:8].hex()
                try:
                    from PIL import Image  # noqa: PLC0415

                    image = Image.open(io.BytesIO(raw))
                    result["artwork"]["image_size"] = list(image.size)
                    result["artwork"]["image_mode"] = image.mode
                    result["artwork"]["within_160px"] = max(image.size) <= 160
                except Exception as exc:
                    result["artwork"]["decode_error"] = "%s: %s" % (type(exc).__name__, exc)
            except Exception as exc:
                result["artwork"]["decode_error"] = "%s: %s" % (type(exc).__name__, exc)
        else:
            result["artwork"]["data_url_length"] = None
            result["artwork"]["error"] = "get_art returned %r" % (data_url,)
        result["artwork"]["unknown_key_returns_none"] = controller.get_art("ffffffff") is None
        result["artwork"]["none_key_returns_none"] = controller.get_art(None) is None
        result["artwork"]["status"] = "exercised"
    else:
        result["artwork"]["status"] = "skipped"
        result["artwork"]["reason"] = "no art_key reported by the session (no artwork, or none yet)"

    # -- play_pause pulse: flip the status away and back -------------------
    expected_flip = "paused" if original_status == "playing" else "playing"
    pp1 = controller.play_pause()
    flipped, seconds1, hit1 = poll_status(controller, expected_flip, timeout=4.0)
    result["returns"]["play_pause_1"] = pp1
    result["returns"]["status_after_play_pause_1"] = flipped.get("status")
    result["returns"]["status_flipped"] = bool(hit1)
    result["returns"]["status_flip_wait_s"] = round(seconds1, 2)
    result["play_pause_1_state"] = brief(flipped)

    pp2 = controller.play_pause()
    back, seconds2, hit2 = poll_status(controller, original_status, timeout=4.0)
    result["returns"]["play_pause_2"] = pp2
    result["returns"]["status_after_play_pause_2"] = back.get("status")
    result["returns"]["status_returned_to_original"] = bool(hit2)
    result["returns"]["status_return_wait_s"] = round(seconds2, 2)

    # The app only pushes timeline updates while it is playing, so next /
    # previous / seek are exercised during playback.
    if back.get("status") != "playing":
        started_playback = controller.play_pause()
        back, _seconds3, hit3 = poll_status(controller, "playing", timeout=4.0)
        result["returns"]["play_pause_3_to_start_playback"] = started_playback
        result["returns"]["reached_playing"] = bool(hit3)
        if not hit3:
            notes.append(
                "could not start playback (status stayed %r), so the seek could not be "
                "verified" % (back.get("status"),)
            )

    # -- next / previous ---------------------------------------------------
    nxt = controller.next_track()
    after_next = read("after next_track")
    result["returns"]["next_track"] = nxt
    result["returns"]["title_after_next"] = after_next.get("title")

    prev = controller.previous_track()
    after_prev = read("after previous_track")
    result["returns"]["previous_track"] = prev
    result["returns"]["title_after_previous"] = after_prev.get("title")
    result["returns"]["title_unchanged_after_next_previous"] = (
        after_prev.get("title") == original_title
    )

    # -- seek: out, prove movement, then back ------------------------------
    seek_state = read("before seek")
    position_before = seek_state.get("position")
    duration = seek_state.get("duration")
    result["seek"]["status_when_exercised"] = seek_state.get("status")
    result["seek"]["position_before"] = position_before
    result["seek"]["duration"] = duration
    if _is_number(position_before) and _is_duration(duration):
        fraction_before = max(0.0, min(1.0, position_before / duration))
        result["seek"]["fraction_before"] = round(fraction_before, 4)
        target_fraction = 0.72 if fraction_before < 0.5 else 0.28
        target_position = target_fraction * duration
        result["seek"]["target_fraction"] = target_fraction
        result["seek"]["target_position"] = round(target_position, 3)
        tolerance = max(4.0, duration * 0.06)
        result["seek"]["tolerance_s"] = round(tolerance, 2)

        ok_seek = controller.seek_fraction(target_fraction)
        result["seek"]["seek_fraction_return"] = ok_seek
        moved, seconds, converged = poll(
            controller,
            lambda s: _is_number(s.get("position"))
            and abs(s["position"] - target_position) <= tolerance,
            timeout=6.0,
        )
        result["seek"]["position_after"] = moved.get("position")
        result["seek"]["moved_to_target"] = bool(converged)
        result["seek"]["converged_in_s"] = round(seconds, 2)

        ok_back = controller.seek_fraction(fraction_before)
        result["seek"]["seek_back_return"] = ok_back
        restored, seconds, converged = poll(
            controller,
            lambda s: _is_number(s.get("position"))
            and abs(s["position"] - position_before) <= tolerance,
            timeout=6.0,
        )
        result["seek"]["position_after_restore_seek"] = restored.get("position")
        result["seek"]["position_restored_by_seek"] = bool(converged)
        if not result["seek"]["moved_to_target"]:
            notes.append(
                "seek_fraction returned %r but the reported position did not reach the "
                "target within 6 s (the app may not push timeline updates while not "
                "playing)" % (ok_seek,)
            )
    else:
        result["seek"]["status"] = "SKIPPED"
        result["seek"]["reason"] = "session publishes no usable duration/position"
        notes.append("seek skipped: the app published no usable duration")

    # -- system volume -----------------------------------------------------
    current = read("before volume")
    level_now = current.get("volume")
    if _is_number(level_now) and _is_number(original_volume):
        probe_level = 0.50 if abs(level_now - 0.50) > 0.03 else (0.25 if level_now > 0.5 else 0.75)
        result["volume"]["original"] = original_volume
        result["volume"]["probe_requested"] = probe_level
        set_ok = controller.set_volume(probe_level)
        result["volume"]["set_volume_return"] = set_ok
        after_set = read("after set_volume")
        result["volume"]["read_back"] = after_set.get("volume")
        result["volume"]["read_back_matches"] = bool(
            _is_number(after_set.get("volume"))
            and abs(after_set["volume"] - probe_level) <= 0.02
        )
        restore_ok = controller.set_volume(original_volume)
        result["volume"]["set_volume_restore_return"] = restore_ok
        after_restore = read("after volume restore")
        result["volume"]["read_back_after_restore"] = after_restore.get("volume")
        result["volume"]["restored"] = bool(
            _is_number(after_restore.get("volume"))
            and abs(after_restore["volume"] - original_volume) <= 0.02
        )
    else:
        result["volume"]["status"] = "skipped"
        result["volume"]["reason"] = "no system endpoint volume available"

    # -- system mute -------------------------------------------------------
    mute_before = read("before mute")
    muted_before = mute_before.get("muted")
    result["volume"]["muted_original"] = muted_before
    if isinstance(muted_before, bool):
        toggle_ok = controller.toggle_mute()
        result["volume"]["toggle_mute_return"] = toggle_ok
        after_toggle = read("after toggle_mute")
        muted_after = after_toggle.get("muted")
        result["volume"]["muted_after_toggle"] = muted_after
        result["volume"]["mute_flipped"] = muted_after is not muted_before
        if muted_after != muted_before:
            undo_ok = controller.toggle_mute()
            result["volume"]["toggle_mute_restore_return"] = undo_ok
            settled, seconds, hit = poll(
                controller, lambda s: s.get("muted") == muted_before, timeout=2.0
            )
            result["volume"]["muted_after_restore"] = settled.get("muted")
            result["volume"]["mute_restored"] = bool(hit)
        else:
            result["volume"]["mute_restored"] = True
            notes.append("toggle_mute did not change the system mute state")
    else:
        result["volume"]["mute_restored"] = False
        notes.append("mute state was not readable as a bool")

    # -- play_pause: put the original playback status back -----------------
    now = read("before status restore")
    restore_toggle = None
    result["restore"]["status_before_restore"] = now.get("status")
    if now.get("status") != original_status:
        if {now.get("status"), original_status} <= {"playing", "paused"}:
            restore_toggle = controller.play_pause()
            settled, _seconds, hit = poll_status(controller, original_status, timeout=4.0)
            result["restore"]["status_after_restore"] = settled.get("status")
            result["restore"]["status_restored"] = bool(hit)
        else:
            result["restore"]["status_after_restore"] = now.get("status")
            result["restore"]["status_restored"] = False
            notes.append(
                "original status %r cannot be restored with play_pause from %r"
                % (original_status, now.get("status"))
            )
    else:
        result["restore"]["status_after_restore"] = now.get("status")
        result["restore"]["status_restored"] = True
    result["returns"]["play_pause_restore"] = restore_toggle

    # -- put the position back if the skips moved it -----------------------
    final = read("before position restore")
    position_final = final.get("position")
    result["restore"]["position_original"] = original_position
    result["restore"]["position_before_final_restore"] = position_final
    if (
        _is_number(position_final)
        and _is_number(original_position)
        and _is_duration(final.get("duration"))
        and abs(position_final - original_position) > 3.0
        and final.get("title") == original_title
    ):
        fraction = max(0.0, min(1.0, original_position / final["duration"]))
        controller.seek_fraction(fraction)
        settled, _seconds, hit = poll(
            controller,
            lambda s: _is_number(s.get("position"))
            and abs(s["position"] - original_position) <= max(4.0, final["duration"] * 0.06),
            timeout=6.0,
        )
        result["restore"]["position_after_final_restore"] = settled.get("position")
        result["restore"]["position_restored"] = bool(hit)
    else:
        result["restore"]["position_after_final_restore"] = position_final
        result["restore"]["position_restored"] = bool(
            _is_number(position_final)
            and _is_number(original_position)
            and abs(position_final - original_position) <= 3.0
        )

    tail = read("tail")
    result["restore"]["original_status"] = original_status
    result["restore"]["final_status"] = tail.get("status")
    result["restore"]["original_volume"] = original_volume
    result["restore"]["final_volume"] = tail.get("volume")
    result["restore"]["original_muted"] = original_muted
    result["restore"]["final_muted"] = tail.get("muted")
    result["restore"]["original_title"] = original_title
    result["restore"]["final_title"] = tail.get("title")
    result["restore"]["original_artist"] = original_artist
    result["restore"]["final_artist"] = tail.get("artist")
    result["restore"]["original_position"] = original_position
    result["restore"]["final_position"] = tail.get("position")
    result["restore"]["status_ok"] = tail.get("status") == original_status or (
        original_status not in ("playing", "paused")
    )
    result["restore"]["volume_ok"] = bool(
        _is_number(tail.get("volume"))
        and _is_number(original_volume)
        and abs(tail["volume"] - original_volume) <= 0.03
    )
    result["restore"]["mute_ok"] = tail.get("muted") == original_muted
    result["restore"]["same_track"] = tail.get("title") == original_title
    if not result["restore"]["same_track"]:
        notes.append(
            "the app moved to a different track during the test (%r -> %r): playback "
            "status, volume, position and mute were restored, but the track itself cannot "
            "be restored through GSMTC - skip_next/skip_previous are one-way"
            % (original_title, tail.get("title"))
        )

    # -- select_session(""): back to following the system's current session -
    result["select"]["follow_current_return"] = controller.select_session("")
    followed, _waited3, hit3 = poll(
        controller, lambda s: s.get("has_session") is True, timeout=3.0
    )
    result["select"]["follow_current_has_session"] = bool(hit3)
    result["select"]["follow_current_app_id"] = followed.get("app_id")

    result["final_state"] = brief(tail)
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    report = {
        "tool": "media_selftest.py",
        "python": sys.version.split()[0],
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checks": {},
        "timing": {},
        "session": {},
        "controls": {},
        "aumid_to_name": {},
        "notes": [],
    }
    notes = report["notes"]
    failures = []

    started = time.monotonic()
    controller = media_mod.MediaController()
    try:
        report["checks"]["controller_constructed"] = True

        # 2 + 3: threaded timing and shape validation
        timing, last_state = run_timing(controller)
        report["timing"] = timing

        base = controller.get_state()
        deviations = validate_state(base, "get_state()")
        report["checks"]["shape_deviations"] = deviations
        report["checks"]["shape_clean"] = not deviations
        report["session"] = brief(base)
        report["session"]["sessions_enumerated"] = base.get("sessions") or []
        report["session"]["session_count"] = len(base.get("sessions") or [])

        if not timing["caller_was_main_thread"]:
            report["checks"]["timing_ran_off_main_thread"] = True
        else:
            failures.append("timing loop ran on the main thread")

        if timing["calls_completed"] < 20:
            failures.append("fewer than 20 marshalled calls completed")
        if timing["errors"]:
            failures.append("get_state() raised inside the timing loop")
        if timing["parallel"]["errors"] or not timing["parallel"]["shape_clean"]:
            failures.append("get_state() failed under concurrent callers")
        if deviations:
            failures.append("contract shape deviations: %s" % "; ".join(deviations[:5]))
        if not timing["shape_clean"]:
            failures.append("contract shape deviations under repeated calls")
        if timing["max_ms"] is not None and timing["max_ms"] >= 50.0:
            failures.append("get_state() exceeded the 50 ms budget (max %.1f ms)" % timing["max_ms"])
        if base.get("ok") is not True:
            failures.append("get_state() returned ok=false: %r" % (base.get("error"),))

        # AUMID -> app_name mappings actually observed.
        for state in (base, last_state):
            for row in (state or {}).get("sessions") or []:
                if row.get("app_id"):
                    report["aumid_to_name"][row["app_id"]] = row.get("app_name")
        if base.get("app_id"):
            report["aumid_to_name"][base["app_id"]] = base.get("app_name")

        # 4-7: control / artwork / volume exercises, only when a session exists
        if base.get("has_session"):
            controls = run_controls(controller, base)
            report["controls"] = controls
            for row in controller.get_state().get("sessions") or []:
                if row.get("app_id"):
                    report["aumid_to_name"][row["app_id"]] = row.get("app_name")

            returns = controls.get("returns", {})
            failed_transport = [
                key
                for key in ("play_pause_1", "play_pause_2", "next_track", "previous_track")
                if returns.get(key) is not True
            ]
            report["checks"]["transport_calls_returned_true"] = not failed_transport
            if failed_transport:
                notes.append(
                    "these transport calls returned False (recorded raw, not a failure): %s"
                    % ", ".join(failed_transport)
                )
            if controls.get("shape_deviations"):
                failures.append("state shape deviated after a command")

            seek = controls.get("seek", {})
            report["checks"]["seek_verified"] = bool(seek.get("moved_to_target"))
            if seek.get("status") != "SKIPPED" and not seek.get("moved_to_target"):
                failures.append(
                    "seek was exercised but its effect could not be verified within "
                    "the timeout (hard check)"
                )

            artwork = controls.get("artwork", {})
            report["checks"]["artwork_decoded"] = bool(artwork.get("decoded_bytes"))
            if artwork.get("status") == "exercised" and not artwork.get("decoded_bytes"):
                failures.append(
                    "artwork was reported (an art_key was present) but its bytes "
                    "did not decode"
                )

            volume = controls.get("volume", {})
            report["checks"]["set_volume_verified"] = bool(volume.get("read_back_matches"))
            if volume.get("set_volume_return") is not True:
                failures.append("set_volume() returned False")
            if not volume.get("read_back_matches"):
                failures.append("set_volume() did not change the system endpoint volume")

            restored = controls.get("restore", {})
            if restored.get("status_restored") is False:
                failures.append("playback status was not restored")
            if restored.get("volume_ok") is False:
                failures.append("system volume was not restored")
            if restored.get("mute_ok") is False:
                failures.append("system mute state was not restored")

            select = controls.get("select", {})
            report["checks"]["select_session_pin_falls_back_and_stays_live"] = bool(
                select.get("has_session_true_after_bogus")
                and select.get("dark_samples_after_bogus") == 0
                and select.get("fallback_kept_real_live_app")
            )
            report["checks"]["select_session_reselected_target"] = bool(
                select.get("has_session_true_after_reselect")
            )
            report["checks"]["follow_current_session_restored"] = bool(
                select.get("follow_current_has_session")
            )
            if select.get("target_aumid"):
                if not select.get("has_session_true_after_bogus"):
                    failures.append(
                        "pinning a nonexistent app id made the widget go dark: "
                        "has_session stayed false with %s session(s) still held by "
                        "the media layer" % select.get("sessions_still_enumerated")
                    )
                if select.get("dark_samples_after_bogus"):
                    failures.append(
                        "the widget flickered dark while a nonexistent app id was "
                        "pinned (%s dark samples of %s)"
                        % (select.get("dark_samples_after_bogus"),
                           len(select.get("samples_after_bogus") or []))
                    )
                if not select.get("fallback_kept_real_live_app"):
                    failures.append(
                        "a nonexistent pin fell back to %r, not a live enumerated "
                        "app %s" % (select.get("app_id_after_bogus"),
                                    select.get("live_sessions"))
                    )
                if not select.get("has_session_true_after_reselect"):
                    failures.append("re-selecting the live app did not take")
            if not select.get("follow_current_has_session"):
                failures.append("select_session('') did not restore follow-current")
        else:
            report["controls"] = {
                "attempted": False,
                "status": "SKIPPED",
                "reason": "no live media session exists on this machine",
            }
            for key in (
                "transport_calls_returned_true",
                "seek_verified",
                "artwork_decoded",
                "set_volume_verified",
            ):
                report["checks"][key] = None
            notes.append(
                "no live session: transport/seek/artwork/volume exercises skipped "
                "(honest skip); timing and shape checks still ran"
            )
    finally:
        controller.shutdown()
        # shutdown() must be idempotent.
        controller.shutdown()

    report["checks"]["calls_after_shutdown_return_false"] = controller.play_pause() is False
    if not report["checks"]["calls_after_shutdown_return_false"]:
        failures.append("play_pause() after shutdown() did not return False")

    report["checks"]["get_state_after_shutdown_ok"] = controller.get_state().get("ok") is False
    if not report["checks"]["get_state_after_shutdown_ok"]:
        failures.append("get_state() after shutdown() did not report ok=false")

    report["checks"]["elapsed_s"] = round(time.monotonic() - started, 2)
    report["pass"] = not failures
    report["failures"] = failures
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
