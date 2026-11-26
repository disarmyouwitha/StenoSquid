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

    template<class ReadPage>
    bool Read(uint32_t at, uint8_t *out, uint32_t bytes, uint8_t (&buffer)[PageBytes], ReadPage read_page) {
        while (bytes) {
            const uint32_t offset = at % PageBytes;
            const uint32_t base = at - offset;
            if (base != page) {
                page = UINT32_MAX;
                if (!read_page(base, buffer)) return false;
                page = base;
            }
            const uint32_t count = std::min(bytes, PageBytes - offset);
            std::memcpy(out, buffer + offset, count);
            at += count;
            out += count;
            bytes -= count;
        }
        return true;
    }
};

} // namespace candidate_b
