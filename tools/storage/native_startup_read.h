#pragma once

#include <algorithm>
#include <cstdint>
#include <cstring>

namespace candidate_b {

// Read only from immutable flash under the startup lock. The caller supplies
// one page of scratch space and invalidates it before each validation attempt.
struct StartupReadAhead {
    static constexpr uint32_t PageBytes = 256;
    uint32_t page = UINT32_MAX;

    template<size_t BlockBytes, class ReadPage>
    bool Read(uint32_t at, uint8_t *out, uint32_t bytes, uint8_t (&buffer)[BlockBytes], ReadPage read_page) {
        static_assert(BlockBytes >= PageBytes && !(BlockBytes & (BlockBytes - 1)));
        constexpr uint32_t block = BlockBytes;
        while (bytes) {
            const uint32_t offset = at % block;
            const uint32_t base = at - offset;
            if (base != page) {
                page = UINT32_MAX;
                if (!read_page(base, buffer)) return false;
                page = base;
            }
            const uint32_t count = std::min(bytes, block - offset);
            std::memcpy(out, buffer + offset, count);
            at += count;
            out += count;
            bytes -= count;
        }
        return true;
    }
};

// Use aligned pages of the existing scratch buffer. Keep the tags and
// replacement cursor in the first page so the lab build adds no static RAM.
struct StartupIndexCache {
    static constexpr size_t BufferBytes = 2048;
#ifdef CONFIG_ZMK_STENO_SQUID_BOOT_INDEX_WIDE_PAGE
    static constexpr uint32_t PageBytes = 512;
#else
    static constexpr uint32_t PageBytes = StartupReadAhead::PageBytes;
#endif
    static constexpr unsigned Slots = BufferBytes / PageBytes - 1;

    static void Reset(uint8_t (&buffer)[BufferBytes]) {
        std::memset(buffer, 0xff, Slots * sizeof(uint32_t));
        buffer[Slots * sizeof(uint32_t)] = 0;
    }

    template<class ReadPage>
    static bool Read(uint32_t at, uint8_t *out, uint32_t bytes, uint8_t (&buffer)[BufferBytes], ReadPage fill) {
        while (bytes) {
            const uint32_t offset = at % PageBytes;
            const uint32_t base = at - offset;
            unsigned slot = 0;
            for (; slot < Slots; ++slot) {
                uint32_t tag;
                std::memcpy(&tag, buffer + slot * sizeof(tag), sizeof(tag));
                if (tag == base) break;
            }
            if (slot == Slots) {
                auto &next = buffer[Slots * sizeof(uint32_t)];
                slot = next;
                auto *tag = buffer + slot * sizeof(uint32_t);
                std::memset(tag, 0xff, sizeof(uint32_t));
                if (!fill(base, buffer + (slot + 1) * PageBytes, PageBytes)) return false;
                std::memcpy(tag, &base, sizeof(base));
                next = (slot + 1) % Slots;
            }
            const uint32_t count = std::min(bytes, PageBytes - offset);
            std::memcpy(out, buffer + (slot + 1) * PageBytes + offset, count);
            at += count;
            out += count;
            bytes -= count;
        }
        return true;
    }
};

} // namespace candidate_b
