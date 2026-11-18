// Explicit destructive clean provisioning session, used only by the opt-in USB
// installer. No automatic formatting, migrations or writes outside the pool.
#pragma once
#include "native_allocator.h"
#include <cstring>
namespace candidate_b {
enum class ProvisionResult { Ok, Invalid, Busy, Incomplete, IoError, ChecksumMismatch };
template<class Device,uint32_t Segments=255>
class NativeProvisioner {
    static_assert(Segments>=3 && Segments<=255);
    static constexpr uint32_t Bytes=Segments*65536;
    Device &device;
    SegmentMap erased{};
    uint32_t expected=0;
    bool active=false,finished=false;
    alignas(4) uint8_t readback[512];
    bool AllErased()const{for(unsigned i=0;i<Segments;++i)if(!erased.Contains(i))return false;return true;}
public:
    explicit NativeProvisioner(Device &value):device(value){}
    bool Active()const{return active;}
    ProvisionResult Begin(uint32_t checksum) {
        if(active)return ProvisionResult::Busy;
        erased={};expected=checksum;active=true;finished=false;return ProvisionResult::Ok;
    }
    void Abort(){active=finished=false;}
    ProvisionResult Erase(uint32_t segment) {
        if(!active || segment>=Segments)return ProvisionResult::Invalid;
        if(erased.Contains(segment))return ProvisionResult::Ok;
        if(!(segment<2?device.EraseCatalog(segment*65536,65536):device.Erase(segment)))return ProvisionResult::IoError;
        erased.Add(segment,1);return ProvisionResult::Ok;
    }
    ProvisionResult Write(uint32_t at,const uint8_t *data,uint32_t bytes) {
        if(!active || !data || !bytes || bytes>512 || bytes%4 || at%256 || at>=Bytes || bytes>Bytes-at)return ProvisionResult::Invalid;
        if(!AllErased())return ProvisionResult::Incomplete;
        if(!device.Read(at,readback,bytes))return ProvisionResult::IoError;
        for(uint32_t i=0;i<bytes;++i)if((readback[i]&data[i])!=data[i])return ProvisionResult::Invalid;
        for(uint32_t i=0;i<bytes;i+=256) {
            uint32_t n=bytes-i;if(n>256)n=256;
            if(std::memcmp(readback+i,data+i,n) && !device.Program(at+i,data+i,n))return ProvisionResult::IoError;
        }
        if(!device.Read(at,readback,bytes) || std::memcmp(readback,data,bytes))return ProvisionResult::IoError;
        return ProvisionResult::Ok;
    }
    ProvisionResult Finish() {
        if(finished)return ProvisionResult::Ok;
        if(!active)return ProvisionResult::Invalid;
        if(!AllErased())return ProvisionResult::Incomplete;
        uint32_t crc;
        if(!device.Checksum(0,Bytes,crc))return ProvisionResult::IoError;
        if(crc!=expected)return ProvisionResult::ChecksumMismatch;
        active=false;finished=true;return ProvisionResult::Ok;
    }
};
}
