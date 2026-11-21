#pragma once
#include "native_store.h"
#include "native_validator.h"

namespace candidate_b {

enum class NativeUpdateKind {
    Compact, Personal, Named, Reorder, Rename, Delete, Backup, Base,
    SnapshotNamed, SnapshotConsolidate, SnapshotDelete, SnapshotPersonal, SnapshotClear, FactoryPackage
};

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
    uint32_t personal_id = 0, added_id = 0, snapshot_id = 0;
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

    static bool EditableRow(const uint8_t *row, bool rename) {
        const bool journal = row[8] == 255 && row[9] == 255;
        return row[11] != kFactoryAbsent && (journal || (rename && row[11] == kUserSnapshot)) &&
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
                if (!rename || renamed || !EditableRow(old_row, true) || !ValidName(new_row) ||
                    !NameAvailable(original.root, dictionaries, new_row) ||
                    std::memcmp(old_row, new_row, 12) ||
                    std::memcmp(old_row + 13, new_row + 13, 3) ||
                    std::memcmp(old_row + 60, new_row + 60, 36)) return false;
                renamed = true;
                continue;
            }
            if (std::memcmp(old_row, new_row, 8) || std::memcmp(old_row + 10, new_row + 10, 86))
                return false;
            if (old_row[12] == 15 && !std::memcmp(old_row + 16, "user_dictionary", 15))
                personal_id = identity;
            const uint16_t old_base = old_row[8] | old_row[9] << 8;
            if (old_base == kNoExtent) {
                if (new_base != kNoExtent) return false;
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
                        !EditableRow(row, false)) return false;
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
    bool StagedExtent(const uint8_t *extent, uint32_t metadata, uint32_t kind) {
        const unsigned start = extent[0] | extent[1] << 8, count = extent[2] | extent[3] << 8;
        const uint32_t bytes = Word(extent + 4);
        if (Word(extent + 12) != kind || start < 2 || start >= 255 || !count || count > 255 - start ||
            !bytes || bytes % 4 || bytes > count * kSegmentBytes || (kind == 1 && bytes < 40))
            return false;
        for (unsigned i = start; i < start + count; ++i)
            if (i == metadata || !erased.Contains(i)) return false;
        return true;
    }

