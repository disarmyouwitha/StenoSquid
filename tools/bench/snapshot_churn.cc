#include "native_store_fixture.h"
#include <fstream>
#include <iostream>

unsigned Count(const SegmentMap &map) {
    unsigned count = 0;
    for (auto word : map.words) count += __builtin_popcount(word);
    return count;
}

struct ChurnDevice : NativeDevice {
    uint64_t erased_bytes = 0, programmed_bytes = 0, read_bytes = 0;
    bool Read(uint32_t at, uint8_t *out, uint32_t bytes) {
        read_bytes += bytes;
        return NativeDevice::Read(at, out, bytes);
    }
    bool Checksum(uint32_t at, uint32_t bytes, uint32_t &crc) {
        read_bytes += bytes;
        return NativeDevice::Checksum(at, bytes, crc);
    }
    bool CheckBase(uint32_t at, uint32_t bytes, uint32_t expected) {
        uint32_t crc;
        return Checksum(at, bytes, crc) && crc == expected;
    }
    bool Erase(uint32_t segment) {
        erased_bytes += kSegmentBytes;
        return NativeDevice::Erase(segment);
    }
    bool EraseIndex(uint32_t segment, uint32_t bytes) {
        erased_bytes += bytes;
        return NativeDevice::EraseIndex(segment, bytes);
    }
    bool ErasePoolSector(uint32_t at) {
        erased_bytes += kEraseSectorBytes;
        return NativeDevice::ErasePoolSector(at);
    }
    bool EraseCatalog(uint32_t at, uint32_t bytes) {
        erased_bytes += bytes;
        return NativeDevice::EraseCatalog(at, bytes);
    }
    bool Program(uint32_t at, const uint8_t *data, uint32_t bytes) {
        programmed_bytes += bytes;
        return NativeDevice::Program(at, data, bytes);
    }
};

struct Experiment {
    Nor nor;
    ChurnDevice device{{{nor}}};
    NativeStore<ChurnDevice> store{device};
    NativeValidator<ChurnDevice> validator{device, store.catalog};
    unsigned jobs = 0, peak_protected = 0;

    auto Check() {
        return [&](uint32_t i, const uint8_t *row) { return validator.Extent(i, row); };
    }
    SegmentMap Owned() {
        SegmentMap used;
        assert(CollectOwnership(device, store.catalog, store.selected.root, used) == CatalogResult::Ok);
        peak_protected = std::max(peak_protected, Count(used) - 1);
        return used;
    }
    void Open() {
        assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
        Owned();
    }
    void Commit(uint32_t id, uint32_t operation, const uint32_t (&strokes)[2], const std::string &value) {
        while (true) {
            const auto result = store.Edit(id, operation, strokes, 2,
                                         reinterpret_cast<const uint8_t *>(value.data()), value.size());
            if (result == LiveResult::Ok) return;
            assert(result == LiveResult::Busy);
            JobResult job;
            do {
                job = store.Step(Check());
            } while (job == JobResult::More);
            assert(job == JobResult::Done);
            ++jobs;
            Owned();
        }
    }
    void Report(const char *prefix, unsigned edits) {
        const auto owned = Owned();
        uint32_t f, r;
        SegmentMap active;
        assert(RootOwnership(device, store.catalog, store.selected.root, active, &f, &r) == CatalogResult::Ok);
        unsigned longest = 0, run = 0;
        for (unsigned i = 2; i < 255; ++i) {
            run = owned.Contains(i) ? 0 : run + 1;
            longest = std::max(longest, run);
        }
        const std::string name = std::string(prefix) + "-" + std::to_string(edits);
        std::ofstream image(name + ".bin", std::ios::binary);
        image.write(reinterpret_cast<const char *>(nor.data.data()), 255 * kSegmentBytes);
        assert(image);
        std::cout << edits << ' ' << (Count(active) + 2) << ' ' << (Count(owned) - 1) << ' '
                  << longest << ' ' << f << ' ' << r << ' ' << jobs << ' ' << peak_protected << ' '
                  << device.programmed_bytes << ' ' << device.erased_bytes << ' '
                  << device.read_bytes << '\n';
    }
};

int main(int argc, char **argv) {
    assert(argc == 5);
    std::ifstream image(argv[1], std::ios::binary), tape(argv[2]);
    Experiment trial;
    trial.nor.data.assign(std::istreambuf_iterator<char>(image), {});
    trial.nor.data.resize(256 * kSegmentBytes, 255);
    trial.Open();
    const uint32_t id = std::stoul(argv[4]);
    trial.Report(argv[3], 0);
    unsigned count = 0;
    uint32_t operation, strokes[2];
    std::string value;
    while (tape >> operation >> strokes[0] >> strokes[1]) {
        std::getline(tape, value);
        assert(!value.empty() && value[0] == ' ');
        trial.Commit(id, operation, strokes, value.substr(1));
        ++count;
        if (count == 100 || count == 1000 || count == 10000 || count == 500) {
            trial.Open();
            trial.Report(argv[3], count);
        }
    }
}
