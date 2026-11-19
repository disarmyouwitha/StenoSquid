"""Decode the experimental flash map, based only on catalog ownership."""
from native_snapshot import inspect_free
from native_update import update_command

REGIONS = {
    "c": "Catalog roots",
    "x": "Reserved final segment",
    "b": "Pinned compact image",
    "s": "User snapshot",
    "h": "Latest journal",
    "j": "Journal",
    "f": "Forward index",
    "r": "Reverse index",
    ".": "Unreferenced",
    "?": "Unknown",
}


def parse_layout(response):
    parts = response.split()
    if len(parts) != 7 or parts[:2] != ["OK", "MAP1"]:
        raise ValueError("Invalid flash map response")
    _, _, selected, access, states, first, second = parts
    if selected not in ("0", "1") or access not in ("w", "r", "p") or len(states) != 2 or set(states) - set("ve?"):
        raise ValueError("Invalid flash map status")
    maps = first, second
    for state, data in zip(states, maps):
        if len(data) != 256 or data[:2] != "cc" or data[-1] != "x" or set(data) - REGIONS.keys():
            raise ValueError("Invalid flash map geometry")
        if (state == "e" and set(data[2:255]) != {"."}) or (state == "?" and set(data[2:255]) != {"?"}):
            raise ValueError("Invalid catalog region")
        if state == "v" and "?" in data:
            raise ValueError("Incomplete catalog region")
    active_slot = int(selected)
    active, alternate = maps[active_slot], maps[1 - active_slot]
    if states[active_slot] == "e":
        raise ValueError("Active catalog is missing")
    whole = "?" not in states and access != "r"
    protected = {i for i, (primary, old) in enumerate(zip(active, alternate)) if primary != "." or old != "."}
    free, longest = inspect_free(protected) if whole else (None, None)
    segments = []
    for idx, (primary, old) in enumerate(zip(active, alternate)):
        if idx in (0, 1, 255):
            kind = primary
        elif primary != ".":
            kind = primary
        elif old in "jh":
            kind = "retained-journal"
        elif old not in ".?":
            kind = "retained"
        else:
            kind = "." if whole else "?"
        segments.append(dict(index=idx, kind=kind, active=REGIONS[primary], alternate=REGIONS[old],
                             shared=primary not in ".?" and old not in ".?" and 2 <= idx < 255))
    return dict(segments=segments, active_slot=active_slot, states=list(states), free_segments=free,
                largest_free_run=longest, reserve=3, staging_blocked=access != "w")


def read_layout(console):
    return parse_layout(update_command(console, "layout-map"))
