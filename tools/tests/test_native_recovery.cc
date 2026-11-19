#include "native_store_fixture.h"
#include <fstream>

int main(int argc, char **argv) {
    assert(argc == 2);
    std::ifstream input(argv[1], std::ios::binary);
    NativeFixture fixture;
    fixture.nor.data.assign(std::istreambuf_iterator<char>(input), {});
    fixture.nor.data.resize(256 * kSegmentBytes, 255);
    fixture.Open();
    uint8_t row[96];
    assert(fixture.store.Row(1, row));
    assert(Word(row) == 8 && row[11] == kUserSnapshot && !std::memcmp(row + 16, "Smoke", 5));
    RecordView record;
    const uint32_t kat = 524552, bob = 66096;
    assert(fixture.store.live.FindLive(8, &kat, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Set && record.text_bytes == 7);
    assert(fixture.store.live.FindLive(8, &bob, 1, record) == LiveResult::Ok);
    assert(record.operation == Operation::Remove);
}
