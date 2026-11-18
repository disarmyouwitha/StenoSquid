// Candidate-B host/native experiment. Container CRC/commit validation is separate.
#pragma once
#include <cstdint>

namespace candidate_b {
enum class Operation : uint8_t { Set, Remove, Reset };
struct RecordView {
    Operation operation;
    uint32_t text_address;
    uint16_t text_bytes;
};

// Read/compare key metadata without a text buffer or dictionary RAM mirror.
// Return stored-key ordering (-1, 0, 1), or 2 for malformed/unreadable records.
// The enclosing committed record supplies payload_size, already CRC-validated.
template <class Read>
int CompareOutline(uint32_t address, uint32_t payload_size, uint32_t identity,
                 const uint32_t *strokes, uint8_t count, Read read, RecordView &view) {
    if (!strokes || count < 1 || count > 16 || payload_size < 8 ||
        payload_size > 8 + 64 + 2047 || address > UINT32_MAX - payload_size) return 2;
    uint8_t header[8];
    if (!read(address, header, sizeof(header))) return 2;
    uint32_t stored_id = 0;
    for (unsigned i = 0; i < 4; ++i) stored_id |= uint32_t(header[i]) << (8*i);
    uint8_t op = header[4], stored_count = header[5];
    uint16_t length = uint16_t(header[6]) | uint16_t(header[7]) << 8;
    if (!stored_id || op > 2 || stored_count < 1 || stored_count > 16 ||
        length > 2047 || (op && length) ||
        payload_size != 8u + 4u * stored_count + length) return 2;
    if (stored_id != identity) return stored_id < identity ? -1 : 1;
    for (uint8_t i = 0; i < count && i < stored_count; ++i) {
        uint8_t bytes[4];
        if (!read(address + 8 + 4*i, bytes, sizeof(bytes))) return 2;
        uint32_t mask = 0;
        for (unsigned j = 0; j < 4; ++j) mask |= uint32_t(bytes[j]) << (8*j);
        if (!mask || mask >> 25) return 2;
        if (mask != strokes[i]) return mask < strokes[i] ? -1 : 1;
    }
    if (stored_count != count) return stored_count < count ? -1 : 1;
    view = {static_cast<Operation>(op), address + 8u + 4u * count, length};
    return 0;
}

template <class Read>
int MatchOutline(uint32_t address, uint32_t payload_size, uint32_t identity,
                 const uint32_t *strokes, uint8_t count, Read read, RecordView &view) {
    const int order = CompareOutline(address, payload_size, identity, strokes, count, read, view);
    return order == 2 ? -1 : order == 0 ? 1 : 0;
}

// Lexicographic UTF-8 byte comparison in fixed 32-byte chunks. Payload/container
// CRC and UTF-8 validation happen before publication/open; no Unicode font or
// decoding is needed to search an exact stored translation.
template <class Read>
int CompareText(uint32_t address, uint32_t payload_size, const uint8_t *query,
                uint16_t query_bytes, Read read) {
    if ((!query && query_bytes) || query_bytes > 2047 || payload_size < 8 ||
        payload_size > 8 + 64 + 2047 || address > UINT32_MAX - payload_size) return 2;
    uint8_t bytes[32];
    if (!read(address, bytes, 8)) return 2;
    uint32_t id = 0;
    for (unsigned i = 0; i < 4; ++i) id |= uint32_t(bytes[i]) << (8*i);
    const uint8_t count = bytes[5];
    const uint16_t length = uint16_t(bytes[6]) | uint16_t(bytes[7]) << 8;
    if (!id || bytes[4] != 0 || count < 1 || count > 16 || length > 2047 ||
        payload_size != 8u + 4u * count + length) return 2;
    uint32_t at = address + 8u + 4u * count;
    uint16_t common = length < query_bytes ? length : query_bytes;
    for (uint16_t offset = 0; offset < common;) {
        uint16_t n = common - offset;
        if (n > sizeof(bytes)) n = sizeof(bytes);
        if (!read(at + offset, bytes, n)) return 2;
        for (uint16_t i = 0; i < n; ++i)
            if (bytes[i] != query[offset+i]) return bytes[i] < query[offset+i] ? -1 : 1;
        offset += n;
    }
    return length == query_bytes ? 0 : length < query_bytes ? -1 : 1;
}
}  // namespace candidate_b
