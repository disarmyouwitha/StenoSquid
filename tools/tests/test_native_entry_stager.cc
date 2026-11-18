#include "native_store_fixture.h"
#include "native_entry_stager.h"
#include <iostream>
int main() {
    NativeFixture f;NativeEntryStager stage;uint32_t stroke=8;
    std::string text;for(unsigned i=0;i<1000;++i)text+="ç";text+=std::string(47,'x');assert(text.size()==2047);
    assert(stage.Begin(1,&stroke,1,text.size())==LiveResult::Ok);
    auto save=[&](uint32_t id,const uint32_t *keys,uint8_t count,const uint8_t *value,uint16_t bytes){return f.store.Edit(id,0,keys,count,value,bytes);};
    assert(stage.Commit(save)==LiveResult::Invalid);
    for(unsigned at=0;at<text.size();at+=512) {
        unsigned n=text.size()-at;if(n>512)n=512;
        const auto *bytes=reinterpret_cast<const uint8_t *>(text.data()+at);
        assert(stage.Write(at,bytes,n)==LiveResult::Ok && stage.Write(at,bytes,n)==LiveResult::Ok);
    }
    assert(f.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
    assert(stage.Commit(save)==LiveResult::Busy);f.Finish();
    assert(stage.Commit(save)==LiveResult::Ok);const auto operations=f.nor.operations;
    assert(stage.Commit(save)==LiveResult::Ok && operations==f.nor.operations);
    f.Open();LiveView view;assert(f.store.Snapshot(view));RecordView record;
    assert(f.store.live.FindSnapshot(view,1,&stroke,1,record)==LiveResult::Ok && record.text_bytes==2047);
    std::string read(2047,'x');assert(f.store.live.ReadText(view,record,reinterpret_cast<uint8_t *>(read.data()),2047)==LiveResult::Ok && read==text);
    assert(f.store.Release(view));stage.Abort();
    assert(stage.Begin(1,&stroke,1,0)==LiveResult::Ok && stage.Commit(save)==LiveResult::Ok);
    stage.Abort();const auto before=f.nor.data;const uint8_t nul=0;
    assert(stage.Begin(1,&stroke,1,1)==LiveResult::Ok && stage.Write(0,&nul,1)==LiveResult::Ok && stage.Commit(save)==LiveResult::Invalid);
    assert(f.nor.data==before);
    stage.Abort();assert(stage.Begin(1,&stroke,1,2048)==LiveResult::Invalid);
    std::cout<<"Native entry transfer: maximum UTF-8, empty text, chunk/commit retries, maintenance backpressure and NUL refusal passed\n";
}
