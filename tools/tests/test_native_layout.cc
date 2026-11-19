#include "native_layout_map.h"
#include "native_test_nor.h"
#include <algorithm>
#include <array>
#include <cassert>

using namespace candidate_b;

struct Metadata {
    std::array<uint8_t, 1024> payload{};
    unsigned reads = 0;
    bool Read(uint32_t address, uint8_t *out, uint32_t size) {
        assert(address >= 32 && address + size <= 32 + payload.size());
        ++reads;
        std::copy_n(payload.data() + address - 32, size, out);
        return true;
    }
};

int main() {
    Metadata device;
    const CatalogRoot root{2, 32, 32 + 2 * 96 + 5 * 24, 0};
    device.payload[4] = 2;
    device.payload[6] = 5;
    auto *snapshot = device.payload.data() + 32 + 96;
    snapshot[8] = 1;
    snapshot[11] = kUserSnapshot;
    auto extent = [&](unsigned i, uint16_t at, uint16_t count, uint8_t kind) {
        auto *row = device.payload.data() + 32 + 2 * 96 + i * 24;
        row[0] = at;
        row[2] = count;
        row[12] = kind;
    };
    extent(0, 20, 2, 1);
    extent(1, 22, 3, 1);
    extent(2, 35, 1, 2);
    extent(3, 47, 1, 3);
    extent(4, 53, 1, 4);
    std::array<char, 256> map;
    assert(ReadLayout(device, root, map.data(), [] { return false; }));
    assert(device.reads == 9);
    assert(map[0] == 'c' && map[1] == 'c' && map[255] == 'x');
    assert(map[20] == 'b' && map[21] == 'b' && map[22] == 's' && map[24] == 's');
    assert(map[35] == 'h' && map[47] == 'f' && map[53] == 'r' && map[54] == '.');
    assert(!ReadLayout(device, root, map.data(), [&] { return device.reads >= 12; }));
    assert(device.reads == 12);
    extent(4, 22, 1, 4);
    assert(!ReadLayout(device, root, map.data(), [] { return false; }));
}
