#pragma once
#include <vector>
#include <cstdint>
#include <cstring>
#include <cassert>
namespace native_test {
struct Cut {};
struct Nor {
    std::vector<uint8_t> data=std::vector<uint8_t>(7*65536,255);
    uint32_t operations=0,cut=0,tear=0;
    bool fail_read=false,fail_program=false,fail_erase=false;
    bool Read(uint32_t at,uint8_t *out,uint32_t n) {
        if(fail_read || at>data.size() || n>data.size()-at)return false;
        std::memcpy(out,data.data()+at,n);return true;
    }
    bool Program(uint32_t at,const uint8_t *in,uint32_t n) {
        if(fail_program)return false;
        assert(n && at/256==(at+n-1)/256 && at+n<=data.size());
        for(uint32_t i=0;i<n;++i)assert((data[at+i]&in[i])==in[i]);
        bool stop=++operations==cut;
        uint32_t amount=stop?(tear<n?tear:n):n;
        for(uint32_t i=0;i<amount;++i)data[at+i]&=in[i];
        if(stop)throw Cut{};
        return true;
    }
    bool EraseCatalog(uint32_t at,uint32_t size) {
        if(fail_erase)return false;
        assert((at==0 || at==65536) && size==65536);
        return EraseBytes(at,size);
    }
    bool EraseSegment(uint32_t segment) {
        assert(segment>=2 && segment<7);
        return EraseBytes(segment*65536,65536);
    }
    bool EraseBytes(uint32_t at,uint32_t size) {
        for(uint32_t offset=0;offset<size;offset+=4096) {
            bool stop=++operations==cut;
            uint32_t n=stop?(tear<4096?tear:4096):4096;
            std::memset(data.data()+at+offset,255,n);
            if(stop)throw Cut{};
        }
        return true;
    }
};
struct Source {
    const std::vector<uint8_t> &bytes;
    bool operator()(uint32_t at,uint8_t *out,uint32_t n) {
        if(at>bytes.size() || n>bytes.size()-at)return false;
        std::memcpy(out,bytes.data()+at,n);return true;
    }
};
}
