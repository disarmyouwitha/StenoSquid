"""Boundary and failure-mode cases for golden_snapshot_plan.py (kept separate so the main generator stays readable).

Each case pins one comparison in the planners: the identity counter limit, the 253-extent limit, every merge-pair
condition, retained reset entries, shared compact extents, scattered free space, and the JSON writer's escaping rules.
"""
import hashlib
import json
import struct
import zlib

import golden_plan as plan_vectors
import golden_snapshot as base_vectors
from native_import_plan import consolidation_cost, metadata
from native_snapshot import merge_rows, offline_merge_token, plan_change, plan_named_snapshot
from native_update import Edit

SEGMENT = 65536


def patch_catalog(image, change):
    """A copy of the image whose catalog payload was edited by change(payload) -> payload (header size and CRC fixed)."""
    result = bytearray(image)
    size, _ = struct.unpack_from("<II", result, SEGMENT + 16)
    payload = bytes(change(bytearray(result[SEGMENT + 32:SEGMENT + 32 + size])))
    result[SEGMENT + 32:SEGMENT + 32 + len(payload)] = payload
    struct.pack_into("<II", result, SEGMENT + 16, len(payload), zlib.crc32(payload))
    return result


def row_at(payload, name):
    count = struct.unpack_from("<H", payload, 4)[0]
    for index in range(count):
        row = payload[32 + 96 * index:32 + 96 * (index + 1)]
        if row[16:16 + row[12]].decode() == name:
            return 32 + 96 * index
    raise KeyError(name)


def pad_extents(total):
    def change(payload):
        count, extents = struct.unpack_from("<HH", payload, 4)
        first = bytes(payload[32 + 96 * count:32 + 96 * count + 24])  # a kind-1 base extent, repeated as filler
        struct.pack_into("<H", payload, 6, total)
        return bytes(payload) + first * (total - extents)
    return change


def patch_row(name, changes):
    def change(payload):
        at = row_at(payload, name)
        for offset, value in changes.items():
            payload[at + offset] = value
        return payload
    return change


def share_base(source, target):
    def change(payload):
        a, b = row_at(payload, source), row_at(payload, target)
        payload[a + 8:a + 10] = payload[b + 8:b + 10]
        return payload
    return change


def set_next_id(value):
    def change(payload):
        struct.pack_into("<I", payload, 0, value)
        return payload
    return change


