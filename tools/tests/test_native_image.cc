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
    for(uint32_t at=0;at<image.size();at+=512) {
        bool blank=true;for(unsigned i=0;i<512;++i)blank&=image[at+i]==255;
        if(!blank)assert(installer.Write(at,image.data()+at,512)==ProvisionResult::Ok);
    }
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
    assert(store.Row(1,row));const auto base_index=uint16_t(row[8]) | uint16_t(row[9])<<8;
    uint8_t extent[24];assert(device.Read(store.selected.root.address+32+7*96+base_index*24,extent,24));
    nor.data[uint16_t(extent[0])*65536]^=1;const auto damaged=nor.data;
    assert(open()!=CatalogResult::Ok && nor.data==damaged);
    std::cout<<"Native factory image: sparse USB provision contract, both catalog copies, relocated bases, persistent edit and corrupt-base refusal passed\n";
}