    bool CheckSnapshotChange(const CatalogRoot &candidate, uint32_t metadata, BaseProbe probe) {
        const bool deleting = kind == NativeUpdateKind::SnapshotDelete;
        const bool clearing = kind == NativeUpdateKind::SnapshotClear;
        const bool converting = kind == NativeUpdateKind::SnapshotPersonal;
        const bool consolidating = converting || clearing || kind == NativeUpdateKind::SnapshotConsolidate;
        uint8_t before[32], after[32], target[96];
        if (!snapshot_id || (!deleting && !clearing && !probe) ||
            !store.device.Read(original.root.address, before, 32) ||
            !store.device.Read(candidate.address, after, 32))
            return false;
        const unsigned rows = before[4] | before[5] << 8, extents = before[6] | before[7] << 8;
        const unsigned new_rows = after[4] | after[5] << 8, new_extents = after[6] | after[7] << 8;
        const uint64_t through = uint64_t(Word(after + 12)) << 32 | Word(after + 8);
        if (Word(before) != Word(after) || new_rows + deleting != rows || new_extents > 253 ||
            candidate.bytes != 32 + 96 * new_rows + 24 * new_extents ||
            (consolidating ? through != store.live.Highwater() : std::memcmp(before + 8, after + 8, 8) != 0) ||
            std::memcmp(before + 16, after + 16, 16))
            return false;
        unsigned target_index = 0;
        for (; target_index < rows; ++target_index) {
            if (!store.Row(target_index, target)) return false;
            if (Word(target) == snapshot_id) break;
        }
        if (target_index == rows) return false;
        const bool personal = target[12] == 15 && !std::memcmp(target + 16, "user_dictionary", 15);
        if ((clearing ? !personal : converting ? !personal || target[11] : target[11] != kUserSnapshot) ||
            (deleting && personal))
            return false;
        const unsigned old_base = target[8] | target[9] << 8;
        if ((converting || (clearing && !target[11])) ? old_base != kNoExtent : old_base >= extents) return false;
        uint8_t remap[253]{};
        unsigned kept = 0;
        for (unsigned i = 0; i < extents; ++i) {
            uint8_t old_extent[24], new_extent[24];
            if (!ReadExtent(original.root, rows, i, old_extent)) return false;
            if (i == old_base || (consolidating && Word(old_extent + 12) >= 3)) continue;
            if (kept >= new_extents || !ReadExtent(candidate, new_rows, kept, new_extent) ||
                std::memcmp(old_extent, new_extent, sizeof(old_extent)))
                return false;
            remap[i] = kept++;
        }
        const unsigned new_base = kept;
        if (!deleting && !clearing) {
            uint8_t extent[24];
            target[11] = kUserSnapshot;
            if (kept >= new_extents || !ReadExtent(candidate, new_rows, kept++, extent) ||
                !StagedExtent(extent, metadata, 1) || !validator.Extent(new_base, extent) || !probe(extent, target))
                return false;
        }
        if (consolidating) {
            uint32_t last_kind = 2;
            for (; kept < new_extents; ++kept) {
                uint8_t extent[24];
                if (!ReadExtent(candidate, new_rows, kept, extent)) return false;
                const uint32_t type = Word(extent + 12);
                if (type <= last_kind || type > 4 || !StagedExtent(extent, metadata, type)) return false;
                last_kind = type;
            }
        }
        if (kept != new_extents) return false;
        for (unsigned i = 0, j = 0; i < rows; ++i) {
            if (i == target_index && deleting) continue;
            uint8_t prior[96], updated[96];
            if (!store.Row(i, prior) ||
                !store.device.Read(candidate.address + 32 + (j++) * 96, updated, 96)) return false;
            const unsigned base = prior[8] | prior[9] << 8;
            if (i == target_index) {
                prior[8] = clearing ? 255 : new_base;
                prior[9] = clearing ? 255 : 0;
                if (clearing) prior[10] = 0;
                prior[11] = clearing ? 0 : kUserSnapshot;
            } else if (base != kNoExtent) {
                if (base >= extents || base == old_base) return false;
                prior[8] = remap[base];
            }
            if (std::memcmp(prior, updated, sizeof(prior))) return false;
        }
        return true;
    }

    bool CheckNewSnapshot(const CatalogRoot &candidate, uint32_t metadata, BaseProbe probe) {
        if (!probe || candidate.bytes != original.root.bytes + 96 + 24)
            return false;
        uint8_t before[32], after[32];
        if (!store.device.Read(original.root.address, before, 32) ||
            !store.device.Read(candidate.address, after, 32) || std::memcmp(before + 8, after + 8, 24))
            return false;
        const uint32_t id = Word(before);
        const unsigned rows = before[4] | before[5] << 8;
        const unsigned extents = before[6] | before[7] << 8;
        if (id == UINT32_MAX || Word(after) != id + 1 || rows >= dictionary_limit || extents >= 253 ||
            (after[4] | unsigned(after[5]) << 8) != rows + 1 ||
            (after[6] | unsigned(after[7]) << 8) != extents + 1)
            return false;
        for (unsigned i = 0; i < extents; ++i) {
            uint8_t old_extent[24], new_extent[24];
            if (!ReadExtent(original.root, rows, i, old_extent) ||
                !ReadExtent(candidate, rows + 1, i, new_extent) || std::memcmp(old_extent, new_extent, 24))
                return false;
        }
        uint8_t found[96]{};
        for (unsigned i = 0, old_index = 0; i <= rows; ++i) {
            uint8_t row[96];
            if (!store.device.Read(candidate.address + 32 + i * 96, row, 96)) return false;
            if (Word(row) == id) {
                if (Word(found) || Word(row + 4) != 1 || row[10] || row[11] != kUserSnapshot ||
                    (row[8] | unsigned(row[9]) << 8) != extents || !ValidName(row) ||
                    !NameAvailable(original.root, rows, row))
                    return false;
                std::memcpy(found, row, sizeof(found));
            } else {
                uint8_t old_row[96];
                if (old_index >= rows ||
                    !store.device.Read(original.root.address + 32 + old_index++ * 96, old_row, 96) ||
                    std::memcmp(old_row, row, sizeof(row)))
                    return false;
            }
        }
        uint8_t extent[24];
        if (!Word(found) || !ReadExtent(candidate, rows + 1, extents, extent) || Word(extent + 12) != 1)
            return false;
        return StagedExtent(extent, metadata, 1) && validator.Extent(extents, extent) && probe(extent, found);
    }

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

