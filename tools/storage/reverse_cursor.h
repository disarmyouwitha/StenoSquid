// Candidate-B native experiment. Caller pins the root/table/records for cursor life.
#pragma once
#include "packed_index.h"

namespace candidate_b {
enum class PageResult { More, Done, Corrupt, IoError };
struct ReverseCursor {
    uint32_t next = 0, end = 0;
    bool ready = false;
};

template <class Read>
bool ReadAddress(uint32_t index, Read read, uint32_t &address) {
    uint8_t bytes[4];
    if (!read(index * 4, bytes, sizeof(bytes))) return false;
    address = 0;
    for (unsigned i = 0; i < 4; ++i) address |= uint32_t(bytes[i]) << (8*i);
    return true;
}

// Reverse table is sorted by (UTF-8 translation bytes, dictionary ID, outline).
// Compare returns record text vs query (-1/0/1), or 2 on malformed/unreadable.
template <class Read, class Compare>
Result SeekReverse(uint32_t count, Read read, Compare compare, ReverseCursor &cursor) {
    cursor = {};
    if (count > kMaxEntries) return Result::Corrupt;
    uint32_t low = 0, high = count;
    while (low < high) {
        uint32_t mid = low + (high - low) / 2, address;
        if (!ReadAddress(mid, read, address)) return Result::IoError;
        int order = compare(address);
        if (order < -1 || order > 1) return Result::Corrupt;
        if (order < 0) low = mid + 1;
        else high = mid;
    }
    cursor = {low, count, true};
    return Result::Found;
}

// Budget limits candidates examined, including stale/shadowed entries that yield
// no result. Accept verifies effective edit address, enabled state and winner;
// it returns -1 on failure, 0 to skip, 1 to emit. Emit capacity must be >= budget.
// No duplicate-group cap: arbitrarily many legitimate results span more pages.
// On error, discard that page's emitted buffer; the cursor stays unchanged.
template <class Read, class Compare, class Accept, class Emit>
PageResult NextReverse(ReverseCursor &cursor, uint32_t budget, Read read,
                       Compare compare, Accept accept, Emit emit) {
    if (!cursor.ready || cursor.end > kMaxEntries || cursor.next > cursor.end ||
        !budget || budget > 256) return PageResult::Corrupt;
    uint32_t next = cursor.next;
    for (uint32_t work = 0; work < budget && next < cursor.end; ++work) {
        uint32_t address;
        if (!ReadAddress(next, read, address)) return PageResult::IoError;
        const int order = compare(address);
        if (order < 0 || order > 1) return PageResult::Corrupt;
        if (order > 0) { cursor.next = cursor.end; return PageResult::Done; }
        const int accepted = accept(address);
        if (accepted < 0 || accepted > 1) return PageResult::Corrupt;
        if (accepted) emit(address);
        ++next;
    }
    cursor.next = next;
    return cursor.next == cursor.end ? PageResult::Done : PageResult::More;
}
} // namespace candidate_b
