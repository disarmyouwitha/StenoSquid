#include "native_store_fixture.h"
#include "native_provisioner.h"
#include "compact_search.h"
#include <fstream>
#include <iostream>

struct RepackDevice : NativeDevice {
    bool Checksum(uint32_t at, uint32_t bytes, uint32_t &crc) {
        if (++nor.operations == nor.cut) throw Cut{};
        return NativeDevice::Checksum(at, bytes, crc);
    }
};

static constexpr uint32_t PoolBytes = 255 * kSegmentBytes, Xip = 0x12000000;
using Transfer = NativeProvisioner<RepackDevice>;

static uint32_t ReadWord(const void *p) {
    return Word(static_cast<const uint8_t *>(p));
}
static uint8_t ReadByte(const void *p) {
    return *static_cast<const uint8_t *>(p);
}

void Install(Transfer &transfer, const std::vector<uint8_t> &staged) {
    for (unsigned i = 0; i < 255; ++i) assert(transfer.Erase(i) == ProvisionResult::Ok);
    auto write = [&](uint32_t first, uint32_t last) {
        for (uint32_t at = first; at < last; at += 512) {
            const auto *page = staged.data() + at;
            if (!std::all_of(page, page + 512, [](uint8_t byte) { return byte == 255; }))
                assert(transfer.Write(at, page, 512) == ProvisionResult::Ok);
        }
    };
    write(2 * kSegmentBytes, PoolBytes);
    write(0, 2 * kSegmentBytes);
    assert(transfer.Finish() == ProvisionResult::Ok);
}

void TestLookups(RepackDevice &device) {
    NativeStore<RepackDevice> store(device);
    NativeValidator<RepackDevice> validator(device, store.catalog);
    assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
    uint8_t row[96], root[32], extent[24];
    assert(store.Row(1, row) && Word(row) == 20 && !Word(row + 4) && row[11] == kUserSnapshot);
    assert(device.Read(store.selected.root.address, root, 32));
    const unsigned count = root[4] | unsigned(root[5]) << 8, base = row[8] | unsigned(row[9]) << 8;
    assert(device.Read(store.selected.root.address + 32 + 96 * count + 24 * base, extent, 24));
    const unsigned segment = extent[0] | unsigned(extent[1]) << 8, address = segment * kSegmentBytes;
    const auto *blob = device.nor.data.data() + address;
    const auto pointer = reinterpret_cast<uintptr_t>(blob);
    StenoSquidCompactSearch view(pointer, Word(extent + 4),
                                pointer + Word(blob + 12) - Xip - address,
                                Word(blob + 8), ReadWord, ReadByte);
    assert(view.Available());
    StenoSquidCompactSearch::Range range;
    StenoSquidCompactSearch::Entry entry;
    assert(view.TextRange("personal cat", 12, false, range) && range.end - range.begin == 1);
    assert(view.ReadTextEntry(range.begin, entry) && entry.length == 1 && entry.strokes[0] == 524552);
    assert(view.TextRange("personal cat", 12, true, range) && range.end - range.begin == 2);
    assert(view.OutlineRange(524552, range) && range.end - range.begin == 2);
    const uint32_t kat = 524552, bob = 66096;
    RecordView record;
    assert(store.live.FindLive(20, &bob, 1, record) == LiveResult::Ok && record.operation == Operation::Remove);
    assert(store.live.FindLive(20, &kat, 1, record) == LiveResult::Missing);
    assert(store.live.FindLive(2, &kat, 1, record) == LiveResult::Ok && record.operation == Operation::Set);
}

int main(int argc, char **argv) {
    assert(argc == 3);
    std::ifstream old_file(argv[1], std::ios::binary), new_file(argv[2], std::ios::binary);
    const std::vector<uint8_t> original(std::istreambuf_iterator<char>(old_file), {});
    const std::vector<uint8_t> target(std::istreambuf_iterator<char>(new_file), {});
    assert(original.size() == PoolBytes && target.size() == PoolBytes);
    auto staged = target;
    for (unsigned slot = 0; slot < 2; ++slot) std::fill_n(staged.begin() + 28 + slot * kSegmentBytes, 4, 255);
    const auto staged_crc = Crc(0, staged.data(), staged.size()), result_crc = Crc(0, target.data(), target.size());
    auto run = [&](unsigned cut, unsigned tear, bool inspect) {
        Nor nor;
        nor.data = original;
        nor.data.resize(256 * kSegmentBytes, 0x5a);
        nor.cut = cut;
        nor.tear = tear;
        RepackDevice device{{{nor}}};
        Transfer transfer(device);
        assert(transfer.BeginRecovery(staged_crc, result_crc) == ProvisionResult::Ok);
        try {
            Install(transfer, staged);
            assert(!cut);
        } catch (const Cut &) {
            assert(cut);
        }
        if (inspect) {
            for (unsigned slot = 0; slot < 2; ++slot) {
                const unsigned at = slot * kSegmentBytes;
                if (!std::memcmp(nor.data.data() + at, target.data() + at, 32)) {
                    for (unsigned i = 0; i < PoolBytes; ++i) {
                        if (i < 2 * kSegmentBytes && i % kSegmentBytes >= 28 && i % kSegmentBytes < 32) continue;
                        assert(nor.data[i] == target[i]);
                    }
                }
            }
        }
        const auto operations = nor.operations;
        if (cut) {
            nor.cut = 0;
            Transfer retry(device);
            assert(retry.BeginRecovery(staged_crc, result_crc) == ProvisionResult::Ok);
            Install(retry, staged);
        }
        assert(std::equal(target.begin(), target.end(), nor.data.begin()));
        assert(std::all_of(nor.data.begin() + PoolBytes, nor.data.end(),
                           [](uint8_t byte) { return byte == 0x5a; }));
        TestLookups(device);
        return operations;
    };
    const unsigned operations = run(0, 0, false);
    for (unsigned cut : {1u, 32u, 4080u, 4081u, operations - 3, operations - 2, operations - 1, operations})
        for (unsigned tear : {1u, 4095u}) run(cut, tear, cut >= 32);
    std::cout << "Offline target passed cold Native open, lookup parity, and 16 selected fake-NOR cuts and retries\n";
}
