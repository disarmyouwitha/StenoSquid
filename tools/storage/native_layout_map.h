#pragma once
#include "native_catalog.h"

namespace candidate_b {
// Uses the immutable roots already validated by Open/Publish. Only catalog
// descriptors are read. '.' is unreferenced by this root, never a flash claim.
template<class Device, class Stop>
bool ReadLayout(Device &device, const CatalogRoot &root, char *segments, Stop stop, unsigned first = 0) {
    uint8_t prefix[32];
    if (!root.generation || stop() || !device.Read(root.address, prefix, sizeof(prefix))) return false;
    const unsigned rows = prefix[4] | prefix[5] << 8, count = prefix[6] | prefix[7] << 8;
    if (rows > 64 || count > 253 || root.bytes != 32 + rows * 96 + count * 24) return false;
    auto extent = [&](unsigned i, uint8_t (&value)[24]) {
        return !stop() && i < count && device.Read(root.address + 32 + rows * 96 + i * 24, value, 24);
    };
    constexpr unsigned page_size = 256;
    const unsigned end = NativePoolEnd(device);
    if (first % page_size || first > end) return false;
    const unsigned after = first + page_size;
    auto span = [&](const uint8_t *value, unsigned &start, unsigned &size) {
        start = value[0] | value[1] << 8;
        size = value[2] | value[3] << 8;
        return start >= 2 && start < end && size && size <= end - start;
    };
    std::memset(segments, '.', page_size);
    if (!first) segments[0] = segments[1] = 'c';
    if (end < after) segments[end - first] = 'x';
    unsigned head = 0;
    for (unsigned i = 0; i < count; ++i) {
        uint8_t value[24];
        unsigned start, size;
        if (!extent(i, value) || !span(value, start, size)) return false;
        const auto kind = Word(value + 12);
        if (!kind || kind > 4) return false;
        for (unsigned segment = start > first ? start : first; segment < start + size && segment < after; ++segment) {
            if (segments[segment - first] != '.') return false;
            segments[segment - first] = ".bjfr"[kind];
        }
        if (kind == 2) head = start;
    }
    if (head && head >= first && head < after) segments[head - first] = 'h';
    for (unsigned i = 0; i < rows; ++i) {
        uint8_t base[4];
        if (stop() || !device.Read(root.address + 32 + i * 96 + 8, base, 4)) return false;
        if (!base[3]) continue;
        if (base[3] == kFactoryAbsent) {
            if (base[0] != 255 || base[1] != 255 || base[2]) return false;
            continue;
        }
        if (base[3] != kUserSnapshot) return false;
        uint8_t value[24];
        unsigned start, size;
        if (!extent(base[0] | base[1] << 8, value) || Word(value + 12) != 1 ||
            !span(value, start, size)) return false;
        for (unsigned segment = start > first ? start : first; segment < start + size && segment < after; ++segment)
            segments[segment - first] = 's';
    }
    return true;
}
} // namespace candidate_b
