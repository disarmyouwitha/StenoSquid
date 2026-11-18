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

// Read exact-key metadata without a text buffer or editable-dictionary RAM mirror.
// Return -1 for malformed/unreadable, 0 for another key, 1 for an exact key.
// The enclosing committed record supplies payload_size, already CRC-validated.
template <class Read>
int MatchOutline(uint32_t address, uint32_t payload_size, uint32_t identity,
                 const uint32_t *strokes, uint8_t count, Read read, RecordView &view) {
    if (!strokes || count < 1 || count > 16 || payload_size < 8 ||
        payload_size > 8 + 64 + 2047 || address > UINT32_MAX - payload_size) return -1;
    uint8_t header[8];
    if (!read(address, header, sizeof(header))) return -1;
    uint32_t stored_id = 0;
    for (unsigned i = 0; i < 4; ++i) stored_id |= uint32_t(header[i]) << (8*i);
    uint8_t op = header[4], stored_count = header[5];
    uint16_t length = uint16_t(header[6]) | uint16_t(header[7]) << 8;
    if (!stored_id || op > 2 || stored_count < 1 || stored_count > 16 ||
        length > 2047 || (op && length) ||
        payload_size != 8u + 4u * stored_count + length) return -1;
    if (stored_id != identity || stored_count != count) return 0;
    for (uint8_t i = 0; i < count; ++i) {
        uint8_t bytes[4];
        if (!read(address + 8 + 4*i, bytes, sizeof(bytes))) return -1;
        uint32_t mask = 0;
        for (unsigned j = 0; j < 4; ++j) mask |= uint32_t(bytes[j]) << (8*j);
        if (!mask || mask >> 25) return -1;
        if (mask != strokes[i]) return 0;
    }
    view = {static_cast<Operation>(op), address + 8u + 4u * count, length};
    return 1;
}
}  // namespace candidate_b
