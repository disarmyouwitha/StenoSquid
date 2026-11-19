#pragma once

#include <cstddef>
#include <cstdint>

// Reads legacy SQS1 trailers. New JSC4 images keep SHI1 only.
class StenoSquidCompactSearch {
public:
    using WordReader = uint32_t (*)(const void *);
    using ByteReader = uint8_t (*)(const void *);
    struct Entry { const uint8_t *text = nullptr; uint32_t strokes[16]{}; unsigned length = 0; };
    struct Range { uint32_t begin = 0, end = 0; };

    StenoSquidCompactSearch(uintptr_t image, uint32_t bytes, uintptr_t text, uint32_t textBytes,
                            WordReader word, ByteReader byte)
        : image(image), text(text), word(word), byte(byte), textBytes(textBytes), legacy(bytes) {
        if (bytes < 60 || bytes % 4 || text < image || text - image > bytes) return;
        const uintptr_t footer = image + bytes - 20;
        if (Word(footer) != 0x31535153) return; // SQS1
        const uint32_t previous = Word(footer + 4);
        const uint32_t reverse = Word(footer + 8), buckets = Word(footer + 12), posts = Word(footer + 16);
        if (previous < 40 || previous % 4 || previous > bytes - 20 || text - image > previous ||
            textBytes > previous - (text - image) || !reverse ||
            uint64_t(previous) + 4ull * (uint64_t(reverse) + 2ull * buckets + posts) != bytes - 20)
            return;
        legacy = previous;
        reverseCount = reverse;
        bucketCount = buckets;
        postingCount = posts;
    }
    bool Available() const { return reverseCount != 0; }
    uint32_t LegacyBytes() const { return legacy; }

    bool TextRange(const char *query, size_t length, bool prefix, Range &range) const {
        if (!Available() || length > 2047) return false;
        auto boundary = [&](bool upper, uint32_t &position) {
            uint32_t first = 0, last = reverseCount;
            while (first < last) {
                const uint32_t mid = first + (last - first) / 2;
                const int result = Compare(mid, query, length, prefix);
                if (result == 2) return false;
                if (result < 0 || (upper && !result)) first = mid + 1;
                else last = mid;
            }
            position = first;
            return true;
        };
        return boundary(false, range.begin) && boundary(true, range.end);
    }

    bool OutlineRange(uint32_t stroke, Range &range) const {
        uint32_t first = 0, last = bucketCount;
        while (first < last) {
            const uint32_t mid = first + (last - first) / 2;
            if (Word(Buckets() + mid * 8) < stroke) first = mid + 1;
            else last = mid;
        }
        if (first == bucketCount || Word(Buckets() + first * 8) != stroke) {
            range = {};
            return true;
        }
        range.begin = Word(Buckets() + first * 8 + 4);
        range.end = first + 1 == bucketCount ? postingCount : Word(Buckets() + (first + 1) * 8 + 4);
        return range.begin < range.end && range.end <= postingCount;
    }
    bool ReadTextEntry(uint32_t index, Entry &entry) const {
        return index < reverseCount && Decode(Word(image + legacy + index * 4), entry);
    }
    bool ReadOutlineEntry(uint32_t index, Entry &entry) const {
        return index < postingCount && Decode(Word(Buckets() + bucketCount * 8 + index * 4), entry);
    }
    bool IsStrictPrefix(const Entry &entry, size_t length) const {
        const auto delta = reinterpret_cast<uintptr_t>(entry.text) - text;
        return delta < textBytes && length < textBytes - delta && Byte(text + delta + length);
    }

private:
    uintptr_t image, text;
    WordReader word;
    ByteReader byte;
    uint32_t textBytes, legacy;
    uint32_t reverseCount = 0, bucketCount = 0, postingCount = 0;

    uintptr_t Buckets() const { return image + legacy + reverseCount * 4; }
    uint32_t Word(uintptr_t at) const { return word(reinterpret_cast<const void *>(at)); }
    uint8_t Byte(uintptr_t at) const { return byte(reinterpret_cast<const void *>(at)); }
    uint32_t U24(uintptr_t at) const {
        // A single aligned word normally covers the three packed bytes.
        const unsigned shift = (at & 3) * 8;
        const uintptr_t aligned = at & ~uintptr_t(3);
        const uint32_t low = Word(aligned) >> shift;
        return (shift <= 8 ? low : low | (Word(aligned + 4) << (32 - shift))) & 0xffffff;
    }
    bool EntryText(uint32_t token, uint32_t &offset, unsigned &length, uintptr_t &address) const {
        const uint32_t at = token >> 4;
        length = (token & 15) + 1;
        if (at < 40 || at > legacy || 3 * (length + 1) > legacy - at) return false;
        address = image + at;
        offset = U24(address);
        return offset < textBytes;
    }
    bool Decode(uint32_t token, Entry &entry) const {
        uint32_t offset;
        uintptr_t address;
        if (!EntryText(token, offset, entry.length, address)) return false;
        entry.text = reinterpret_cast<const uint8_t *>(text + offset);
        for (unsigned i = 0; i < entry.length; ++i) {
            entry.strokes[i] = U24(address + 3 * (i + 1));
            if (!entry.strokes[i]) return false;
        }
        return true;
    }
    int Compare(uint32_t index, const char *query, size_t size, bool prefix) const {
        uint32_t offset;
        unsigned length;
        uintptr_t address;
        if (!EntryText(Word(image + legacy + index * 4), offset, length, address)) return 2;
        for (size_t i = 0; i < size; ++i) {
            if (i >= textBytes - offset) return 2;
            const uint8_t stored = Byte(text + offset + i), wanted = static_cast<uint8_t>(query[i]);
            if (stored != wanted) return stored < wanted ? -1 : 1;
        }
        if (prefix) return 0;
        if (size >= textBytes - offset) return 2;
        return Byte(text + offset + size) ? 1 : 0;
    }
};
