// Freestanding ARM compile/stack probe only; this is not a flashable image.
#include "packed_index.h"
#include "binary_record.h"
#include "reverse_cursor.h"

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

extern "C" int ProbeText(uint32_t address, uint32_t size, const uint8_t *text,
        uint16_t length, Read read) {
    return candidate_b::CompareText(address, size, text, length, read);
}

extern "C" candidate_b::Result ProbeSeek(uint32_t count, Read read, Match compare,
        candidate_b::ReverseCursor &cursor) {
    return candidate_b::SeekReverse(count, read, compare, cursor);
}

extern "C" candidate_b::PageResult ProbePage(candidate_b::ReverseCursor &cursor,
        uint32_t budget, Read read, Match compare, Match accept, void (*emit)(uint32_t)) {
    return candidate_b::NextReverse(cursor, budget, read, compare, accept, emit);
}
