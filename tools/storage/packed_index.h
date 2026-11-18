// Candidate-B host/native experiment. Not linked into keyboard firmware.
#pragma once
#include <cstddef>
#include <cstdint>

namespace candidate_b {
constexpr uint32_t kMaxEntries = 1u << 20;
constexpr uint32_t kMaxCollisions = 16;
constexpr uint32_t kIndexStride = 12;
enum class Result { Missing, Found, Corrupt, IoError };

// Alternative: sort by the complete record key, storing only uint32 addresses.
// Compare returns stored-key vs query (-1, 0, 1), or 2 for a bad/unreadable record.
// This trades more full-key reads for 4 rather than 12 index bytes per outline,
// with no hash collision limit. Sorting/CRC/ownership are publication checks.
template <class Read, class Compare>
Result FindOrdered(uint32_t count, Read read, Compare compare, uint32_t &address) {
    if (count > kMaxEntries) return Result::Corrupt;
    uint32_t low = 0, high = count;
    while (low < high) {
        uint32_t mid = low + (high - low) / 2;
        uint8_t bytes[4];
        if (!read(mid * 4, bytes, sizeof(bytes))) return Result::IoError;
        uint32_t record = 0;
        for (unsigned i = 0; i < 4; ++i) record |= uint32_t(bytes[i]) << (8*i);
        const int order = compare(record);
        if (order == 2 || order < -1 || order > 1) return Result::Corrupt;
        if (!order) { address = record; return Result::Found; }
        if (order < 0) low = mid + 1;
        else high = mid;
    }
    return Result::Missing;
}

// A checkpoint may be followed by at most 256 committed edits. The writer must
// reserve a slot BEFORE committing an edit; on full, checkpoint or refuse safely.
// Addresses, not keys/text, are held in RAM. A snapshot copies this fixed state.
struct Tail {
    static constexpr uint32_t kCapacity = 256;
    uint32_t addresses[kCapacity]{};
    uint32_t count = 0;
    bool Append(uint32_t address) {
        if (count >= kCapacity) return false;
        addresses[count++] = address;
        return true;
    }
};

template <class Read, class Compare>
Result FindLatestOrdered(const Tail &tail, uint32_t count, Read read,
                         Compare compare, uint32_t &address) {
    if (tail.count > Tail::kCapacity) return Result::Corrupt;
    for (uint32_t i = tail.count; i > 0; --i) {
        const int order = compare(tail.addresses[i-1]);
        if (order < -1 || order > 1) return Result::Corrupt;
        if (!order) { address = tail.addresses[i-1]; return Result::Found; }
    }
    return FindOrdered(count, read, compare, address);
}

// The immutable table is sorted by (64-bit hash, record address), little endian.
// The caller validates table CRC/order/collision bounds before publication.
// Hash equality is never key equality: Match must check the complete record key.
// Each Read copies exactly 12 bytes; no index or dictionary mirror is required.
template <class Read, class Match>
Result Find(uint32_t count, uint64_t key, Read read, Match match,
            uint32_t &address) {
    if (count > kMaxEntries) return Result::Corrupt;
    uint32_t low = 0, high = count;
    uint8_t bytes[kIndexStride];
    auto load = [&](uint32_t index, uint64_t &hash, uint32_t &record) {
        if (!read(index * kIndexStride, bytes, kIndexStride)) return false;
        hash = 0;
        record = 0;
        for (unsigned i = 0; i < 8; ++i) hash |= uint64_t(bytes[i]) << (8 * i);
        for (unsigned i = 0; i < 4; ++i) record |= uint32_t(bytes[8+i]) << (8*i);
        return true;
    };
    while (low < high) {
        uint32_t mid = low + (high - low) / 2, record;
        uint64_t hash;
        if (!load(mid, hash, record)) return Result::IoError;
        if (hash < key) low = mid + 1;
        else high = mid;
    }
    for (uint32_t collision = 0; low < count; ++collision, ++low) {
        uint64_t hash;
        uint32_t record;
        if (!load(low, hash, record)) return Result::IoError;
        if (hash != key) return Result::Missing;
        if (collision == kMaxCollisions) return Result::Corrupt;
        // -1: unreadable/malformed record; 0: different full key; 1: exact key.
        int matched = match(record);
        if (matched < 0) return Result::Corrupt;
        if (matched > 0) { address = record; return Result::Found; }
    }
    return Result::Missing;
}

template <class Read, class Match>
Result FindLatest(const Tail &tail, uint32_t count, uint64_t key, Read read,
                  Match match, uint32_t &address) {
    if (tail.count > Tail::kCapacity) return Result::Corrupt;
    for (uint32_t i = tail.count; i > 0; --i) {
        int matched = match(tail.addresses[i-1]);
        if (matched < 0) return Result::Corrupt;
        if (matched > 0) { address = tail.addresses[i-1]; return Result::Found; }
    }
    return Find(count, key, read, match, address);
}
}  // namespace candidate_b