    bool CheckFactoryPackage(const CatalogRoot &candidate, uint32_t metadata, BaseProbe probe) {
        if (snapshot_id != 6 && snapshot_id != 7) return false;
        uint8_t before[32], after[32];
        if (!store.device.Read(original.root.address, before, sizeof(before)) ||
            !store.device.Read(candidate.address, after, sizeof(after)) ||
            std::memcmp(before, after, 6) || std::memcmp(before + 8, after + 8, 24)) return false;
        const unsigned dictionaries = before[4] | before[5] << 8;
        const unsigned old_count = before[6] | before[7] << 8;
        const unsigned new_count = after[6] | after[7] << 8;
        const bool restore = new_count == old_count + 1;
        if ((!restore && old_count != new_count + 1) || old_count > 253 || new_count > 253 ||
            candidate.bytes != 32 + dictionaries * 96u + new_count * 24u) return false;
        const unsigned first_id = snapshot_id == 6 ? 2 : 7;
        unsigned package_extent = restore ? old_count : kNoExtent;
        unsigned seen = 0;
        uint8_t extent[24]{};
        if (restore) {
            if (!probe || !ReadExtent(candidate, dictionaries, package_extent, extent) ||
                Word(extent + 12) != 1 || Word(extent + 16) || Word(extent + 20)) return false;
            const unsigned start = extent[0] | extent[1] << 8;
            const unsigned count = extent[2] | extent[3] << 8;
            const uint32_t bytes = Word(extent + 4);
            if (start < 2 || start >= 255 || !count || count > 255 - start ||
                bytes < 40 || bytes > count * kSegmentBytes || bytes % 4) return false;
            for (unsigned segment = start; segment < start + count; ++segment)
                if (segment == metadata || !erased.Contains(segment)) return false;
            uint8_t header[8];
            if (!store.device.Read(start * kSegmentBytes, header, sizeof(header)) ||
                Word(header) != 0x3443534a || Word(header + 4) != snapshot_id - first_id + 1) return false;
        }
        for (unsigned i = 0; i < dictionaries; ++i) {
            uint8_t old_row[96], new_row[96];
            if (!store.device.Read(original.root.address + 32 + i * 96u, old_row, sizeof(old_row)) ||
                !store.device.Read(candidate.address + 32 + i * 96u, new_row, sizeof(new_row))) return false;
            const uint32_t id = Word(old_row);
            if (id < first_id || id > snapshot_id) continue;
            const unsigned member = id - first_id;
            const char *name = FactoryName(id);
            if (old_row[12] != std::strlen(name) || std::memcmp(old_row + 16, name, old_row[12]) ||
                (seen & (1u << member))) return false;
            seen |= 1u << member;
            const unsigned old_base = old_row[8] | old_row[9] << 8;
            if (restore) {
                if (old_row[11] != kFactoryAbsent || old_base != kNoExtent) return false;
                old_row[8] = uint8_t(package_extent);
                old_row[9] = uint8_t(package_extent >> 8);
                old_row[10] = uint8_t(member);
                old_row[11] = 0;
                bool has_pin = false;
                for (unsigned j = 64; j < sizeof(new_row); ++j) has_pin |= new_row[j] != 0;
                if (!has_pin) return false;
                std::memcpy(old_row + 64, new_row + 64, 32);
            } else {
                if (old_row[11] || old_row[10] != member || old_base >= old_count) return false;
                if (package_extent == kNoExtent) package_extent = old_base;
                if (old_base != package_extent) return false;
                old_row[4] = 0;
                old_row[8] = old_row[9] = 255;
                old_row[10] = 0;
                old_row[11] = kFactoryAbsent;
            }
            if (std::memcmp(old_row, new_row, sizeof(old_row)) || (restore && !probe(extent, new_row))) return false;
        }
        if (seen != (1u << (snapshot_id - first_id + 1)) - 1) return false;
        for (unsigned i = 0; i < dictionaries; ++i) {
            uint8_t old_row[96], new_row[96];
            if (!store.device.Read(original.root.address + 32 + i * 96u, old_row, sizeof(old_row)) ||
                !store.device.Read(candidate.address + 32 + i * 96u, new_row, sizeof(new_row))) return false;
            const uint32_t id = Word(old_row);
            if (id >= first_id && id <= snapshot_id) continue;
            unsigned base = old_row[8] | old_row[9] << 8;
            if (!restore && base != kNoExtent) {
                if (base == package_extent) return false;
                if (base > package_extent) --base;
                old_row[8] = uint8_t(base);
                old_row[9] = uint8_t(base >> 8);
            }
            if (std::memcmp(old_row, new_row, sizeof(old_row))) return false;
        }
        for (unsigned i = 0, j = 0; i < old_count; ++i) {
            if (!restore && i == package_extent) continue;
            uint8_t old_extent[24], new_extent[24];
            if (!ReadExtent(original.root, dictionaries, i, old_extent) ||
                !ReadExtent(candidate, dictionaries, j++, new_extent) ||
                std::memcmp(old_extent, new_extent, sizeof(old_extent))) return false;
        }
        return true;
    }

