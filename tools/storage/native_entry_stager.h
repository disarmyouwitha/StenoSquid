// Bounded, retry-safe UTF-8 entry transfer. No writes until Commit succeeds.
#pragma once
#include "native_live_journal.h"
#include <cstring>
namespace candidate_b {
class NativeEntryStager {
    bool active=false,committed=false;
    uint32_t identity=0,received=0,bytes=0,outline[16]{};
    uint8_t count=0,text[2048]{};
public:
    void Abort(){active=committed=false;}
    LiveResult Begin(uint32_t id,const uint32_t *strokes,uint8_t length,uint32_t text_bytes) {
        if(active && !committed)return LiveResult::Busy;
        if(!id || !strokes || !length || length>16 || text_bytes>2047)return LiveResult::Invalid;
        for(unsigned i=0;i<length;++i)if(!ValidNativeStroke(strokes[i]))return LiveResult::Invalid;
        identity=id;count=length;bytes=text_bytes;received=0;
        for(unsigned i=0;i<count;++i)outline[i]=strokes[i];
        active=true;committed=false;return LiveResult::Ok;
    }
    LiveResult Write(uint32_t offset,const uint8_t *data,uint32_t n) {
        if(!active || !data || !n || n>512 || offset>received || offset>bytes || n>bytes-offset)return LiveResult::Invalid;
        if(offset<received)return n<=received-offset && !std::memcmp(text+offset,data,n)?LiveResult::Ok:LiveResult::Invalid;
        std::memcpy(text+offset,data,n);received+=n;return LiveResult::Ok;
    }
    template<class Save>
    LiveResult Commit(Save save) {
        if(!active || received!=bytes)return LiveResult::Invalid;
        if(committed)return LiveResult::Ok;
        if(!ValidEdit(identity,0,outline,count,text,bytes))return LiveResult::Invalid;
        const auto status=save(identity,outline,count,text,uint16_t(bytes));
        if(status==LiveResult::Ok)committed=true;
        return status;
    }
};
}
