#include "compact_search.h"
#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <set>
#include <vector>

static size_t words, bytes;
static uint32_t Word(const void *p) {
    ++words;
    uint32_t result;
    std::memcpy(&result, p, sizeof(result));
    return result;
}
static uint8_t Byte(const void *p) {
    ++bytes;
    return *static_cast<const uint8_t *>(p);
}

int main(int argc, char **argv) {
    assert(argc == 3);
    std::ifstream source(argv[1], std::ios::binary);
    std::vector<uint8_t> data(std::istreambuf_iterator<char>(source), {});
    const uintptr_t start = reinterpret_cast<uintptr_t>(data.data());
    StenoSquidCompactSearch view(start, data.size(), start + Word(data.data() + 12), Word(data.data() + 8), Word, Byte);
    assert(view.Available());
    StenoSquidCompactSearch::Range range;
    StenoSquidCompactSearch::Entry entry;
    const bool large = std::strcmp(argv[2], "large") == 0;
    words = bytes = 0;
    const char *query = large ? "translation 19" : "cat";
    assert(view.TextRange(query, std::strlen(query), false, range));
    const size_t reverse_words = words, reverse_bytes = bytes;
    assert(range.end - range.begin == (large ? 67 : 3));
    unsigned previous = 0;
    for (uint32_t i = range.begin; i < range.end; ++i) {
        assert(view.ReadTextEntry(i, entry) && std::strcmp(reinterpret_cast<const char *>(entry.text), query) == 0);
        assert(entry.length >= previous);
        previous = entry.length;
    }
    if (!large) {
        assert(view.TextRange("cat", 3, true, range) && range.end - range.begin == 4);
        unsigned strict = 0;
        for (uint32_t i = range.begin; i < range.end; ++i) {
            assert(view.ReadTextEntry(i, entry));
            strict += view.IsStrictPrefix(entry, 3);
        }
        assert(strict == 1);
        assert(view.TextRange("ç", 2, true, range) && range.end - range.begin == 1);
        assert(view.TextRange("absent", 6, false, range) && range.begin == range.end);
    }
    words = bytes = 0;
    assert(view.OutlineRange(large ? 500 : 524552, range));
    const size_t partial_words = words;
    assert(range.end - range.begin == (large ? 195 : 2));
    std::set<std::vector<uint32_t>> outlines;
    for (uint32_t i = range.begin; i < range.end; ++i) {
        assert(view.ReadOutlineEntry(i, entry));
        assert(entry.length > 1);
        const std::vector<uint32_t> outline(entry.strokes, entry.strokes + entry.length);
        assert(outlines.insert(outline).second);
    }
    if (!large) {
        assert(outlines.count({4, 524552}) && outlines.count({524552, 524552, 8}));
        assert(view.OutlineRange(0x400000, range) && range.begin == range.end);
        const size_t footer = data.size() - 20;
        data[footer + 8] = 0xff;
        StenoSquidCompactSearch corrupt(start, data.size(), start + Word(data.data() + 12), Word(data.data() + 8), Word, Byte);
        assert(!corrupt.Available());
    }
    std::cout << (large ? "200K" : "Small") << " compact search: "
              << reverse_words << " word + " << reverse_bytes << " byte reads for an exact-text range; "
              << partial_words << " word reads for a partial-outline bucket; query view "
              << sizeof(view) << " bytes on host\n";
}
