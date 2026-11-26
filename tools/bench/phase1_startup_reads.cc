// Compare physical read requests for the full pool using the real startup validator.
#include "native_store_fixture.h"
#include "native_startup_read.h"

#include <fstream>
#include <iostream>
#include <iterator>

struct StartupDevice : NativeDevice {
    bool read_ahead = false;
    bool wide_read_ahead = false;
    StartupReadAhead cache;
    uint8_t buffer[StartupReadAhead::PageBytes]{};
    uint8_t wide_buffer[2048]{};

    bool Read(uint32_t at, uint8_t *out, uint32_t bytes) {
        if (at > nor.data.size() || bytes > nor.data.size() - at) return false;
        if (!read_ahead) return NativeDevice::Read(at, out, bytes);
        auto fill = [&](uint32_t base, auto &page) {
            return NativeDevice::Read(base, page, sizeof(page));
        };
        return wide_read_ahead ? cache.Read(at, out, bytes, wide_buffer, fill)
                               : cache.Read(at, out, bytes, buffer, fill);
    }

    // The measured pool already has verified bases. Index CRCs still read
    // every byte, using the same request size as FlashDevice.
    bool CheckBase(uint32_t, uint32_t, uint32_t) { return true; }
    bool Checksum(uint32_t at, uint32_t bytes, uint32_t &crc) {
        uint8_t chunk[StartupReadAhead::PageBytes];
        crc = 0;
        for (uint32_t offset = 0; offset < bytes;) {
            const auto count = std::min<uint32_t>(sizeof(chunk), bytes - offset);
            if (!Read(at + offset, chunk, count)) return false;
            crc = Crc(crc, chunk, count);
            offset += count;
        }
        return true;
    }
};

static StartupDevice *hybrid_device;
static void JournalDmaOnly(ValidationStage stage) {
    hybrid_device->read_ahead = stage == ValidationStage::Journal;
    if (hybrid_device->read_ahead) hybrid_device->cache = {};
}

int main(int argc, char **argv) {
    assert(argc == 2);
    std::ifstream stream(argv[1], std::ios::binary);
    Nor nor;
    nor.data.assign(std::istreambuf_iterator<char>(stream), {});
    assert(nor.data.size() == kLargePoolEnd * kSegmentBytes);
    nor.data.resize(nor.data.size() + kSegmentBytes, 255);

    const char *mode_name[] = {"direct requests", "DMA pages", "reused recovery", "batched index keys",
                               "batched index + journal DMA", "batched index + 2 KiB journal DMA"};
    for (unsigned mode = 0; mode < sizeof(mode_name) / sizeof(mode_name[0]); ++mode) {
        const bool read_ahead = mode == 1;
        const bool reuse_recovery = mode >= 2;
        const bool batch_keys = mode >= 3;
        const auto stage = mode >= 4 ? JournalDmaOnly : nullptr;
        StartupDevice device{{{nor}, kLargePoolEnd}, read_ahead, mode == 5, {}, {}, {}};
        hybrid_device = &device;
        NativeStore<StartupDevice> store(device);
        NativeValidator<StartupDevice> validator(device, store.catalog);
        nor.reads = nor.read_bytes = 0;
        const auto writes = nor.operations;
        const auto validate = [&](const CatalogRoot &root) {
            const auto result = validator.Validate(root, false, stage, nullptr,
                                                   reuse_recovery ? &store.live : nullptr, batch_keys);
            if (stage) device.read_ahead = false;
            return result;
        };
        const auto result = store.Open(validate, CatalogValidation::ReuseIdenticalPayload, reuse_recovery);
        assert(result == CatalogResult::Ok && store.opened);
        assert(store.selected.root.generation == 2 && store.live.TailCount() == 17);
        assert(nor.operations == writes);
        std::cout << mode_name[mode] << ": "
                  << nor.reads << " reads, " << nor.read_bytes << " bytes\n";

        // The fixture's index bytes remain protected by their original CRCs.
        const auto forward_index = validator.ValidatedView().forward_address;
        assert(forward_index >= 2 * kSegmentBytes);
        nor.data[forward_index] ^= 1;
        device.cache = {};
        assert(store.Open(validate, CatalogValidation::ReuseIdenticalPayload, reuse_recovery) == CatalogResult::Corrupt);
        assert(!store.opened && nor.operations == writes);
        nor.data[forward_index] ^= 1;
    }
}
