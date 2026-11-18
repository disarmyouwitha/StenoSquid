// Freestanding ARM compile/stack probe only; this is not a flashable image.
#include "packed_index.h"
#include "binary_record.h"

using Read = bool (*)(uint32_t, uint8_t *, size_t);
using Match = int (*)(uint32_t);
static_assert(sizeof(candidate_b::Tail) == 1028);

extern "C" candidate_b::Result ProbeFind(const candidate_b::Tail &tail,
        uint32_t count, uint64_t hash, Read read, Match match, uint32_t &address) {
    return candidate_b::FindLatest(tail, count, hash, read, match, address);
}

extern "C" int ProbeRecord(uint32_t address, uint32_t size, uint32_t identity,
        const uint32_t *strokes, uint8_t count, Read read,
        candidate_b::RecordView &view) {
    return candidate_b::MatchOutline(address, size, identity, strokes, count, read, view);
}

extern "C" candidate_b::Result ProbeOrdered(const candidate_b::Tail &tail, uint32_t count, Read read,
        Match compare, uint32_t &address) {
    return candidate_b::FindLatestOrdered(tail, count, read, compare, address);
}
