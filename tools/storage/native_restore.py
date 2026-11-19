"""Validate and restore a portable Native dictionary backup by explicit reprovisioning."""
from build_native_image import SOURCES, build
from import_personal import open_console
from native_base_update import PINS
from native_install import provision, wait_ready
from native_manage import checked
from native_update import validate_name
from prepare import fetch
from manifest import ROOT

LAYOUTS = ("Mac U.S.", "Windows U.S. (plain text)")


def parse_backup(backup, limit, pins=PINS):
    if not isinstance(backup, dict) or backup.get("format") != "stenosquid-backup-1" or backup.get("host_layout") not in LAYOUTS:
        raise ValueError("Choose a Steno Squid full backup with a supported host layout.")
    base_ids = {name: identity for identity, name, _, source in SOURCES if source}
    dictionaries = backup.get("dictionaries")
    next_id = backup.get("next_id")
    if not isinstance(dictionaries, list) or not len(base_ids) + 1 <= len(dictionaries) <= limit:
        raise ValueError(f"The backup must fit this keyboard’s {limit}-dictionary catalog.")
    if type(next_id) is not int or not 8 <= next_id <= 0xffffffff:
        raise ValueError("Invalid next dictionary ID")
    rows, entries, ids, names = [], {}, set(), set()
    for dictionary in dictionaries:
        if not isinstance(dictionary, dict) or not isinstance(dictionary.get("entries"), list):
            raise ValueError("Invalid dictionary in backup")
        identity, name, enabled = (dictionary.get(key) for key in ("id", "name", "enabled"))
        if (type(identity) is not int or not 0 < identity < next_id
                or identity in ids or type(enabled) is not bool or not isinstance(name, str) or name in names):
            raise ValueError("Invalid or duplicate dictionary identity")
        if name in base_ids:
            if identity != base_ids[name] or dictionary.get("source_sha256") != pins[name][2]:
                raise ValueError(f"{name} needs the pinned source revision listed in the backup.")
        elif name == "user_dictionary":
            if identity != 1 and identity <= 7:
                raise ValueError("Invalid personal dictionary ID")
        else:
            if identity < 8:
                raise ValueError("Invalid named dictionary ID")
            validate_name(name)
        rows.append((identity, name, enabled))
        ids.add(identity)
        names.add(name)
        for entry in dictionary["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("strokes"), list):
                raise ValueError(f"Invalid record in {name}")
            strokes = tuple(entry["strokes"])
            if not 1 <= len(strokes) <= 16 or any(type(mask) is not int or not 0 < mask < 1 << 25
                                                  for mask in strokes):
                raise ValueError(f"Invalid outline in {name}")
            if (identity, strokes) in entries:
                raise ValueError(f"Duplicate outline in {name}")
            operation = entry.get("operation")
            if operation == "set":
                if not isinstance(entry.get("text"), str):
                    raise ValueError(f"Invalid translation in {name}")
                text = entry["text"].encode("utf-8")
                if len(text) > 2047 or b"\0" in text:
                    raise ValueError(f"Invalid translation in {name}")
                entries[identity, strokes] = (0, text)
            elif operation == "remove":
                entries[identity, strokes] = (1, b"")
            else:
                raise ValueError(f"Invalid overlay operation in {name}")
    if not {*base_ids, "user_dictionary"} <= names:
        raise ValueError("The backup is missing a base or the personal dictionary.")
    return rows, entries, next_id


def restore_all(serial, backup, progress=print):
    with open_console(serial) as console:
        capabilities = console.command("native_install update named-capabilities").split()
        if len(capabilities) != 3 or capabilities[:2] != ["OK", "NAMED1"]:
            raise RuntimeError("Flash the matching Native firmware before restoring a full backup.")
    collection = parse_backup(backup, int(capabilities[2]))
    progress("Preparing the backup and checking pinned sources…")
    for url, file, digest, directory in PINS.values():
        path = ROOT / "build" / directory / file
        path.parent.mkdir(parents=True, exist_ok=True)
        fetch(url, path, digest)
    image, manifest = build(ROOT / "build", collection=collection)
    with open_console(serial) as console:
        provision(console, image, manifest, progress)
    progress("The keyboard acknowledged the restore and is reconnecting…")
    wait_ready(serial)
    with open_console(serial) as console:
        checked(console, "set_host_layout " + backup["host_layout"])
