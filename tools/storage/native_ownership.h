// Collect catalog/reader ownership before planning any staged extent. Does not
// erase or infer that unknown/corrupt metadata is free space.
#pragma once
#include "native_catalog.h"

namespace candidate_b {
template <class Device>
CatalogResult CollectOwnership(Device &device,NativeCatalog<Device> &catalog,
                              const CatalogRoot &current,SegmentMap &result) {
    // Fail closed even if a caller accidentally ignores the returned error.
    for(auto &word:result.words)word=UINT32_MAX;
    SegmentMap pending;pending.Add(0,2);pending.Add(255,1);
    if(!catalog.CanWrite(current))return CatalogResult::Quarantined;
    auto collect=[&](const CatalogRoot &root) {
        if(!catalog.IsTrusted(root))return false;
        uint8_t prefix[32];
        if(!device.Read(root.address,prefix,32))return false;
        uint32_t dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        uint32_t extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        if(dictionaries>64 || extents>253 || root.bytes!=32+dictionaries*96+extents*24)return false;
        for(uint32_t i=0;i<extents;++i) {
            uint8_t row[24];
            if(!device.Read(root.address+32+dictionaries*96+i*24,row,24))return false;
            uint32_t start=uint16_t(row[0]) | uint16_t(row[1])<<8,count=uint16_t(row[2]) | uint16_t(row[3])<<8;
            if(start<2 || start>=255 || !count || count>255-start || !pending.Add(start,count))return false;
        }
        return true;
    };
    bool found=false;
    for(uint8_t slot=0;slot<2;++slot) {
        CatalogRoot root;const auto status=catalog.Inspect(slot,root);
        if(status==CatalogResult::Empty)continue;
        if(status!=CatalogResult::Ok)return status;
        if(!collect(root))return CatalogResult::Corrupt;
        found |= root.generation==current.generation && root.crc==current.crc && root.address==current.address;
    }
    if(!found)return CatalogResult::Invalid;
    const auto status=catalog.VisitPins(collect);
    if(status==CatalogResult::Ok)result=pending;
    return status;
}
} // namespace candidate_b