def generate_extra(images, cases, chain, ids, replacement, helpers):
    build_compiled, compiled_spec, outcome, serialise = (helpers[k] for k in ("build_compiled", "compiled_spec", "outcome", "serialise"))
    small, small_end = chain["small"]
    snaps, _ = chain["snaps"]
    snap_one, snap_two, personal = ids["Snap One"], ids["Snap Two"], ids["user_dictionary"]
    one = compiled_spec(40000, [0, 8, 40, 96, 4096])

    def add_image(label, data):
        chain[label] = (data, small_end)
        images[label] = plan_vectors.record_image(data, small_end)

    def snapshot_for(label):
        return plan_vectors.snapshot_of(chain[label][0], small_end)

    def named_on(label, title, spec=one, limit=16):
        snapshot, owned = snapshot_for(label), plan_vectors.owned(chain[label][0], small_end)
        cases.append({"kind": "named", "image": label, "name": title, "compiled": spec, "limit": limit, "protected": sorted(owned),
                      "expect": outcome(lambda: serialise(plan_named_snapshot(snapshot, owned, title, build_compiled(spec), limit=limit)))})

    def change_on(label, title, identity, spec=None, protected=None, **flags):
        snapshot = snapshot_for(label)
        owned = plan_vectors.owned(chain[label][0], small_end) if protected is None else protected
        compiled = None if spec is None else build_compiled(spec)
        cases.append({"kind": "change", "image": label, "title": title, "identity": identity, "compiled": spec, "flags": dict(flags),
                      "protected": sorted(owned),
                      "expect": outcome(lambda: serialise(plan_change(snapshot, owned, identity, compiled, **flags)))})

    def merge_rows_on(label, title, upper, lower):
        snapshot = snapshot_for(label)

        def pair():
            a, b = merge_rows(snapshot, upper, lower)
            return [snapshot.rows.index(a), snapshot.rows.index(b)]
        cases.append({"kind": "mergeRows", "image": label, "title": title, "upper": upper, "lower": lower, "expect": outcome(pair)})

    # The identity counter at its limit and one below it.
    add_image("nextid_max", patch_catalog(small, set_next_id(0xffffffff)))
    add_image("nextid_edge", patch_catalog(small, set_next_id(0xfffffffe)))
    named_on("nextid_max", "Exhausted Counter")
    named_on("nextid_edge", "Last Identity")

    # The catalog extent limit (253): 252 extents leave room for one more, 253 do not.
    add_image("ext252", patch_catalog(small, pad_extents(252)))
    add_image("ext253", patch_catalog(small, pad_extents(253)))
    named_on("ext252", "One More Extent")
    named_on("ext253", "No More Extents")
    custom_a = next(struct.unpack_from("<I", r)[0] for r in snapshot_for("ext252").rows if r[16:16 + r[12]].decode() == "Custom A")
    change_on("ext252", "convert with 252 extents (the image makes 253)", custom_a, replacement, convert=True)
    change_on("ext253", "convert with 253 extents", custom_a, replacement, convert=True)
    change_on("ext252", "clear with 252 extents (two indexes would make 254)", personal, clear=True)

    # Merge-pair conditions, one broken at a time.
    add_image("pair_enabled", patch_catalog(snaps, patch_row("Snap One", {4: 0})))
    merge_rows_on("pair_enabled", "different Active setting", snap_two, snap_one)
    add_image("pair_samebase", patch_catalog(snaps, share_base("Snap One", "Snap Two")))
    merge_rows_on("pair_samebase", "both rows share one compact extent", snap_two, snap_one)
    change_on("pair_samebase", "rebuild a dictionary whose extent is shared", snap_two, replacement, consolidate=True)
    add_image("pair_marker", patch_catalog(snaps, patch_row("Snap One", {11: 0})))
    merge_rows_on("pair_marker", "lower row is not a compact dictionary", snap_two, snap_one)
    add_image("pair_personal", patch_catalog(snaps, patch_row("user_dictionary", {11: 1})))
    merge_rows_on("pair_personal", "the personal dictionary in the pair", personal, snap_two)
    change_on("pair_personal", "delete a personal dictionary flagged as compact", personal)
    add_image("idrange", patch_catalog(small, patch_row("Plover Base", {8: 255, 9: 255, 10: 0, 11: 0})))
    change_on("idrange", "convert a factory identity", 7, replacement, convert=True)
    change_on("small", "convert without a replacement", custom_a, None, convert=True)
    change_on("small", "convert and merge", custom_a, replacement, convert=True, merge_id=snap_one)
    change_on("small", "convert, consolidate and merge", custom_a, replacement, convert=True, consolidate=True, merge_id=snap_one)

    # Journal entries (a reset, sets, entries of both merged identities) and a highwater beyond the checkpoint.
    tailed = bytearray(snaps)
    base_vectors.append_tail(tailed, small_end, [
        Edit(0, 8, 1, (0x99,), b""), Edit(0, 8, 0, (0x98,), b"live in custom a"),
        Edit(0, snap_one, 0, (0x31,), b"in snap one"), Edit(0, snap_two, 0, (0x32,), b"in snap two"),
        Edit(0, snap_one, 1, (0x33,), b""), Edit(0, personal, 0, (0x34,), b"personal")])
    add_image("snaps_tail", tailed)
    change_on("snaps_tail", "rebuild with journal entries and a highwater tail", snap_one, replacement, consolidate=True)
    change_on("snaps_tail", "merge with entries of both identities", snap_two, replacement, consolidate=True, merge_id=snap_one)
    change_on("snaps_tail", "convert keeps reset entries", custom_a, replacement, convert=True)
    change_on("snaps_tail", "clear drops personal entries", personal, clear=True)
    change_on("snaps_tail", "delete a compact dictionary", snap_one)
    holes = set(range(small_end + 1)) - {2} - set(range(40, 60))
    change_on("snaps_tail", "merge into scattered free segments", snap_two, replacement, protected=holes, consolidate=True, merge_id=snap_one)

    # Token inputs that exercise the JSON writer: control characters, DEL, U+0080..U+00FF, astral characters, escapes.
    snapshot = snapshot_for("snaps")
    serials = ["a" + chr(10) + "b" + chr(9) + "c" + chr(13) + "d" + chr(8) + "e" + chr(12) + "f", "ctrl" + chr(1) + chr(31),
               "del" + chr(127), "nbsp" + chr(0xa0) + chr(0xff) + chr(0x100), "emoji " + chr(0x1f600) + " end",
               "back" + chr(92) + "slash " + chr(34) + "q" + chr(34) + " /"]
    for serial in serials:
        cases.append({"kind": "offlineToken", "image": "snaps", "serial": serial, "upper": 1, "lower": 2,
                      "expect": offline_merge_token(snapshot, serial, 1, 2)})
    update = plan_change(snapshot, holes, snap_two, build_compiled(replacement), consolidate=True, merge_id=snap_one)
    fingerprint = hashlib.sha256(json.dumps(["s", "OK 9", snap_two, snap_one, snapshot.highwater], separators=(",", ":")).encode())
    fingerprint.update(snapshot.payload)
    for segment, block in sorted(update.segments.items()):
        fingerprint.update(struct.pack("<II", segment, len(block)))
        fingerprint.update(block)
    wide = compiled_spec(70000, [0, 16, 900])
    unsorted_update = plan_change(snapshot, holes, snap_two, build_compiled(wide), consolidate=True, merge_id=snap_one)
    wide_fingerprint = hashlib.sha256(json.dumps(["s", "OK 9", snap_two, snap_one, snapshot.highwater], separators=(",", ":")).encode())
    wide_fingerprint.update(snapshot.payload)
    for segment, block in sorted(unsorted_update.segments.items()):
        wide_fingerprint.update(struct.pack("<II", segment, len(block)))
        wide_fingerprint.update(block)
    cases.append({"kind": "mergeToken", "image": "snaps", "compiled": wide, "serial": "s", "answer": "OK 9", "upper": snap_two,
                  "lower": snap_one, "protected": sorted(holes), "expect": wide_fingerprint.hexdigest(),
                  "stagedOrder": list(unsorted_update.segments)})
    cases.append({"kind": "mergeToken", "image": "snaps", "compiled": replacement, "serial": "s", "answer": "OK 9", "upper": snap_two,
                  "lower": snap_one, "protected": sorted(holes), "expect": fingerprint.hexdigest()})

    # consolidation_cost at exact block boundaries and at the extent limit.
    def cost_cases(label, combos):
        data = chain[label][0]
        snap = plan_vectors.snapshot_of(data, small_end)
        probe = plan_named_snapshot(snap, plan_vectors.owned(data, small_end), "Cost Probe", build_compiled(one), limit=16)
        after = metadata(probe.payload, small_end)
        for image_segments, index_records in combos:
            cases.append({"kind": "consolidationCost", "image": label, "compiled": one, "limit": 16, "imageSegments": image_segments,
                          "indexRecords": list(index_records),
                          "expect": outcome(lambda: consolidation_cost(after, image_segments, index_records))})

    cost_cases("small", ((1, (16384, 0)), (1, (16385, 0)), (1, (0, 16384)), (2, (16384, 16384))))
    cost_cases("ext252", ((1, (0, 0)), (1, (5, 0)), (1, (5, 5))))

    def padded_without_indexes(payload):
        payload = bytearray(pad_extents(253)(payload))
        count = struct.unpack_from("<H", payload, 4)[0]
        for index in range(struct.unpack_from("<H", payload, 6)[0]):
            at = 32 + 96 * count + 24 * index
            if struct.unpack_from("<I", payload, at + 12)[0] in (3, 4):
                struct.pack_into("<I", payload, at + 12, 1)
                struct.pack_into("<Q", payload, at + 16, 0)
        return payload

    add_image("ext253k", patch_catalog(small, padded_without_indexes))
    after = snapshot_for("ext253k")
    for image_segments, index_records in ((1, (0, 0)), (1, (1, 0)), (1, (0, 1)), (1, (1, 1))):
        cases.append({"kind": "costDirect", "image": "ext253k", "imageSegments": image_segments, "indexRecords": list(index_records),
                      "expect": outcome(lambda: consolidation_cost(after, image_segments, index_records))})
