#pragma once
#include "native_store.h"
#include "native_validator.h"

namespace candidate_b {

enum class NativeUpdateKind { Compact, Personal, Named, Reorder, Rename, Delete, Backup, Base };

// Exclusive USB staging session. The active catalog and both retained roots
// remain readable. Writes are restricted to previously unowned segments.
template <class Device>
class NativeStage {
    NativeStore<Device> &store;
    NativeValidator<Device> &validator;
    SegmentMap protected_segments{}, erased{};
    CatalogSelection original{};
    unsigned remaining = 0;
    bool active = false;
    NativeUpdateKind kind = NativeUpdateKind::Compact;
    uint32_t personal_id = 0, added_id = 0;
    uint16_t dictionary_limit = 64;

    bool ReadExtent(const CatalogRoot &root, uint16_t dictionaries, unsigned row,
                    uint8_t (&extent)[24]) {
        return store.device.Read(root.address + 32 + dictionaries * 96u + row * 24u,
                                 extent, sizeof(extent));
    }

    static bool ValidName(const uint8_t *row) {
        const unsigned count = row[12];
        if (!count || count > 44 || row[16 + count - 1] == ' ') return false;
        for (unsigned i = 0; i < count; ++i) {
            const uint8_t c = row[16 + i];
            const bool alnum = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                               (c >= '0' && c <= '9');
            if (!alnum && !(i && (c == ' ' || c == '.' || c == '-' || c == '_'))) return false;
        }
        return true;
    }

    bool MetadataOnly() const {
        return kind == NativeUpdateKind::Reorder || kind == NativeUpdateKind::Rename ||
               kind == NativeUpdateKind::Delete;
    }

    static bool EditableRow(const uint8_t *row) {
        return row[8] == 255 && row[9] == 255 &&
               !(row[12] == 15 && !std::memcmp(row + 16, "user_dictionary", 15));
    }

    bool NameAvailable(const CatalogRoot &root, unsigned count, const uint8_t *proposed) {
        for (unsigned i = 0; i < count; ++i) {
            uint8_t row[96];
            if (!store.device.Read(root.address + 32 + i * sizeof(row), row, sizeof(row))) return false;
            if (row[12] == proposed[12] &&
                !std::memcmp(row + 16, proposed + 16, proposed[12])) return false;
        }
        return true;
    }

