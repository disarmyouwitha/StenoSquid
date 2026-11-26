// Fixed-workspace merge operations over a pinned, validated live view. Caller
// serializes flash IO. Key comparisons trust immutable post-recovery payloads;
// output validation performs complete CRC checks before catalog publication.
#pragma once
#include "native_live_journal.h"
#include "native_index_sink.h"
#include "native_relocation.h"

namespace candidate_b {
struct MergeRecord : JournalKey {
    JournalRecord record{};
    uint32_t text_address = 0;
};
template <class Device>
class NativeMergeOps {
    Device &device;
    NativeJournal<Device> journal;
    const LiveView &view;
    IndexSink<Device> &forward,&reverse;
    uint32_t reverse_address=0,reverse_count=0;
    const NativeRelocationMap *relocation=nullptr;
public:
    NativeMergeOps(Device &value,const LiveView &input,IndexSink<Device> &f,IndexSink<Device> &r)
        :device(value),journal(value),view(input),forward(f),reverse(r){}
    void Relocation(const NativeRelocationMap *value){relocation=value;}
    NativeJournal<Device> &Journal(){return journal;}
    void ReverseInput(uint32_t at,uint32_t count){reverse_address=at;reverse_count=count;}
    bool Active(uint32_t identity)const {
        for(uint32_t i=0;i<view.dictionary_count;++i)if(view.identities[i]==identity)return true;
        return false;
    }
    bool Record(uint32_t address,MergeRecord &out,bool full=false,bool index_key=false) {
        const uint32_t segment = address / kSegmentBytes;
        const bool staged = relocation && relocation->destination == segment;
        if(segment>=kLargePoolEnd || address%kSegmentBytes<256 || address%4 ||
           (!staged && !view.journals.Contains(segment)))return false;
        const auto status = full ? journal.Read(address, out.record, &out)
                                 : index_key ? journal.ReadIndexKey(address, out.record, out)
                                             : journal.ReadKey(address, out.record, out);
        if (status != JournalResult::Ok || out.record.sequence > (staged ? relocation->through : view.highwater)) return false;
        out.text_address = address + 40u + 4u * out.count;
        return true;
    }
    static int Keys(const MergeRecord &a,const MergeRecord &b) {
        return CompareOutlineKey(a, b.identity, b.strokes, b.count);
    }
    int Operation(uint32_t at){MergeRecord record;return Record(at,record)?record.operation:-1;}
    int CompareKey(uint32_t a,uint32_t b) {
        MergeRecord x,y;if(!Record(a,x) || !Record(b,y))return 2;
        return Keys(x,y);
    }
    int CompareTo(uint32_t address, const MergeRecord &target, bool reverse) {
        MergeRecord record;
        if (!Record(address, record)) return 2;
        return reverse ? ReverseKeys(record, target) : Keys(record, target);
    }
    int CompareReverse(uint32_t a, uint32_t b) {
        MergeRecord target;
        return Record(b, target) ? CompareTo(a, target, true) : 2;
    }
    int ReverseKeys(const MergeRecord &x, const MergeRecord &y,
                    const uint8_t *left_prefix = nullptr, const uint8_t *right_prefix = nullptr) {
        if (x.operation || y.operation) return 2;
        uint8_t left[32],right[32];
        for(uint32_t at=0;at<x.text_bytes && at<y.text_bytes;) {
            uint32_t n=x.text_bytes-at;if(n>y.text_bytes-at)n=y.text_bytes-at;if(n>32)n=32;
            const uint8_t *x_text = at == 0 ? left_prefix : nullptr;
            const uint8_t *y_text = at == 0 ? right_prefix : nullptr;
            if (!x_text) {
                if (!device.Read(x.text_address+at,left,n)) return 2;
                x_text = left;
            }
            if (!y_text) {
                if (!device.Read(y.text_address+at,right,n)) return 2;
                y_text = right;
            }
            for(uint32_t i=0;i<n;++i)if(x_text[i]!=y_text[i])return x_text[i]<y_text[i]?-1:1;
            at+=n;
        }
        return x.text_bytes == y.text_bytes ? CompareReverseKeyTie(x, y.identity, y.strokes, y.count)
                                            : x.text_bytes < y.text_bytes ? -1 : 1;
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
        if(relocation)address=relocation->Resolve(address);
        return rev?reverse.Append(address):forward.Append(address);
    }
};
} // namespace candidate_b
