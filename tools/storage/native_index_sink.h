// Bounded writer for an unpublished ordered-address index extent. Caller must
// authorize an erased extent, reserve worst-case capacity, and publish only after
// Finish plus complete semantic validation. This never changes a catalog/root.
#pragma once
#include "native_journal.h"

namespace candidate_b {
template <class Device>
class IndexSink {
    Device &device;
    uint32_t start=0,capacity=0,written=0,used=0,count=0,crc=0;
    bool ready=false,finished=false,failed=false;
    alignas(4) uint8_t page[256]{};
    bool Flush() {
        if(!used)return true;
        if(!device.Program(start+written,page,used)){failed=true;return false;}
        uint8_t check[32];
        for(uint32_t offset=0;offset<used;) {
            uint32_t n=used-offset;if(n>sizeof(check))n=sizeof(check);
            if(!device.Read(start+written+offset,check,n) || std::memcmp(check,page+offset,n)) {
                failed=true;return false;
            }
            offset+=n;
        }
        crc=Crc(crc,page,used);written+=used;used=0;return true;
    }
public:
    explicit IndexSink(Device &value):device(value){}
    bool Begin(uint32_t address,uint32_t bytes) {
        if(ready && !finished && !failed)return false;
        if(address%256 || bytes%4 || !bytes || bytes>0x1000000 || address<2*kSegmentBytes ||
           address>255*kSegmentBytes || bytes>255*kSegmentBytes-address)return false;
        start=address;capacity=bytes;written=used=count=crc=0;finished=failed=false;ready=true;return true;
    }
    bool Append(uint32_t address) {
        if(!ready || failed || finished)return false;
        if(written+used>capacity || capacity-written-used<4 ||
           address<2*kSegmentBytes+256 || address>=255*kSegmentBytes || address%4){failed=true;return false;}
        PutWord(page+used,address);used+=4;++count;
        return used<256 || Flush();
    }
    bool Finish() {
        if(!ready || failed)return false;
        if(finished)return true;
        if(!Flush())return false;
        finished=true;return true;
    }
    void Cancel(){ready=false;failed=true;finished=false;}
    uint32_t Bytes()const{return written;}
    uint32_t Count()const{return count;}
    uint32_t Checksum()const{return crc;}
    bool Finished()const{return finished;}
};
} // namespace candidate_b
