// Fixed-workspace merge operations over a pinned, validated live view. Caller
// serializes flash IO. Key comparisons trust immutable post-recovery payloads;
// output validation performs complete CRC checks before catalog publication.
#pragma once
#include "native_live_journal.h"
#include "native_index_sink.h"

namespace candidate_b {
struct MergeRecord {
    JournalRecord record{};
    uint32_t identity=0,strokes[16]{};
    uint32_t text_address=0;
    uint16_t text_bytes=0;
    uint8_t operation=0,count=0;
};
template <class Device>
class NativeMergeOps {
    Device &device;
    NativeJournal<Device> journal;
    const LiveView &view;
    IndexSink<Device> &forward,&reverse;
    uint32_t reverse_address=0,reverse_count=0;
public:
    NativeMergeOps(Device &value,const LiveView &input,IndexSink<Device> &f,IndexSink<Device> &r)
        :device(value),journal(value),view(input),forward(f),reverse(r){}
    void ReverseInput(uint32_t at,uint32_t count){reverse_address=at;reverse_count=count;}
    bool Active(uint32_t identity)const {
        for(uint32_t i=0;i<view.dictionary_count;++i)if(view.identities[i]==identity)return true;
        return false;
    }
    bool Record(uint32_t address,MergeRecord &out,bool full=false) {
        if(address/kSegmentBytes>=255 || address%kSegmentBytes<256 || address%4 ||
           !view.journals.Contains(address/kSegmentBytes))return false;
        JournalRecord record;
        if((full?journal.Read(address,record):journal.ReadKey(address,record))!=JournalResult::Ok ||
           record.sequence>view.highwater)return false;
        uint8_t prefix[72];
        if(!device.Read(address+32,prefix,8))return false;
        const uint8_t count=prefix[5];
        if(count<1 || count>16 || !device.Read(address+40,prefix+8,count*4))return false;
        out.record=record;out.identity=Word(prefix);out.operation=prefix[4];out.count=count;
        out.text_bytes=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        out.text_address=address+40+count*4;
        for(uint8_t i=0;i<count;++i)out.strokes[i]=Word(prefix+8+i*4);
        return true;
    }
    static int Keys(const MergeRecord &a,const MergeRecord &b) {
        if(a.identity!=b.identity)return a.identity<b.identity?-1:1;
        for(uint8_t i=0;i<a.count && i<b.count;++i)
            if(a.strokes[i]!=b.strokes[i])return a.strokes[i]<b.strokes[i]?-1:1;
        return a.count==b.count?0:a.count<b.count?-1:1;
    }
    int Operation(uint32_t at){MergeRecord record;return Record(at,record)?record.operation:-1;}
    int CompareKey(uint32_t a,uint32_t b) {
        MergeRecord x,y;if(!Record(a,x) || !Record(b,y))return 2;
        return Keys(x,y);
    }
    int CompareReverse(uint32_t a,uint32_t b) {
        MergeRecord x,y;if(!Record(a,x) || !Record(b,y) || x.operation || y.operation)return 2;
        uint8_t left[32],right[32];
        for(uint32_t at=0;at<x.text_bytes && at<y.text_bytes;) {
            uint32_t n=x.text_bytes-at;if(n>y.text_bytes-at)n=y.text_bytes-at;if(n>32)n=32;
            if(!device.Read(x.text_address+at,left,n) || !device.Read(y.text_address+at,right,n))return 2;
            for(uint32_t i=0;i<n;++i)if(left[i]!=right[i])return left[i]<right[i]?-1:1;
            at+=n;
        }
        return x.text_bytes==y.text_bytes?Keys(x,y):x.text_bytes<y.text_bytes?-1:1;
    }
    bool ReadIndex(bool rev,uint32_t position,uint32_t &address) {
        if(position>=(rev?reverse_count:view.forward_count))return false;
        uint8_t bytes[4];
        if(!device.Read((rev?reverse_address:view.forward_address)+position*4,bytes,4))return false;
        address=Word(bytes);MergeRecord record;
        return Record(address,record) && record.record.sequence<=view.through;
    }
    bool Emit(bool rev,uint32_t address) {
        MergeRecord record;if(!Record(address,record))return false;
        // Deleted identities are never reused; their old overlays must not be
        // retained forever merely because an old checkpoint references them.
        if(!Active(record.identity))return true;
        return rev?reverse.Append(address):forward.Append(address);
    }
};
} // namespace candidate_b
