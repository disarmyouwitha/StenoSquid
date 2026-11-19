#pragma once
#include "native_store_fixture.h"
#include "native_stage.h"
#include <fstream>

struct Plan {
    uint32_t metadata, bytes, crc;
    std::vector<std::pair<uint32_t, std::vector<uint8_t>>> blocks;
};

uint32_t ReadWord(std::istream &stream) {
    uint8_t bytes[4];
    stream.read(reinterpret_cast<char *>(bytes), sizeof(bytes));
    assert(stream);
    return Word(bytes);
}

Plan ReadPlan(const char *path) {
    std::ifstream file(path, std::ios::binary);
    const uint32_t count = ReadWord(file);
    Plan plan{ReadWord(file), ReadWord(file), ReadWord(file), {}};
    for (uint32_t i = 0; i < count; ++i) {
        const uint32_t segment = ReadWord(file);
        std::vector<uint8_t> body(ReadWord(file));
        file.read(reinterpret_cast<char *>(body.data()), body.size());
        assert(file);
        plan.blocks.emplace_back(segment, std::move(body));
    }
    return plan;
}

void Upload(NativeStage<NativeDevice> &stage, const Plan &plan) {
    for (const auto &[segment, block] : plan.blocks) {
        assert(stage.Erase(segment));
        for (unsigned offset = 0; offset < block.size(); offset += 512) {
            const uint32_t n = std::min(size_t(512), block.size() - offset);
            const auto *start = block.data() + offset;
            if (std::all_of(start, start + n, [](uint8_t v) { return v == 255; })) continue;
            assert(stage.Write(segment * kSegmentBytes + offset, start, n));
        }
    }
}