    // Reorder changes only the row permutation. Named import adds one fresh
    // overlay identity. Every pre-existing row and compiled base is retained.
    bool CheckLayout(const CatalogRoot &candidate, uint32_t metadata_segment) {
        uint8_t old_prefix[32], new_prefix[32];
        if (!store.device.Read(original.root.address, old_prefix, sizeof(old_prefix)) ||
            !store.device.Read(candidate.address, new_prefix, sizeof(new_prefix))) return false;
        const uint16_t dictionaries = old_prefix[4] | old_prefix[5] << 8;
        const uint16_t new_dictionaries = new_prefix[4] | new_prefix[5] << 8;
        const uint16_t old_count = old_prefix[6] | old_prefix[7] << 8;
        const uint16_t new_count = new_prefix[6] | new_prefix[7] << 8;
        const bool named = kind == NativeUpdateKind::Named;
        const bool reorder = kind == NativeUpdateKind::Reorder;
        const bool rename = kind == NativeUpdateKind::Rename;
        const bool remove = kind == NativeUpdateKind::Delete;
        const bool metadata = MetadataOnly();
        const uint32_t next_id = Word(old_prefix);
        if (new_dictionaries + unsigned(remove) != dictionaries + unsigned(named) || new_dictionaries > dictionary_limit ||
            (named && next_id == UINT32_MAX) || Word(new_prefix) != next_id + unsigned(named) ||
            old_count > 253 || new_count > 253 ||
            candidate.bytes != 32 + new_dictionaries * 96u + new_count * 24u) return false;
        if (metadata) {
            if (new_count != old_count || std::memcmp(old_prefix + 6, new_prefix + 6, 26)) return false;
        } else {
            const uint64_t through = uint64_t(Word(new_prefix + 12)) << 32 | Word(new_prefix + 8);
            if (through < store.live.Highwater()) return false;
        }

        personal_id = added_id = 0;
        unsigned previous = 0;
        bool renamed = false;
        uint64_t seen = 0;
        for (unsigned i = 0; i < new_dictionaries; ++i) {
            uint8_t old_row[96], new_row[96];
            if (!store.device.Read(candidate.address + 32 + i * sizeof(new_row), new_row, sizeof(new_row)))
                return false;
            const uint32_t identity = Word(new_row);
            const uint16_t new_base = new_row[8] | new_row[9] << 8;
            if (named && identity == next_id) {
                if (added_id || new_base != kNoExtent || Word(new_row + 4) != 1 ||
                    !ValidName(new_row)) return false;
                if (!NameAvailable(original.root, dictionaries, new_row)) return false;
                for (unsigned j = 64; j < sizeof(new_row); ++j)
                    if (new_row[j]) return false;
                added_id = identity;
                continue;
            }
            unsigned old_index = previous;
            if (reorder || remove) {
                for (old_index = remove ? previous : 0; old_index < dictionaries; ++old_index) {
                    if (!store.device.Read(original.root.address + 32 + old_index * sizeof(old_row),
                                           old_row, sizeof(old_row))) return false;
                    if (Word(old_row) == identity) break;
                }
            } else if (old_index < dictionaries) {
                if (!store.device.Read(original.root.address + 32 + old_index * sizeof(old_row),
                                       old_row, sizeof(old_row))) return false;
            }
            if (old_index >= dictionaries || (seen & (uint64_t(1) << old_index))) return false;
            seen |= uint64_t(1) << old_index;
            previous = old_index + 1;
            if (metadata) {
                if (!std::memcmp(old_row, new_row, sizeof(old_row))) continue;
                if (!rename || renamed || !EditableRow(old_row) || !ValidName(new_row) ||
                    !NameAvailable(original.root, dictionaries, new_row) ||
                    std::memcmp(old_row, new_row, 12) ||
                    std::memcmp(old_row + 13, new_row + 13, 3) ||
                    std::memcmp(old_row + 60, new_row + 60, 36)) return false;
                renamed = true;
                continue;
            }
            if (std::memcmp(old_row, new_row, 8) || std::memcmp(old_row + 10, new_row + 10, 86))
                return false;
            const uint16_t old_base = old_row[8] | old_row[9] << 8;
            if (old_base == kNoExtent) {
                if (new_base != kNoExtent) return false;
                if (old_row[12] == 15 && !std::memcmp(old_row + 16, "user_dictionary", 15))
                    personal_id = identity;
            } else {
                uint8_t old_extent[24], new_extent[24];
                if (old_base >= old_count || new_base >= new_count ||
                    !ReadExtent(original.root, dictionaries, old_base, old_extent) ||
                    !ReadExtent(candidate, new_dictionaries, new_base, new_extent) ||
                    std::memcmp(old_extent, new_extent, sizeof(old_extent))) return false;
            }
        }
        if (named && !added_id) return false;
        if (metadata) {
            if (rename && !renamed) return false;
            if (remove) {
                for (unsigned i = 0; i < dictionaries; ++i) {
                    if (seen & (uint64_t(1) << i)) continue;
                    uint8_t row[96];
                    if (!store.device.Read(original.root.address + 32 + i * sizeof(row), row, sizeof(row)) ||
                        !EditableRow(row)) return false;
                }
            }
            for (unsigned i = 0; i < new_count; ++i) {
                uint8_t old_extent[24], new_extent[24];
                if (!ReadExtent(original.root, dictionaries, i, old_extent) ||
                    !ReadExtent(candidate, new_dictionaries, i, new_extent) ||
                    std::memcmp(old_extent, new_extent, sizeof(old_extent))) return false;
            }
            return true;
        }
        for (unsigned i = 0; i < new_count; ++i) {
            uint8_t extent[24];
            if (!ReadExtent(candidate, new_dictionaries, i, extent)) return false;
            const unsigned start = extent[0] | extent[1] << 8, count = extent[2] | extent[3] << 8;
            if (start < 2 || start >= 255 || !count || count > 255 - start) return false;
            bool staged = true;
            for (unsigned j = start; j < start + count; ++j) {
                if (j == metadata_segment) return false;
                staged &= erased.Contains(j);
            }
            if (staged && Word(extent + 12) != 1) continue;
            bool retained = false;
            for (unsigned j = 0; j < old_count && !retained; ++j) {
                uint8_t old_extent[24];
                if (!ReadExtent(original.root, dictionaries, j, old_extent)) return false;
                retained = std::memcmp(extent, old_extent, sizeof(extent)) == 0;
            }
            if (!retained) return false;
        }
        return true;
    }

