"""Deterministic sample identifiers (design D6).

A sample's identity is the *slot* it was scheduled for, floored to the poll
interval and stamped in UTC: `20260820T091500Z` is the 17:15 Asia/Manila slot.

UTC because the identifier has to stay unambiguous across DST and any later
timezone reconfiguration, and because it then sorts lexicographically in the
same order as chronologically — which makes "the most recent sample" a
descending key scan needing no index at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

SAMPLE_ID_FORMAT = "%Y%m%dT%H%M%SZ"


def floor_to_slot(moment: datetime, interval_seconds: int) -> datetime:
    """Floor `moment` to the start of its poll slot, in UTC."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")

    utc = moment.astimezone(UTC).replace(microsecond=0)
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % interval_seconds), tz=UTC)


def sample_id_for(moment: datetime, interval_seconds: int) -> str:
    """The identifier of the slot `moment` falls in."""
    return floor_to_slot(moment, interval_seconds).strftime(SAMPLE_ID_FORMAT)


def sample_id_of_slot(slot: datetime) -> str:
    """The identifier of an already-floored slot instant."""
    return slot.astimezone(UTC).strftime(SAMPLE_ID_FORMAT)


def slot_of_sample_id(sample_id: str) -> datetime:
    """Recover the scheduled instant a sample identifier names."""
    return datetime.strptime(sample_id, SAMPLE_ID_FORMAT).replace(tzinfo=UTC)
