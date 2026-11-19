// The input root owns every victim until its new journal and indexes publish.
#pragma once
#include "native_journal.h"

namespace candidate_b {
struct NativeRelocationMap {
    static constexpr uint32_t CopyBudget = 4096, Granularity = 32;
    static constexpr unsigned MaxVictims = 4, MaxMoves = CopyBudget / 64;
    struct Move { uint16_t source, offset; };
    Move moves[MaxMoves]{};
    uint8_t victims[MaxVictims]{};
    uint32_t destination = 0, cursor = kPageBytes, count = 0;
    uint64_t through = 0, epoch = 0;

    unsigned Victim(uint32_t segment) const {
        for (unsigned i = 0; i < MaxVictims; ++i)
            if (victims[i] == segment) return i;
        return MaxVictims;
    }
    bool Contains(uint32_t segment) const { return segment && Victim(segment) != MaxVictims; }
    uint32_t Resolve(uint32_t address) const {
        if (!destination) return address;
        const unsigned victim = Victim(address / kSegmentBytes);
        if (victim < MaxVictims) {
            const uint16_t key = (victim << 14) | ((address % kSegmentBytes) / 4);
            for (unsigned i = 0; i < count; ++i)
                if (moves[i].source == key) return destination * kSegmentBytes + moves[i].offset;
        }
        return address;
    }
    bool Append(uint32_t old_address, uint32_t span) {
        const unsigned victim = Victim(old_address / kSegmentBytes);
        if (victim >= MaxVictims || count >= MaxMoves || span > CopyBudget + kPageBytes - cursor)
            return false;
        const uint16_t key = (victim << 14) | ((old_address % kSegmentBytes) / 4);
        moves[count++] = {key, uint16_t(cursor)};
        cursor += span;
        return true;
    }
    void Reset(uint64_t highwater) {
        std::memset(victims,0,sizeof(victims));
        destination = count = 0;
        cursor = kPageBytes;
        through = highwater;
        epoch = 0;
    }
};
} // namespace candidate_b