    using BaseProbe = bool (*)(const uint8_t *extent, const uint8_t *row);
    bool CheckBaseLayout(const CatalogRoot &candidate, uint32_t metadata_segment, BaseProbe probe) {
        uint8_t old_prefix[32], prefix[32];
        if (!probe || candidate.bytes != original.root.bytes ||
            !store.device.Read(original.root.address, old_prefix, sizeof(old_prefix)) ||
            !store.device.Read(candidate.address, prefix, sizeof(prefix)) ||
            std::memcmp(old_prefix, prefix, sizeof(prefix))) return false;
        const uint16_t dictionaries = prefix[4] | prefix[5] << 8;
        const uint16_t count = prefix[6] | prefix[7] << 8;
        SegmentMap updated, changed_sources;
        for (unsigned i = 0; i < count; ++i) {
            uint8_t before[24], after[24];
            if (!ReadExtent(original.root, dictionaries, i, before) ||
                !ReadExtent(candidate, dictionaries, i, after)) return false;
            if (!std::memcmp(before, after, sizeof(before))) continue;
            if (Word(before + 12) != 1 || Word(after + 12) != 1) return false;
            const unsigned start = after[0] | after[1] << 8, segments = after[2] | after[3] << 8;
            const uint32_t bytes = Word(after + 4);
            if (start < 2 || start >= 255 || !segments || segments > 255 - start ||
                bytes < 40 || bytes > segments * kSegmentBytes || bytes % 4 ||
                Word(after + 16) || Word(after + 20)) return false;
            for (unsigned j = start; j < start + segments; ++j)
                if (j == metadata_segment || !erased.Contains(j)) return false;
            if (!validator.Extent(i, after)) return false;
            updated.Add(i, 1);
        }
        bool has_update = false;
        for (unsigned i = 0; i < dictionaries; ++i) {
            uint8_t before[96], after[96];
            if (!store.device.Read(original.root.address + 32 + i * 96, before, sizeof(before)) ||
                !store.device.Read(candidate.address + 32 + i * 96, after, sizeof(after)) ||
                std::memcmp(before, after, 64)) return false;
            const uint16_t base = after[8] | after[9] << 8;
            const bool changed = std::memcmp(before + 64, after + 64, 32) != 0;
            if (base >= count) {
                if (changed) return false;
                continue;
            }
            if (!updated.Contains(base)) {
                if (changed) return false;
                continue;
            }
            if (changed) {
                changed_sources.Add(base, 1);
                has_update = true;
            }
            uint8_t extent[24];
            if (!ReadExtent(candidate, dictionaries, base, extent) || !probe(extent, after)) return false;
        }
        return has_update && !std::memcmp(updated.words, changed_sources.words, sizeof(updated.words));
    }

