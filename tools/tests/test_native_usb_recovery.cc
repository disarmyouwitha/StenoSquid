#include "native_store_fixture.h"
#include "native_provisioner.h"
#include <iostream>

struct RecoveryDevice : NativeDevice {
    bool Checksum(uint32_t at, uint32_t bytes, uint32_t &crc) {
        // Count verification itself so a cut can occur before or after publication.
        if (++nor.operations == nor.cut) throw Cut{};
        return NativeDevice::Checksum(at, bytes, crc);
    }
};

static constexpr unsigned Segments = 8, Size = Segments * kSegmentBytes;
using Transfer = NativeProvisioner<RecoveryDevice, Segments>;

void Stage(Transfer &install, const std::vector<uint8_t> &image) {
    for (unsigned segment = 0; segment < Segments; ++segment)
        assert(install.Erase(segment) == ProvisionResult::Ok);
    auto write = [&](uint32_t at) {
        const auto *data = image.data() + at;
        if (!std::all_of(data, data + 512, [](uint8_t byte) { return byte == 255; }))
            assert(install.Write(at, data, 512) == ProvisionResult::Ok);
    };
    for (uint32_t at = 2 * kSegmentBytes; at < Size; at += 512) write(at);
    for (uint32_t at = 0; at < 2 * kSegmentBytes; at += 512) write(at);
}

int main() {
    NativeFixture fixture;
    assert(fixture.Edit(1, 8, "recovered text") == LiveResult::Ok);
    auto expected = fixture.nor.data;
    expected.resize(Size);
    std::copy_n(expected.begin(), 65536, expected.begin() + 65536);
    auto *second = expected.data() + 65536;
    PutWord(second + 8, 2);
    PutWord(second + 24, Crc(0, second, 24));
    auto staged = expected;
    std::fill_n(staged.begin() + 28, 4, 255);
    std::fill_n(staged.begin() + 65536 + 28, 4, 255);
    const auto staged_crc = Crc(0, staged.data(), Size);
    const auto final_crc = Crc(0, expected.data(), Size);
    auto begin = [&](Transfer &install) {
        assert(install.BeginRecovery(staged_crc, final_crc) == ProvisionResult::Ok);
    };

    unsigned boundaries = 0;
    for (unsigned cut = 0; cut <= boundaries; ++cut) {
        for (unsigned tear : {1u, 4095u}) {
            for (uint8_t initial : {uint8_t(255), uint8_t(0x69)}) {
                Nor nor;
                nor.data.assign(256 * kSegmentBytes, initial);
                nor.cut = cut;
                nor.tear = tear;
                RecoveryDevice device{{{nor}}};
                Transfer transfer(device);
                begin(transfer);
                try {
                    Stage(transfer, staged);
                    assert(transfer.Write(0, expected.data(), 512) == ProvisionResult::Invalid);
                    assert(transfer.Finish() == ProvisionResult::Ok);
                    assert(!cut);
                    boundaries = std::max(boundaries, nor.operations);
                } catch (const Cut &) {
                    assert(cut);
                }
                const bool published = Word(nor.data.data() + 28) == 0x434f4d54 ||
                                       Word(nor.data.data() + 65536 + 28) == 0x434f4d54;
                if (published) {
                    for (unsigned i = 0; i < Size; ++i) {
                        if ((i & 65535) >= 28 && (i & 65535) < 32 && i < 2 * 65536u) continue;
                        assert(nor.data[i] == expected[i]);
                    }
                }
                nor.cut = 0;
                // A power cycle loses all transfer state. The same immutable
                // input can explicitly restart, even without a valid catalog.
                if (cut) {
                    Transfer retry(device);
                    begin(retry);
                    Stage(retry, staged);
                    assert(retry.Finish() == ProvisionResult::Ok);
                }
                assert(std::equal(expected.begin(), expected.end(), nor.data.begin()));
                assert(std::all_of(nor.data.begin() + Size, nor.data.end(),
                                   [&](uint8_t byte) { return byte == initial; }));
                NativeStore<RecoveryDevice> store(device);
                NativeValidator<RecoveryDevice> validator(device, store.catalog);
                assert(store.Open([&](const CatalogRoot &root) { return validator.Validate(root); }) == CatalogResult::Ok);
                const uint32_t outline = 8;
                RecordView row;
                assert(store.live.FindLive(1, &outline, 1, row) == LiveResult::Ok);
                assert(row.text_bytes == 14 && !std::memcmp(nor.data.data() + row.text_address, "recovered text", 14));
            }
        }
    }
    {
        Nor nor;
        nor.data.resize(Size, 255);
        RecoveryDevice device{{{nor}}};
        Transfer transfer(device);
        begin(transfer);
        Stage(transfer, staged);
        nor.fail_after_commit = true;
        assert(transfer.Finish() == ProvisionResult::IoError);
        assert(Word(nor.data.data() + 28) == 0x434f4d54);
        nor.fail_read = false;
        assert(transfer.Erase(0) == ProvisionResult::Invalid);
        assert(transfer.Finish() == ProvisionResult::Ok);
        assert(transfer.Finish() == ProvisionResult::Ok);
    }
    {
        Nor nor;
        nor.data.resize(Size, 255);
        RecoveryDevice device{{{nor}}};
        Transfer transfer(device);
        assert(transfer.BeginRecovery(staged_crc ^ 1, final_crc) == ProvisionResult::Ok);
        Stage(transfer, staged);
        assert(transfer.Finish() == ProvisionResult::ChecksumMismatch);
        assert(Word(nor.data.data() + 28) == UINT32_MAX && Word(nor.data.data() + 65536 + 28) == UINT32_MAX);
    }
    std::cout << "USB recovery: " << boundaries
              << " erasings/programs/verifications/publication boundaries, two tears and two initial states each\n";
}
