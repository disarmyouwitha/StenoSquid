#include "packed_index.h"
#include "binary_record.h"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>

int main(int argc, char **argv) {
    assert(argc == 4);
    std::ifstream recordsFile(argv[2], std::ios::binary);
    const std::vector<uint8_t> records((std::istreambuf_iterator<char>(recordsFile)), {});
    auto recordRead = [&](uint32_t offset, uint8_t *out, size_t size) {
        if (offset > records.size() || size > records.size() - offset) return false;
        std::memcpy(out, records.data() + offset, size);
        return true;
    };
    std::ifstream file(argv[1], std::ios::binary);
    const std::vector<uint8_t> image((std::istreambuf_iterator<char>(file)), {});
    assert(image.size() == 200000 * candidate_b::kIndexStride);
    unsigned reads = 0, comparisons = 0, maximum = 0;
    auto read = [&](uint32_t offset, uint8_t *out, size_t size) {
        ++reads;
        if (size != 12 || offset + size > image.size()) return false;
        std::memcpy(out, image.data() + offset, size);
        return true;
    };
    uint32_t address = 0;
    for (uint32_t i = 0; i < 200000; ++i) {
        reads = comparisons = 0;
        uint32_t stroke = i + 1;
        candidate_b::RecordView view{};
        const auto result = candidate_b::Find(200000, i / 4, read,
            [&](uint32_t record) {
                ++comparisons;
                return candidate_b::MatchOutline(record, 16, 1, &stroke, 1, recordRead, view);
            }, address);
        assert(result == candidate_b::Result::Found && address == i * 16);
        assert(view.text_bytes == 4 && view.text_address == address + 12);
        assert(reads <= 23 && comparisons <= 4);
        maximum = std::max(maximum, reads);
    }
    candidate_b::RecordView view{};
    uint32_t stroke = 1;
    assert(candidate_b::MatchOutline(0, 16, 2, &stroke, 1, recordRead, view) == 0);
    assert(candidate_b::MatchOutline(0, 15, 1, &stroke, 1, recordRead, view) == -1);
    assert(candidate_b::MatchOutline(UINT32_MAX, 16, 1, &stroke, 1, recordRead, view) == -1);
    assert(candidate_b::MatchOutline(0, 16, 1, nullptr, 1, recordRead, view) == -1);
    assert(candidate_b::MatchOutline(0, 16, 1, &stroke, 17, recordRead, view) == -1);
    const uint8_t multi[] = {1,0,0,0,0,2,4,0,1,0,0,0,1,0,0,0,'w','o','r','d'};
    auto multiRead = [&](uint32_t offset, uint8_t *out, size_t size) {
        if (offset > sizeof(multi) || size > sizeof(multi) - offset) return false;
        std::memcpy(out, multi + offset, size);
        return true;
    };
    uint32_t pair[] = {1, 2};
    assert(candidate_b::CompareOutline(0, 20, 1, pair, 1, multiRead, view) == 1);
    assert(candidate_b::CompareOutline(0, 20, 1, pair, 2, multiRead, view) == -1);
    pair[1] = 1;
    assert(candidate_b::CompareOutline(0, 20, 1, pair, 2, multiRead, view) == 0);
    assert(view.text_bytes == 4 && view.text_address == 16);
    assert(candidate_b::CompareOutline(0, 20, 2, pair, 2, multiRead, view) == -1);
    using candidate_b::Result;
    auto mismatch = [](uint32_t) { return 0; };
    reads = comparisons = 0;
    assert(candidate_b::Find(200000, 50000, read, mismatch, address) == Result::Missing);
    assert(reads <= 19);
    assert(candidate_b::Find(200000, 0, read, [](uint32_t) { return -1; }, address) == Result::Corrupt);
    assert(candidate_b::Find(1u << 21, 0, read, mismatch, address) == Result::Corrupt);
    assert(candidate_b::Find(0, 0, read, mismatch, address) == Result::Missing);
    auto failedRead = [](uint32_t, uint8_t *, size_t) { return false; };
    assert(candidate_b::Find(1, 0, failedRead, mismatch, address) == Result::IoError);
    auto excessive = [](uint32_t offset, uint8_t *out, size_t size) {
        std::memset(out, 0, size);
        out[8] = uint8_t(offset / 12);
        return true;
    };
    assert(candidate_b::Find(17, 0, excessive, mismatch, address) == Result::Corrupt);
    candidate_b::Tail tail;
    assert(sizeof(tail) == 1028);
    assert(tail.Append(0) && tail.Append(16));
    const auto snapshot = tail;
    assert(tail.Append(32));
    assert(candidate_b::FindLatest(tail, 200000, 0, read,
        [](uint32_t) { return 1; }, address) == Result::Found && address == 32);
    assert(candidate_b::FindLatest(snapshot, 200000, 0, read,
        [](uint32_t) { return 1; }, address) == Result::Found && address == 16);
    while (tail.count < tail.kCapacity) assert(tail.Append(tail.count * 16));
    const auto count = tail.count;
    assert(!tail.Append(123) && tail.count == count);
    comparisons = reads = 0;
    assert(candidate_b::FindLatest(tail, 200000, 50000, read,
        [&](uint32_t) { ++comparisons; return 0; }, address) == Result::Missing);
    assert(comparisons == 256 && reads <= 19);
    ++tail.count;
    assert(candidate_b::FindLatest(tail, 0, 0, read, mismatch, address) == Result::Corrupt);
    std::ifstream orderedFile(argv[3], std::ios::binary);
    const std::vector<uint8_t> ordered((std::istreambuf_iterator<char>(orderedFile)), {});
    assert(ordered.size() == 200000 * 4);
    auto orderedRead = [&](uint32_t offset, uint8_t *out, size_t size) {
        ++reads;
        if (size != 4 || offset + size > ordered.size()) return false;
        std::memcpy(out, ordered.data() + offset, size);
        return true;
    };
    unsigned orderedMaximum = 0;
    for (uint32_t i = 0; i < 200000; ++i) {
        reads = comparisons = 0;
        uint32_t query = i + 1;
        assert(candidate_b::FindOrdered(200000, orderedRead, [&](uint32_t record) {
            ++comparisons;
            return candidate_b::CompareOutline(record, 16, 1, &query, 1, recordRead, view);
        }, address) == Result::Found && address == i * 16);
        assert(reads <= 18 && comparisons == reads);
        orderedMaximum = std::max(orderedMaximum, reads);
    }
    uint32_t missing = 200001;
    assert(candidate_b::FindOrdered(200000, orderedRead, [&](uint32_t record) {
        return candidate_b::CompareOutline(record, 16, 1, &missing, 1, recordRead, view);
    }, address) == Result::Missing);
    assert(candidate_b::FindOrdered(1, failedRead, mismatch, address) == Result::IoError);
    assert(candidate_b::FindOrdered(1, orderedRead, [](uint32_t) { return 2; }, address) == Result::Corrupt);
    assert(candidate_b::FindOrdered(0, orderedRead, mismatch, address) == Result::Missing);
    assert(candidate_b::FindOrdered(1u << 21, orderedRead, mismatch, address) == Result::Corrupt);
    candidate_b::Tail orderedTail;
    assert(orderedTail.Append(16));
    uint32_t query = 2;
    reads = 0;
    assert(candidate_b::FindLatestOrdered(orderedTail, 200000, orderedRead, [&](uint32_t record) {
        return candidate_b::CompareOutline(record, 16, 1, &query, 1, recordRead, view);
    }, address) == Result::Found && address == 16 && reads == 0);
    query = 3;
    assert(candidate_b::FindLatestOrdered(orderedTail, 200000, orderedRead, [&](uint32_t record) {
        return candidate_b::CompareOutline(record, 16, 1, &query, 1, recordRead, view);
    }, address) == Result::Found && address == 32);
    std::cout << "200000 exact-key lookups; max " << maximum
              << " 12-byte table reads; 1028-byte bounded tail; collision/corruption/IO bounds passed\n";
    std::cout << "Ordered-address alternative: max " << orderedMaximum
              << " 4-byte table reads plus record comparisons; 800000-byte index\n";
}
