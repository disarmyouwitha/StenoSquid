// Small, optional evacuation pass used by idle checkpoints. The input root
// keeps ownership of every victim until the new journal and indexes publish.
#pragma once
#include "native_journal.h"
#include "native_allocator.h"

namespace candidate_b {
struct NativeRelocationMap {
    static constexpr uint32_t CopyBudget = 4096;
    struct Move { uint32_t old_address, new_address; };
    Move moves[CopyBudget / 44]{};
    SegmentMap victims{};
    uint32_t destination = 0, cursor = kPageBytes, count = 0;
    uint64_t through = 0, epoch = 0;

    uint32_t Resolve(uint32_t address) const {
        if (victims.Contains(address / kSegmentBytes))
            for (unsigned i = 0; i < count; ++i)
                if (moves[i].old_address == address) return moves[i].new_address;
        return address;
    }
    bool Append(uint32_t old_address, uint32_t span) {
        if (count >= CopyBudget / 44 || span > CopyBudget + kPageBytes - cursor)
            return false;
        moves[count++] = {old_address, destination * kSegmentBytes + cursor};
        cursor += span;
        return true;
    }
    void Reset(uint64_t highwater) {
        victims = {};
        destination = count = 0;
        cursor = kPageBytes;
        through = highwater;
        epoch = 0;
    }
};
} // namespace candidate_b
