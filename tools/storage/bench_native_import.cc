// Developer-only host experiment: actual store/allocator, strict simulated NOR.
// Host time is not a keyboard latency measurement. CRC caching models the runtime.
#include "native_store.h"
#include "native_validator.h"
#include "native_test_nor.h"
#include <chrono>
#include <algorithm>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
using namespace candidate_b;
using namespace native_test;
struct Device {
    Nor &nor;
    struct Cached {
        uint32_t at = 0, bytes = 0, crc = 0;
        bool valid = false;
    } cache[32]{};
    unsigned next = 0;
    uint64_t reads = 0, read_bytes = 0, crc_bytes = 0, programs = 0, erases = 0, catalog_erases = 0;
    void Invalidate(uint32_t at, uint32_t bytes) {
        for (auto &item : cache)
            if (item.valid && at < item.at + item.bytes && item.at < at + bytes)
                item.valid = false;
    }
    bool Read(uint32_t at, uint8_t *out, uint32_t n) {
        if (at > 255 * 65536 || n > 255 * 65536 - at)
            return false;
        ++reads;
        read_bytes += n;
        return nor.Read(at, out, n);
    }
    bool Program(uint32_t at, const uint8_t *in, uint32_t n) {
        if (at > 255 * 65536 || n > 255 * 65536 - at)
            return false;
        Invalidate(at, n);
        ++programs;
        return nor.Program(at, in, n);
    }
    bool Erase(uint32_t segment) {
        if (segment < 2 || segment >= 255)
            return false;
        Invalidate(segment * 65536, 65536);
        ++erases;
        return nor.EraseSegment(segment);
    }
    bool EraseCatalog(uint32_t at, uint32_t n) {
        Invalidate(at, n);
        ++catalog_erases;
        return nor.EraseCatalog(at, n);
    }
    bool Checksum(uint32_t at, uint32_t n, uint32_t &crc) {
        if (at > 255 * 65536 || n > 255 * 65536 - at)
            return false;
        for (const auto &item : cache)
            if (item.valid && item.at == at && item.bytes == n) {
                crc = item.crc;
                return true;
            }
        crc_bytes += n;
        crc = 0;
        uint8_t buffer[256];
        for (uint32_t offset = 0; offset < n;) {
            const uint32_t amount = std::min(uint32_t(sizeof(buffer)), n - offset);
            if (!Read(at + offset, buffer, amount))
                return false;
            crc = Crc(crc, buffer, amount);
            offset += amount;
        }
        cache[next++ % 32] = {at, n, crc, true};
        return true;
    }
    bool CheckBase(uint32_t at, uint32_t n, uint32_t expected) {
        uint32_t crc;
        return Checksum(at, n, crc) && crc == expected;
    }
};
struct Entry {
    std::vector<uint32_t> strokes;
    std::string text;
};
uint32_t ReadWord(std::istream &input) {
    uint8_t word[4];
    input.read(reinterpret_cast<char *>(word), 4);
    assert(input);
    return Word(word);
}
int main(int argc, char **argv) {
    assert(argc == 3);
    std::ifstream input(argv[1], std::ios::binary);
    assert(input);
    Nor nor;
    nor.data.assign(256 * 65536, 0x42);
    input.read(reinterpret_cast<char *>(nor.data.data()), 255 * 65536);
    assert(input);
    std::ifstream source(argv[2], std::ios::binary);
    assert(source);
    char magic[8];
    source.read(magic, 8);
    assert(!std::memcmp(magic, "SQBENCH1", 8));
    const unsigned count = ReadWord(source);
    std::vector<Entry> entries;
    entries.reserve(count);
    for (unsigned i = 0; i < count; ++i) {
        uint8_t prefix[3];
        source.read(reinterpret_cast<char *>(prefix), 3);
        assert(source && prefix[0] && prefix[0] <= 16);
        Entry entry;
        entry.strokes.resize(prefix[0]);
        for (auto &stroke : entry.strokes)
            stroke = ReadWord(source);
        entry.text.resize(uint16_t(prefix[1]) | uint16_t(prefix[2]) << 8);
        assert(entry.text.size() <= 2047);
        source.read(entry.text.data(), entry.text.size());
        assert(source);
        entries.push_back(std::move(entry));
    }
    Device device{nor};
    NativeStore<Device> store(device);
    NativeValidator<Device> validator(device, store.catalog);
    auto check = [&](uint32_t i, const uint8_t *row) { return validator.Extent(i, row); };
    auto open = [&] {
        return store.Open([&](const CatalogRoot &root) { return validator.Validate(root); });
    };
    const auto started = std::chrono::steady_clock::now();
    assert(open() == CatalogResult::Ok && store.live.Highwater() == 0);
    uint32_t identity = 0;
    for (unsigned i = 0;; ++i) {
        uint8_t row[96];
        if (!store.Row(i, row))
            break;
        if (row[12] == 15 && !std::memcmp(row + 16, "user_dictionary", 15))
            identity = Word(row);
    }
    assert(identity);
    uint64_t steps = 0, jobs = 0;
    std::string outcome = "complete";
    unsigned committed = 0;
    auto finish = [&]() {
        ++jobs;
        for (unsigned n = 0; n < 10000000; ++n) {
            const auto status = store.Step(check);
            ++steps;
            if (status != JobResult::More)
                return status;
        }
        return JobResult::Invalid;
    };
    for (const auto &entry : entries) {
        bool done = false;
        for (unsigned tries = 0; tries < 8; ++tries) {
            const auto result =
                store.Edit(identity, 0, entry.strokes.data(), entry.strokes.size(),
                           reinterpret_cast<const uint8_t *>(entry.text.data()), entry.text.size());
            if (result == LiveResult::Ok) {
                done = true;
                break;
            }
            if (result != LiveResult::Busy) {
                outcome = "edit_refused_" + std::to_string(unsigned(result));
                break;
            }
            const auto status = finish();
            if (status != JobResult::Done) {
                outcome = status == JobResult::NoSpace
                              ? "no_space"
                              : "maintenance_error_" + std::to_string(unsigned(status));
                break;
            }
        }
        if (!done) {
            if (outcome == "complete")
                outcome = "retry_limit";
            break;
        }
        ++committed;
        if (!(committed % 5000))
            std::cerr << "Native store committed " << committed << "/" << count << " entries, "
                      << steps << " steps\n";
    }
    if (outcome == "complete") {
        assert(store.Start(NativeStore<Device>::Work::Checkpoint) == JobResult::More);
        const auto result = finish();
        if (result != JobResult::Done)
            outcome = result == JobResult::NoSpace ? "final_checkpoint_no_space"
                                                   : "final_checkpoint_error";
    }
    const auto imported = std::chrono::steady_clock::now();
    const auto bytes = nor.data;
    Device reboot_device{nor};
    NativeStore<Device> reboot(reboot_device);
    NativeValidator<Device> reboot_validator(reboot_device, reboot.catalog);
    assert(reboot.Open([&](const CatalogRoot &root) { return reboot_validator.Validate(root); }) ==
               CatalogResult::Ok &&
           bytes == nor.data);
    for (unsigned i = 0; i < committed; ++i) {
        const auto &entry = entries[i];
        LiveView view;
        assert(reboot.Snapshot(view));
        RecordView record;
        assert(reboot.live.FindSnapshot(view, identity, entry.strokes.data(), entry.strokes.size(),
                                        record) == LiveResult::Ok);
        assert(record.operation == Operation::Set && record.text_bytes == entry.text.size());
        std::string text(record.text_bytes, ' ');
        assert(reboot.live.ReadText(view, record, reinterpret_cast<uint8_t *>(text.data()),
                                    text.size()) == LiveResult::Ok &&
               text == entry.text);
        assert(reboot.Release(view));
    }
    SegmentMap owned;
    assert(CollectOwnership(reboot_device, reboot.catalog, reboot.selected.root, owned) ==
           CatalogResult::Ok);
    assert(nor.data == bytes && !reboot_device.programs && !reboot_device.erases &&
           !reboot_device.catalog_erases);
    for (uint32_t at = 255 * 65536; at < 256 * 65536; ++at)
        assert(nor.data[at] == 0x42);
    const auto verified = std::chrono::steady_clock::now();
    std::cout << "{\"outcome\":\"" << outcome << "\",\"requested\":" << count
              << ",\"committed\":" << committed << ",\"verified_after_reopen\":" << committed
              << ",\"free_segments\":" << AllocationPlan(owned).Free()
              << ",\"maintenance_steps\":" << steps << ",\"maintenance_jobs\":" << jobs
              << ",\"read_calls\":" << (device.reads + reboot_device.reads)
              << ",\"read_bytes\":" << (device.read_bytes + reboot_device.read_bytes)
              << ",\"checksum_bytes\":" << (device.crc_bytes + reboot_device.crc_bytes)
              << ",\"import_read_bytes\":" << device.read_bytes
              << ",\"verification_read_bytes\":" << reboot_device.read_bytes
              << ",\"cold_reopen_verified\":true"
              << ",\"program_calls\":" << device.programs
              << ",\"journal_index_erases\":" << device.erases
              << ",\"catalog_erases\":" << device.catalog_erases << ",\"host_import_ms\":"
              << std::chrono::duration_cast<std::chrono::milliseconds>(imported - started).count()
              << ",\"host_verify_ms\":"
              << std::chrono::duration_cast<std::chrono::milliseconds>(verified - imported).count()
              << "}\n";
}
