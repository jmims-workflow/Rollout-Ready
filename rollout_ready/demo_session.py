"""Demo mode keeps a visitor's overrides and release events in the signed session cookie.

Demo mode writes nothing to disk, so the cookie is the only place. Browsers cap a
cookie at about 4,096 bytes, so events are stored in a short form (one-letter
fields, 16-character fingerprints, 8-character override ids) and every change is measured
before it is saved: if it would take the cookie past COOKIE_BUDGET, it is refused,
nothing changes, and the visitor is offered "Start over".
"""

from __future__ import annotations

import json
from base64 import b64encode

from .evidence import OverrideEvent
from .releases import ReleaseEvent

# The signed value Starlette writes is the base64 session plus about 40 characters of
# timestamp and signature. 3,600 leaves room for those, the cookie's name and its settings.
COOKIE_BUDGET = 3600
BROWSER_COOKIE_LIMIT = 4096
FULL = ("This demo keeps your changes in your browser, and there's no room for more. "
        "Start over to keep exploring.")
OVERRIDES, RELEASES = "override_events", "release_events"

_CALLS = {"not_worse": "w", "not_better": "b", "other": "o"}
_RESULTS = {"worse": "w", "better": "b", "same": "s"}
_TYPES = {"baseline": "b", "rollout": "r", "exception": "x", "return": "t"}
_SOURCES = {"recorded": "r", "live": "l"}
_VERDICTS = {"Ready to roll out": "r", "Not ready to roll out": "n", "Needs review": "v", "No clear difference": "d"}


def _back(table: dict, code):
    return next((k for k, v in table.items() if v == code), None)


class CookieFull(Exception):
    """The change would take the demo cookie past its budget. Nothing was saved."""


def encoded_size(session: dict) -> int:
    """The length of the session as Starlette encodes it, before signing."""
    return len(b64encode(json.dumps(session).encode("utf-8")))


def fits(session: dict) -> bool:
    return encoded_size(session) <= COOKIE_BUDGET


# short forms ------------------------------------------------------------------------
def pack_override(e: OverrideEvent) -> dict:
    out = {"i": e.id[:8], "f": e.fingerprint[:16], "c": e.case_id, "t": e.created[:10]}
    if e.evaluation_id:
        out["e"] = e.evaluation_id
    if e.action == "remove":
        out["a"] = "r"
    else:
        out.update(k=_CALLS[e.call], r=_RESULTS[e.result], w=e.reason)
    return out


def unpack_override(d: dict) -> OverrideEvent:
    remove = d.get("a") == "r"
    return OverrideEvent(id=d["i"], fingerprint=d["f"], evaluation_id=d.get("e", ""), case_id=d["c"],
                         action="remove" if remove else "set",
                         call=None if remove else _back(_CALLS, d.get("k")),
                         result=None if remove else _back(_RESULTS, d.get("r")),
                         reason="" if remove else d.get("w", ""), created=d.get("t", ""))


def pack_release(e: ReleaseEvent) -> dict:
    out = {"i": e.id, "y": _TYPES[e.type], "o": e.to_version, "t": e.timestamp}  # full id: pages link to it
    optional = {"f": e.from_version, "b": e.confirmed_by, "e": e.evaluation_id,
                "s": _SOURCES.get(e.evaluation_source, ""), "v": _VERDICTS.get(e.verdict or "", ""),
                "x": [i[:8] for i in e.override_ids], "w": e.reason, "k": e.token}
    out.update({k: v for k, v in optional.items() if v})
    if e.baseline_ack:
        out["a"] = 1
    return out


def unpack_release(d: dict) -> ReleaseEvent:
    return ReleaseEvent(id=d["i"], type=_back(_TYPES, d["y"]), from_version=d.get("f", ""), to_version=d["o"],
                        timestamp=d["t"], confirmed_by=d.get("b", ""), evaluation_id=d.get("e", ""),
                        evaluation_source=_back(_SOURCES, d.get("s")) or "",
                        verdict=_back(_VERDICTS, d.get("v")), override_ids=list(d.get("x", [])),
                        reason=d.get("w", ""), baseline_ack=bool(d.get("a")), token=d.get("k", ""))


# reading and adding ---------------------------------------------------------------------
def _read(session: dict, key: str, suite_id: str, unpack) -> list:
    out = []
    for d in session.get(key, {}).get(suite_id, []):
        try:
            out.append(unpack(d))
        except (KeyError, TypeError, ValueError):
            continue  # an unreadable entry is skipped, never fatal
    return out


def overrides(session: dict, suite_id: str) -> list[OverrideEvent]:
    return _read(session, OVERRIDES, suite_id, unpack_override)


def releases(session: dict, suite_id: str) -> list[ReleaseEvent]:
    return _read(session, RELEASES, suite_id, unpack_release)


def with_added(session: dict, key: str, suite_id: str, packed: dict) -> dict:
    """The session with one more event, or CookieFull if it wouldn't fit. The session itself is unchanged."""
    events = dict(session.get(key, {}))
    events[suite_id] = [*events.get(suite_id, []), packed]
    new = {**session, key: events}
    if not fits(new):
        raise CookieFull(FULL)
    return new


def add_override(session: dict, suite_id: str, event: OverrideEvent) -> None:
    new = with_added(session, OVERRIDES, suite_id, pack_override(event))
    session[OVERRIDES] = new[OVERRIDES]


def add_release(session: dict, suite_id: str, event: ReleaseEvent) -> None:
    new = with_added(session, RELEASES, suite_id, pack_release(event))
    session[RELEASES] = new[RELEASES]


def start_over(session: dict) -> None:
    """Clear this visit's demo changes. The shipped demo history is untouched."""
    session.pop(OVERRIDES, None)
    session.pop(RELEASES, None)