    bool PreservesEntries() {
        const bool merging_personal = kind == NativeUpdateKind::Personal;
        if (merging_personal && !personal_id) return false;
        const auto &old = store.live.CurrentView();
        const auto &next = validator.ValidatedView();
        if (next.highwater != next.through) return false;
        IndexSink<Device> forward(store.device), reverse(store.device);
        NativeMergeOps<Device> old_ops(store.device, old, forward, reverse);
        NativeMergeOps<Device> new_ops(store.device, next, forward, reverse);
        uint32_t protected_old = 0, protected_new = 0;
        for (uint32_t i = 0; i < old.forward_count + old.tail.count; ++i) {
            uint32_t address;
            if (i < old.forward_count) {
                if (!old_ops.ReadIndex(false, i, address)) return false;
            } else {
                address = old.tail.addresses[i - old.forward_count];
            }
            MergeRecord entry;
            if (!old_ops.Record(address, entry)) return false;
            if (!old_ops.Active(entry.identity)) continue;
            RecordView live;
            const auto state = store.live.FindLive(entry.identity, entry.strokes, entry.count, live);
            if (state == LiveResult::Missing) continue;
            if (state != LiveResult::Ok) return false;
            if (live.operation == Operation::Reset || live.text_address != entry.text_address) continue;
            uint32_t found = 0;
            auto read = [&](uint32_t at, uint8_t *out, size_t bytes) {
                return store.device.Read(next.forward_address + at, out, bytes);
            };
            auto compare = [&](uint32_t candidate) { return new_ops.CompareTo(candidate, entry, false); };
            if (FindOrdered(next.forward_count, read, compare, found) != Result::Found) return false;
            MergeRecord replacement;
            if (!new_ops.Record(found, replacement)) return false;
            if (merging_personal && entry.identity == personal_id) {
                if (!replacement.operation) continue;
            } else {
                ++protected_old;
            }
            if (replacement.operation != entry.operation ||
                (!entry.operation && new_ops.ReverseKeys(entry, replacement) != 0)) return false;
        }
        for (uint32_t i = 0; i < next.forward_count; ++i) {
            uint32_t address;
            MergeRecord entry;
            if (!new_ops.ReadIndex(false, i, address) || !new_ops.Record(address, entry) ||
                !new_ops.Active(entry.identity)) return false;
            if (added_id && entry.identity == added_id) {
                if (entry.operation || entry.record.sequence <= old.highwater) return false;
            } else if (!merging_personal || entry.identity != personal_id) {
                ++protected_new;
            }
        }
        return protected_old == protected_new;
    }

public:
    NativeStage(NativeStore<Device> &store, NativeValidator<Device> &validator)
        : store(store), validator(validator) {}
    bool Active() const { return active; }
    const SegmentMap &Protected() const { return protected_segments; }
    const CatalogRoot &Root() const { return original.root; }

    bool Begin(NativeUpdateKind operation, uint16_t limit = 64) {
        if (limit > 64) return false;
        if (active || !store.opened || store.work != NativeStore<Device>::Work::None)
            return false;
        const auto &root = store.selected.root;
        if (operation == NativeUpdateKind::Backup) {
            // The catalog may be trusted for reading even if its alternate
            // copy was damaged. Export only extents owned by the selected root.
            if (RootOwnership(store.device, store.catalog, root, protected_segments) != CatalogResult::Ok)
                return false;
        } else if (!store.catalog.CanWrite(root) || store.catalog.DestinationPinned(store.selected) ||
                   CollectOwnership(store.device, store.catalog, root, protected_segments) != CatalogResult::Ok) {
            return false;
        }
        original = store.selected;
        kind = operation;
        dictionary_limit = limit;
        erased = {};
        remaining = AllocationPlan(protected_segments).Free();
        active = true;
        store.external_update = true;
        return true;
    }

    void Abort() {
        active = false;
        store.external_update = false;
    }

    bool Read(uint32_t address, uint8_t *out, uint32_t bytes) {
        if (!active || !bytes || bytes > 512 || address >= 255 * kSegmentBytes ||
            bytes > 255 * kSegmentBytes - address) return false;
        if (address >= original.root.address && address - original.root.address <= original.root.bytes &&
            bytes <= original.root.bytes - (address - original.root.address))
            return store.device.Read(address, out, bytes);
        if (kind == NativeUpdateKind::Backup) {
            for (uint32_t at = address / kSegmentBytes; at <= (address + bytes - 1) / kSegmentBytes; ++at)
                if (!protected_segments.Contains(at)) return false;
        }
        // Staging sessions also verify newly allocated extents.
        return address >= 2 * kSegmentBytes && store.device.Read(address, out, bytes);
    }

