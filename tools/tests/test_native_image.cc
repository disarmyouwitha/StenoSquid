#include "native_store_fixture.h"
#include "native_provisioner.h"
#include <fstream>
#include <iostream>
int main(int argc,char **argv) {
    assert(argc==2);std::ifstream input(argv[1],std::ios::binary);
    std::vector<uint8_t> image((std::istreambuf_iterator<char>(input)),{});assert(image.size()==255*65536);
    Nor nor;nor.data.assign(256*65536,0x42);NativeDevice device{{nor}};
    NativeProvisioner<NativeDevice> installer(device);
    assert(installer.Begin(Crc(0,image.data(),image.size()))==ProvisionResult::Ok);
    for(unsigned i=0;i<255;++i)assert(installer.Erase(i)==ProvisionResult::Ok);
    auto write_range = [&](uint32_t start, uint32_t end) {
        for (uint32_t at = start; at < end; at += 512) {
            bool blank = true;
            for (unsigned i = 0; i < 512; ++i) blank &= image[at + i] == 255;
            if (!blank) assert(installer.Write(at, image.data() + at, 512) == ProvisionResult::Ok);
        }
    };
    write_range(2 * kSegmentBytes, image.size());
    NativeCatalog<NativeDevice> interrupted(device);
    CatalogSelection unpublished;
    assert(interrupted.Open(unpublished, [](uint32_t, const uint8_t *) { return true; }) ==
           CatalogResult::Empty);
    write_range(0, 2 * kSegmentBytes);
    assert(installer.Finish()==ProvisionResult::Ok);
    assert(std::equal(image.begin(),image.end(),nor.data.begin()));
    for(size_t i=image.size();i<nor.data.size();++i)assert(nor.data[i]==0x42);
    NativeStore<NativeDevice> store(device);NativeValidator<NativeDevice> validator(device,store.catalog);
    auto open=[&](){return store.Open([&](const CatalogRoot &r){return validator.Validate(r);});};
    assert(open()==CatalogResult::Ok);uint8_t row[96];assert(store.Row(0,row) && Word(row)==1);
    assert(store.Row(5,row) && Word(row)==6 && Word(row+4)==1);
    uint32_t key=8;const uint8_t text[]={'s','a','v','e','d'};
    assert(store.Edit(1,0,&key,1,text,5)==LiveResult::Ok);
    assert(open()==CatalogResult::Ok);LiveView view;assert(store.Snapshot(view));RecordView record;
    assert(store.live.FindSnapshot(view,1,&key,1,record)==LiveResult::Ok && record.text_bytes==5);assert(store.Release(view));
    // The factory restore is already in the committed indexes, including
    // multi-stroke records staged beyond the initial journal.
    assert(store.Snapshot(view));
    for (uint32_t i = 1; i <= 300; ++i) {
        const uint32_t outline[] = {0x1000 + i, 4};
        assert(store.live.FindSnapshot(view, 1, outline, 2, record) == LiveResult::Ok);
        std::string expected;
        for (int repetition = 0; repetition < 70; ++repetition) expected += "é" + std::to_string(i) + " ";
        assert(record.text_bytes == expected.size());
        assert(std::memcmp(nor.data.data() + record.text_address, expected.data(), expected.size()) == 0);
    }
    assert(store.Release(view));
    assert(store.Enable(2, false, view, [&](uint32_t i, const uint8_t *extent) {
        return validator.Extent(i, extent);
    }) == CatalogResult::Ok);
    assert(open() == CatalogResult::Ok);
    assert(store.Row(1, row) && Word(row + 4) == 0);
    assert(store.Row(2, row) && Word(row + 4) == 1);
    const auto &root = store.selected.root;
    std::vector<uint8_t> invalid(root.bytes);
    assert(device.Read(root.address, invalid.data(), invalid.size()));
    invalid[32 + 2 * 96 + 10] = invalid[32 + 96 + 10]; // Duplicate collection member.
    CatalogSelection rejected;
    assert(store.catalog.Publish(store.selected, invalid.size(), Source{invalid},
        [&](uint32_t i, const uint8_t *extent) { return validator.Extent(i, extent); },
        rejected) == CatalogResult::Corrupt);
    assert(open() == CatalogResult::Ok);
    assert(store.Row(1,row));const auto base_index=uint16_t(row[8]) | uint16_t(row[9])<<8;
    uint8_t extent[24];assert(device.Read(store.selected.root.address+32+7*96+base_index*24,extent,24));
    nor.data[uint16_t(extent[0])*65536]^=1;const auto damaged=nor.data;
    assert(open()!=CatalogResult::Ok && nor.data==damaged);
    std::cout<<"Native factory image: sparse USB provision contract, both catalog copies, relocated bases, persistent edit and corrupt-base refusal passed\n";
}
