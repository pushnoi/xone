# SPDX-License-Identifier: GPL-2.0-only
"""Failure detection only: no USB, subprocess, filesystem, or reset operations.

Caller supplies ordered current journal records fetched AFTER state.cursor (or
the full batch containing that cursor). Never restart at an old cursor after
journal rotation. A new installation must seed cursor at the journal end.
Pass CLOCK_MONOTONIC seconds, matching journald's monotonic timestamp clock.
State must be persisted atomically before executing any returned candidate.
"""

from dataclasses import dataclass
import re
from typing import Callable, Iterable, Mapping


FAILURE = re.compile(
    r"^xone-dongle (?P<port>\d+-\d+(?:\.\d+)*):(?P<intf>\d+\.\d+): "
    r"xone_dongle_fw_load: init radio failed: -110$"
)


@dataclass
class State:
    boot_id: str
    cursor: str | None = None
    resume_epoch: int = 0
    attempted_epoch: int | None = None


@dataclass(frozen=True)
class Candidate:
    port: str
    interface: str
    cursor: str
    monotonic_seconds: float
    resume_epoch: int
    bootstrap: bool = False
    usb_device_number: int | None = None


def consume(
    events: Iterable[Mapping],
    state: State,
    *,
    boot_id: str,
    now: float,
    guard: Callable[[Candidate], bool],
    bootstrap: bool = False,
) -> Candidate | None:
    """Consume events and reserve at most one recovery in the latest epoch.

    guard MUST verify stock loaded source, configured serial/045e:02e6, pairing=0,
    no active clients, and unchanged USB generation. It must reject ambiguous
    evidence. Recheck those guards under a lock immediately before any action.
    False or an exception consumes the candidate without reserving an attempt.
    The once-per-epoch budget is for this ONE receiver, across all USB ports.
    bootstrap requires full-boot records. An older first-boot candidate needs
    guard to prove wakeup disabled and usb_device_number still current; the
    same extra checks and 120-second uptime bound must hold before acting.
    """
    records = list(events)
    if state.boot_id != boot_id:
        state.boot_id, state.cursor = boot_id, None
        state.resume_epoch, state.attempted_epoch = 0, None
    bootstrap_allowed = bootstrap and state.cursor is None and 0 <= now <= 120
    # Permit full-batch replay only when our exact cursor is present.
    if state.cursor:
        positions = [i for i, e in enumerate(records)
                     if e.get("__CURSOR") == state.cursor]
        if positions:
            records = records[positions[-1] + 1:]
    pending = None
    enumerations = {}
    for event in records:
        if event.get("_BOOT_ID") != boot_id:
            continue
        cursor = event.get("__CURSOR")
        if not isinstance(cursor, str) or not cursor:
            continue
        state.cursor = cursor  # Includes stale, unrelated, malformed or skipped.
        if event.get("_TRANSPORT") != "kernel":
            continue
        message = event.get("MESSAGE")
        if not isinstance(message, str):
            continue
        if message.startswith("PM: suspend entry ("):
            state.resume_epoch += 1
            pending = None  # Never act on a failure preceding this sleep cycle.
            continue
        usb_event = re.match(r"^usb (\d+-\d+(?:\.\d+)*): (?:USB disconnect|new .* USB device number)", message)
        if usb_event:
            enumerations.pop(usb_event[1], None)
            enumeration = re.match(r"^usb (\d+-\d+(?:\.\d+)*): new .* USB device number (\d+) using ", message)
            if enumeration:
                enumerations[enumeration[1]] = int(enumeration[2])
        if pending is not None and usb_event and usb_event[1] == pending.port:
            pending = None  # The failed USB generation no longer exists.
        if message == "usbcore: deregistering interface driver xone-dongle":
            pending = None
        match = FAILURE.fullmatch(message)
        if not match:
            continue
        try:
            timestamp = int(event["__MONOTONIC_TIMESTAMP"]) / 1_000_000
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        age = now - timestamp
        boot_candidate = (bootstrap_allowed and state.resume_epoch == 0 and
                          20 < age <= 120 and match["port"] in enumerations)
        if 0 <= age <= 20 or boot_candidate:
            pending = Candidate(match["port"],
                                f'{match["port"]}:{match["intf"]}',
                                cursor, timestamp, state.resume_epoch,
                                boot_candidate, enumerations.get(match["port"]))
    if pending is None or state.attempted_epoch == state.resume_epoch:
        return None
    try:
        allowed = guard(pending)
    except Exception:
        return None
    if not allowed:
        return None
    state.attempted_epoch = state.resume_epoch  # Reserve BEFORE acting.
    return pending