    bool Erase(uint32_t segment) {
        if (kind == NativeUpdateKind::Backup || !active || segment < 2 || segment >= 255 || protected_segments.Contains(segment))
            return false;
        if (erased.Contains(segment)) return true;
        if (remaining <= 3 || !store.device.Erase(segment)) return false;
        --remaining;
        return erased.Add(segment, 1);
    }

    bool Write(uint32_t address, const uint8_t *data, uint32_t bytes) {
        if (kind == NativeUpdateKind::Backup || !active || !bytes || bytes > 512 || address % 256 || bytes % 4 ||
            address >= 255 * kSegmentBytes || bytes > kSegmentBytes - address % kSegmentBytes ||
            !erased.Contains(address / kSegmentBytes)) return false;
        uint8_t verify[256];
        for (uint32_t offset = 0; offset < bytes; offset += sizeof(verify)) {
            const uint32_t size = bytes - offset < sizeof(verify) ? bytes - offset : sizeof(verify);
            if (!store.device.Read(address + offset, verify, size)) return false;
            for (uint32_t i = 0; i < size; ++i)
                if ((verify[i] & data[offset + i]) != data[offset + i]) return false;
            if (std::memcmp(verify, data + offset, size) &&
                (!store.device.Program(address + offset, data + offset, size) ||
                 !store.device.Read(address + offset, verify, size) ||
                 std::memcmp(verify, data + offset, size))) return false;
        }
        return true;
    }

    // A successful commit needs a reopen. On ambiguous flash failures the
    // caller must also reopen before allowing writes.
    CatalogResult Commit(uint32_t segment, uint32_t bytes, uint32_t crc, bool &reopen,
                         BaseProbe probe = nullptr) {
        reopen = false;
        if (kind == NativeUpdateKind::Backup || !active || segment < 2 || segment >= 255 || !erased.Contains(segment) ||
            bytes < 32 || bytes > store.catalog.PayloadCapacity() ||
            store.selected.root.generation != original.root.generation) return CatalogResult::Invalid;
        bool pinned = false;
        const auto pins = store.catalog.VisitPins([&](const CatalogRoot &) { pinned = true; return true; });
        if (pins != CatalogResult::Ok || pinned) return CatalogResult::Busy;
        const CatalogRoot candidate{original.root.generation + 1, segment * kSegmentBytes, bytes, crc};
        uint32_t checksum = 0;
        if (!store.device.Checksum(candidate.address, bytes, checksum)) return CatalogResult::IoError;
        const bool replacing_base = kind == NativeUpdateKind::Base;
        if (checksum != crc || !(replacing_base ? CheckBaseLayout(candidate, segment, probe)
                                                : CheckLayout(candidate, segment))) return CatalogResult::Corrupt;
        if (!MetadataOnly() && !replacing_base) {
            const auto validation = validator.Validate(candidate);
            if (validation != CatalogResult::Ok) return validation;
            if (!PreservesEntries()) return CatalogResult::Corrupt;
        }
        auto check = [&](uint32_t i, const uint8_t *row) { return validator.Extent(i, row); };
        auto source = [&](uint32_t at, uint8_t *out, uint32_t n) {
            return store.device.Read(candidate.address + at, out, n);
        };
        CatalogSelection published;
        reopen = true;
        const auto result = store.catalog.Publish(original, bytes, source, check, published, &crc);
        if (result != CatalogResult::Ok) return result;
        // The new root is already durable. A second copy retires the old
        // ownership, releasing victims and both old indexes after reopen.
        CatalogSelection mirror;
        const auto mirrored = store.catalog.Publish(published, bytes, source, check, mirror, &crc);
        return mirrored == CatalogResult::Ok ? CatalogResult::Ok : CatalogResult::Quarantined;
    }
};

} // namespace candidate_b
