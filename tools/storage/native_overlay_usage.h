#pragma once
#include "native_live_journal.h"

namespace candidate_b {
struct OverlayUsage {
    uint32_t count = 0, entries = 0, bytes = 0;
};

// Caller holds the flash lock and freezes writes between slices. Count the
// checkpoint plus the latest tail edits without exporting translation text.
template<class Device, class Emit, class Stop>
bool MeasureOverlay(Device &device, const LiveView &view, uint32_t &position,
                    Emit emit, Stop stop) {
    NativeJournal<Device> journal(device);
    auto read_key = [&](uint32_t address, bool indexed, JournalRecord &record, JournalKey &key) {
        return address < kLargePoolEnd * kSegmentBytes && !(address % 4) &&
               view.journals.Contains(address / kSegmentBytes) &&
               address % kSegmentBytes >= 256 &&
               journal.ReadKey(address, record, key) == JournalResult::Ok &&
               record.sequence <= view.highwater && (record.sequence <= view.through) == indexed;
    };
    const uint32_t total = view.forward_count + view.tail.count;
    if (position > total) return false;
    for (unsigned examined = 0; position < total && examined < 128; ++examined, ++position) {
        if (examined && stop()) break;
        const bool indexed = position < view.forward_count;
        uint32_t address;
        if (indexed) {
            uint8_t raw[4];
            if (!device.Read(view.forward_address + position * 4, raw, sizeof(raw))) return false;
            address = Word(raw);
        } else {
            address = view.tail.addresses[position - view.forward_count];
        }
        JournalRecord record;
        JournalKey key;
        if (!read_key(address, indexed, record, key)) return false;
        unsigned slot = 0;
        while (slot < view.dictionary_count && view.identities[slot] != key.identity) ++slot;
        if (slot == view.dictionary_count || key.operation == 2) continue;

        // The tail is sorted by key and then append order. Its last matching
        // address shadows the checkpoint and all earlier edits for the key.
        uint32_t low = indexed ? 0 : position - view.forward_count + 1;
        uint32_t high = view.tail.count;
        const uint32_t first = low;
        JournalRecord recent;
        JournalKey recent_key;
        while (low < high) {
            const uint32_t mid = low + (high - low) / 2;
            if (!read_key(view.tail.addresses[mid], false, recent, recent_key)) return false;
            if (CompareOutlineKey(recent_key, key.identity, key.strokes, key.count) <= 0) low = mid + 1;
            else high = mid;
        }
        if (low > first) {
            if (!read_key(view.tail.addresses[low - 1], false, recent, recent_key)) return false;
            if (!CompareOutlineKey(recent_key, key.identity, key.strokes, key.count)) continue;
        }
        if (!emit(slot, address / kSegmentBytes, key.operation, record.span)) break;
    }
    return true;
}
} // namespace candidate_b
