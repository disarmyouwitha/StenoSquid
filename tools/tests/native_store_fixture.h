#pragma once
#include "native_live_fixture.h"
#include <cstdio>
#include "native_store.h"
#include "native_validator.h"
struct NativeDevice : LiveDevice {
    bool CheckBase(uint32_t at,uint32_t n,uint32_t expected){uint32_t crc;return Checksum(at,n,crc) && crc==expected;}
    bool Checksum(uint32_t at,uint32_t n,uint32_t &crc) {
        if(nor.fail_read || at>nor.data.size() || n>nor.data.size()-at)return false;
        crc=Crc(0,nor.data.data()+at,n);return true;
    }
};
struct NativeFixture {
    Nor nor;
    NativeDevice device{{nor}};
    NativeStore<NativeDevice> store{device};
    NativeValidator<NativeDevice> validator{device,store.catalog};
    NativeFixture() {
        nor.data.resize(256*65536,255);NativeJournal<NativeDevice> journal(device);
        assert(journal.Initialize(2,1)==JournalResult::Ok);
        std::vector<uint8_t> data(32+3*96+24,0);PutWord(data.data(),4);data[4]=3;data[6]=1;
        const char *names[]={"user_dictionary","Upper","Lower"};
        for(unsigned i=0;i<3;++i) {
            auto *row=data.data()+32+i*96;PutWord(row,i+1);PutWord(row+4,1);row[8]=row[9]=255;
            row[12]=std::strlen(names[i]);std::memcpy(row+16,names[i],row[12]);
        }
        auto *extent=data.data()+32+3*96;extent[0]=2;extent[2]=1;PutWord(extent+4,65536);PutWord(extent+12,2);PutWord(extent+16,1);
        CatalogSelection empty,selection;
        assert(store.catalog.Publish(empty,data.size(),Source{data},[&](uint32_t i,const uint8_t *r){return validator.Extent(i,r);},selection)==CatalogResult::Ok);
        Open();
    }
    void Open(){assert(store.Open([&](const CatalogRoot &r){return validator.Validate(r);})==CatalogResult::Ok);}
    auto Check(){return [&](uint32_t i,const uint8_t *r){return validator.Extent(i,r);};}
    void Finish() {
        for(unsigned i=0;i<2000000;++i) {
            auto result=store.Step(Check());if(result==JobResult::Done)return;assert(result==JobResult::More);
        }
        assert(false);
    }
    LiveResult Edit(uint32_t id,uint32_t key,const char *text,uint8_t operation=0) {
        return store.Edit(id,operation,&key,1,reinterpret_cast<const uint8_t *>(text),std::strlen(text));
    }
};
