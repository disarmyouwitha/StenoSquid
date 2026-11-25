#pragma once
/* Transport-neutral binary frame codec for the experimental transfer session.
 *
 * Header-only, no Zephyr or heap dependencies, so USB HID, BLE HID, CDC and
 * host tests share one implementation. Nothing in firmware includes this yet
 * (Phase 2 prototype); see docs/transfer-session.md.
 *
 * Wire layout, little-endian:
 *   0  u8   magic 0xB5   (never ASCII, so it cannot collide with console text)
 *   1  u8   type
 *   2  u16  session id   (nonzero)
 *   4  u32  offset       (byte offset for DATA/END; next expected for replies)
 *   8  u16  payload length (<= kMaxPayload)
 *   10 ..   payload
 *   .. u32  CRC-32 (IEEE 802.3, as zlib) over every preceding byte
 *
 * Overhead is 14 bytes: 49 useful bytes in a 63-byte HID report (78%).
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace steno_transfer {

constexpr uint8_t kMagic = 0xB5;
constexpr size_t kHeaderBytes = 10;
constexpr size_t kTrailerBytes = 4;
constexpr size_t kMaxPayload = 240;
constexpr size_t kMaxFrameBytes = kHeaderBytes + kMaxPayload + kTrailerBytes;

enum FrameType : uint8_t {
    // Host -> keyboard
    kBegin = 0x01,
    kData = 0x02,
    kEnd = 0x03,
    kCommit = 0x04,
    kAbort = 0x05,
    // Keyboard -> host
    kAck = 0x81,
    kNak = 0x82,
    kError = 0x83,
    kDone = 0x84,
};

inline uint32_t Crc32Update(uint32_t crc, const uint8_t *data, size_t size) {
    // Nibble table: 64 bytes of flash instead of 1 KiB, fast enough for 250+ KiB/s.
    static constexpr uint32_t table[16] = {
        0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4,
        0x4DB26158, 0x5005713C, 0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C,
        0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C};
    crc = ~crc;
    for (size_t i = 0; i < size; i++) {
        crc ^= data[i];
        crc = (crc >> 4) ^ table[crc & 15];
        crc = (crc >> 4) ^ table[crc & 15];
    }
    return ~crc;
}

inline uint32_t Crc32(const uint8_t *data, size_t size) { return Crc32Update(0, data, size); }

inline void Put16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }
inline void Put32(uint8_t *p, uint32_t v) { Put16(p, v); Put16(p + 2, v >> 16); }
inline uint16_t Get16(const uint8_t *p) { return p[0] | (p[1] << 8); }
inline uint32_t Get32(const uint8_t *p) { return Get16(p) | (uint32_t(Get16(p + 2)) << 16); }

struct Frame {
    uint8_t type = 0;
    uint16_t session = 0;
    uint32_t offset = 0;
    uint16_t length = 0;
    const uint8_t *payload = nullptr; // Valid only until the next decoder call.
};

/* Returns the encoded size, or 0 if the frame is invalid or `capacity` is too small. */
inline size_t Encode(uint8_t type, uint16_t session, uint32_t offset, const uint8_t *payload,
                     size_t length, uint8_t *out, size_t capacity) {
    if (length > kMaxPayload || session == 0 || (length && !payload)) return 0;
    const size_t total = kHeaderBytes + length + kTrailerBytes;
    if (capacity < total) return 0;
    out[0] = kMagic;
    out[1] = type;
    Put16(out + 2, session);
    Put32(out + 4, offset);
    Put16(out + 8, static_cast<uint16_t>(length));
    if (length) memcpy(out + kHeaderBytes, payload, length);
    Put32(out + kHeaderBytes + length, Crc32(out, kHeaderBytes + length));
    return total;
}

/* Byte-stream decoder with resynchronisation. For report-based transports feed
 * each whole report (zero padding between frames is skipped). A corrupt frame
 * is dropped one byte at a time and rescanned, so a lost byte costs at most
 * the frames it touched and never stalls the stream. Fixed memory, no heap.
 */
class Decoder {
public:
    /* Append bytes; returns how many were consumed (may be fewer than `size`
     * when the buffer is full, in which case call Poll and push the remainder). */
    size_t Push(const uint8_t *data, size_t size) {
        Compact();
        const size_t room = sizeof(buffer) - end;
        const size_t count = size < room ? size : room;
        memcpy(buffer + end, data, count);
        end += count;
        return count;
    }

    /* Extract the next valid frame, if any. */
    bool Poll(Frame &frame) {
        for (;;) {
            while (start < end && buffer[start] != kMagic) { start++; skipped_++; }
            const size_t have = end - start;
            if (have < kHeaderBytes) return false;
            const uint8_t *p = buffer + start;
            const size_t length = Get16(p + 8);
            if (p[1] == 0 || Get16(p + 2) == 0 || length > kMaxPayload) {
                start++; skipped_++; continue;
            }
            const size_t total = kHeaderBytes + length + kTrailerBytes;
            if (have < total) return false;
            if (Crc32(p, total - kTrailerBytes) != Get32(p + total - kTrailerBytes)) {
                start++; skipped_++; crc_errors_++; continue;
            }
            frame.type = p[1];
            frame.session = Get16(p + 2);
            frame.offset = Get32(p + 4);
            frame.length = static_cast<uint16_t>(length);
            frame.payload = p + kHeaderBytes;
            start += total; // Payload stays intact until the next Push/Poll.
            return true;
        }
    }

    void Reset() { start = end = 0; }
    uint32_t skipped() const { return skipped_; }
    uint32_t crc_errors() const { return crc_errors_; }

private:
    void Compact() {
        if (start == 0) return;
        memmove(buffer, buffer + start, end - start);
        end -= start;
        start = 0;
    }
    uint8_t buffer[2 * kMaxFrameBytes];
    size_t start = 0, end = 0;
    uint32_t skipped_ = 0, crc_errors_ = 0;
};

} // namespace steno_transfer