    bool PreservesEntries() {
        const bool merging_personal = kind == NativeUpdateKind::Personal;
        const auto folded = [&](const MergeRecord &entry) {
            return entry.identity == snapshot_id && (kind == NativeUpdateKind::SnapshotConsolidate ||
                   kind == NativeUpdateKind::SnapshotClear ||
                   (kind == NativeUpdateKind::SnapshotPersonal && entry.operation == 0));
        };
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
            if (folded(entry)) continue;
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
            if (folded(entry)) return false;
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
    NativeUpdateKind Kind() const { return kind; }
    const SegmentMap &Protected() const { return protected_segments; }
    const CatalogRoot &Root() const { return original.root; }

    bool Begin(NativeUpdateKind operation, uint16_t limit = 64, uint32_t id = 0) {
        if (limit > 64 ||
            (bool(id) != (operation >= NativeUpdateKind::SnapshotConsolidate))) return false;
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
        snapshot_id = id;
        added_id = personal_id = 0;
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
        const bool adding_snapshot = kind == NativeUpdateKind::SnapshotNamed;
        const bool changing_snapshot = kind >= NativeUpdateKind::SnapshotConsolidate && kind <= NativeUpdateKind::SnapshotClear;
        const bool factory = kind == NativeUpdateKind::FactoryPackage;
        if (checksum != crc) return CatalogResult::Corrupt;
        const bool layout_ok = factory ? CheckFactoryPackage(candidate, segment, probe) :
            adding_snapshot ? CheckNewSnapshot(candidate, segment, probe) :
            changing_snapshot ? CheckSnapshotChange(candidate, segment, probe) :
            replacing_base ? CheckBaseLayout(candidate, segment, probe) : CheckLayout(candidate, segment);
        if (!layout_ok) return CatalogResult::Corrupt;
        if (kind == NativeUpdateKind::SnapshotConsolidate || kind == NativeUpdateKind::SnapshotPersonal ||
            kind == NativeUpdateKind::SnapshotClear ||
            (!MetadataOnly() && !replacing_base && !adding_snapshot && !changing_snapshot && !factory)) {
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
