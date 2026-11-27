// Candidate-B bounded binary catalog. Development format, not a migration reader.
#pragma once
#include "native_journal.h"
#include "native_allocator.h"

namespace candidate_b {
constexpr uint32_t kCatalogHeader = 32, kCatalogPrefix = 32;
constexpr uint32_t kDictionaryBytes = 96, kExtentBytes = 24;
constexpr uint16_t kNoExtent = 65535;
constexpr uint8_t kUserSnapshot = 1;
constexpr uint8_t kFactoryAbsent = 2;

inline const char *FactoryName(uint32_t id) {
    switch (id) {
    case 2: return "Lapwing Commands";
    case 3: return "Lapwing Numbers";
    case 4: return "Lapwing UK Additions";
    case 5: return "Lapwing Proper Nouns";
    case 6: return "Lapwing Base";
    case 7: return "Plover Base";
    default: return nullptr;
    }
}
enum class CatalogResult { Ok, Empty, Corrupt, IoError, Invalid, Quarantined, Busy };
enum class CatalogValidation { Independent, ReuseIdenticalPayload };
enum class ExtentKind : uint32_t { Base=1, Journal=2, Forward=3, Reverse=4 };
// Required marker in the forward extent's 64-bit auxiliary field.
constexpr uint32_t kOutlineFirst = 1;
struct CatalogRoot {
    uint64_t generation=0;
    uint32_t address=0, bytes=0, crc=0;
};
struct CatalogShape {
    uint32_t next_id=0;
    uint16_t dictionaries=0, extents=0;
    uint64_t through=0;
};
struct CatalogSelection {
    CatalogRoot root{};
    bool quarantine=false;
    uint8_t slot=0;
};

// Device: Read/Program plus EraseCatalog(address,bytes). Caller authorizes and
// protects the two distinct catalog regions; this core never erases on open.
template <class Device>
class NativeCatalog {
    Device &device;
    uint32_t slots[2], capacity;
    uint8_t buffer[256]{};
    struct Pin { uint64_t generation=0;uint32_t token=0;uint8_t slot=0; } pins[4];
    CatalogRoot trusted[2]{};
    bool write_blocked=true;
    uint32_t next_token=1;
    bool Bounds() const {
        const uint32_t end = NativePoolEnd(device)*kSegmentBytes;
        return capacity>=256 && capacity<=65536 && !(capacity%256) &&
            !(slots[0]%256) && !(slots[1]%256) &&
            capacity<=end && slots[0]<=end-capacity && slots[1]<=end-capacity &&
            (slots[0]+capacity<=slots[1] || slots[1]+capacity<=slots[0]);
    }
    bool Write(uint32_t at,const uint8_t *data,uint32_t size) {
        while(size) {
            uint32_t n=256-at%256; if(n>size)n=size;
            if(!device.Program(at,data,n))return false;
            at+=n; data+=n; size-=n;
        }
        return true;
    }
    bool SamePayload(const CatalogRoot &first, const CatalogRoot &second) {
        if (first.bytes != second.bytes || first.crc != second.crc) return false;
        // Inspect already checked both headers and payload CRCs. Compare the
        // bytes as well so a CRC collision cannot skip validation.
        constexpr uint32_t granule = sizeof(buffer) / 2;
        for (uint32_t offset = 0; offset < first.bytes; offset += granule) {
            const uint32_t count = first.bytes - offset < granule ? first.bytes - offset : granule;
            if (!device.Read(first.address + offset, buffer, count) ||
                !device.Read(second.address + offset, buffer + granule, count) ||
                std::memcmp(buffer, buffer + granule, count) != 0) return false;
        }
        return true;
    }
public:
    NativeCatalog(Device &value,uint32_t a=0,uint32_t b=65536,uint32_t bytes=65536)
        :device(value),slots{a,b},capacity(bytes){}

    // Before any out-of-band erase/reset. Existing tokens can still be released,
    // but cannot authorize reads or writes until a validated reopen.
    void Invalidate(){trusted[0]={};trusted[1]={};write_blocked=true;}

    uint32_t PayloadCapacity()const{return Bounds()?capacity-32:0;}

    CatalogRoot TrustedRoot(uint8_t slot) const { return slot < 2 ? trusted[slot] : CatalogRoot{}; }

    bool IsTrusted(const CatalogRoot &root)const {
        for(const auto &known:trusted)
            if(root.generation && root.generation==known.generation && root.address==known.address &&
               root.bytes==known.bytes && root.crc==known.crc)return true;
        return false;
    }
    bool IsLatestTrusted(const CatalogRoot &root)const {
        if(!IsTrusted(root))return false;
        for(const auto &other:trusted)if(other.generation>root.generation)return false;
        return true;
    }
    bool SameTrustedPayload(const CatalogRoot &first, const CatalogRoot &second) {
        if (!IsTrusted(first) || !IsTrusted(second)) return false;
        return first.address == second.address || SamePayload(first, second);
    }
    bool CanWrite(const CatalogRoot &root)const{return !write_blocked && IsLatestTrusted(root);}
    bool HasPin(uint32_t token,const CatalogRoot &root)const {
        if(!token || !IsTrusted(root))return false;
        for(const auto &pin:pins)if(pin.token==token && pin.generation==root.generation &&
                                   root.address==slots[pin.slot]+32)return true;
        return false;
    }

    // Serialize with Open/Publish. Pins protect catalog metadata itself as well
    // as the extents the allocator must collect through VisitPins. No hidden heap.
    bool AcquirePin(const CatalogRoot &root,uint32_t &token) {
        token=0;
        if(!next_token || next_token==UINT32_MAX)return false;
        for(uint8_t slot=0;slot<2;++slot)if(root.address==slots[slot]+32) {
            // Open/publication validated this immutable root. No flash reads on
            // the per-lookup pin path. Scrub/reopen handles later corruption.
            if(!IsTrusted(root))return false;
            for(auto &pin:pins)if(!pin.token) {
                token=next_token++;pin={root.generation,token,slot};return true;
            }
            return false;
        }
        return false;
    }
    bool ReleasePin(uint32_t token) {
        if(!token)return false;
        for(auto &pin:pins)if(pin.token==token){pin={};return true;}
        return false;
    }
    bool DestinationPinned(const CatalogSelection &current) const {
        if(current.slot>1)return true;
        const uint8_t destination=uint8_t(1-current.slot);
        for(const auto &pin:pins)if(pin.token && (!current.root.generation || pin.slot==destination))return true;
        return false;
    }
    template <class Visit>
    CatalogResult VisitPins(Visit visit) {
        for(const auto &pin:pins)if(pin.token) {
            CatalogRoot root;
            auto status=Inspect(pin.slot,root);if(status!=CatalogResult::Ok)return status;
            if(root.generation!=pin.generation)return CatalogResult::Corrupt;
            if(!visit(root))return CatalogResult::Corrupt;
        }
        return CatalogResult::Ok;
    }

    CatalogResult Inspect(uint8_t slot,CatalogRoot &root) {
        root={};
        if(slot>1 || !Bounds())return CatalogResult::Invalid;
        uint8_t header[32];
        if(!device.Read(slots[slot],header,32))return CatalogResult::IoError;
        bool blank=true; for(auto byte:header)blank&=byte==255;
        if(blank)return CatalogResult::Empty;
        // An unfinished alternate copy is ignored; committed malformed data
        // must be reported distinctly so the caller can quarantine its extents.
        if(Word(header+28)!=kCommit)return CatalogResult::Empty;
        const uint64_t generation=uint64_t(Word(header+8)) | uint64_t(Word(header+12))<<32;
        const uint32_t size=Word(header+16),crc=Word(header+20);
        if(std::memcmp(header,"SQBCAT02",8) || !generation || size<kCatalogPrefix ||
           size>capacity-32 || size%4 || Word(header+24)!=Crc(0,header,24))return CatalogResult::Corrupt;
        uint32_t checksum=0;
        for(uint32_t offset=0;offset<size;) {
            uint32_t n=size-offset;if(n>sizeof(buffer))n=sizeof(buffer);
            if(!device.Read(slots[slot]+32+offset,buffer,n))return CatalogResult::IoError;
            checksum=Crc(checksum,buffer,n);offset+=n;
        }
        if(checksum!=crc)return CatalogResult::Corrupt;
        root={generation,slots[slot]+32,size,crc};
        return CatalogResult::Ok;
    }

    // Shape/ownership validation is streamed; no catalog-sized RAM mirror.
    // CheckExtent must verify actual extent hash/content/type and journal/index
    // cross references before this catalog may drive engine lookups or writes.
    template <class Reader,class CheckExtent>
    static CatalogResult ValidatePayload(Reader &input,uint32_t limit,const CatalogRoot &root,CheckExtent check,CatalogShape *shape=nullptr,
                                         uint32_t pool_end=kSmallPoolEnd) {
        const uint32_t end=pool_end*kSegmentBytes;
        if(limit<256 || limit>65536 || !root.generation || root.bytes<32 ||
           root.bytes>limit-32 || root.bytes>end || root.address>end-root.bytes)return CatalogResult::Invalid;
        uint8_t prefix[32];
        if(!input.Read(root.address,prefix,32))return CatalogResult::IoError;
        CatalogShape header;
        header.next_id=Word(prefix);
        header.dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        header.extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        header.through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        if(!header.next_id || header.dictionaries>64 || header.extents>253 ||
           root.bytes!=32+header.dictionaries*kDictionaryBytes+header.extents*kExtentBytes)return CatalogResult::Corrupt;
        for(unsigned i=16;i<32;++i)if(prefix[i])return CatalogResult::Corrupt;
        uint32_t ids[64]{};
        uint16_t base_members[64]{};
        SegmentMap occupied, bases;
        occupied.Add(0,2);occupied.Add(pool_end,1);
        unsigned forwards=0,reverses=0;
        const uint32_t extent_start=root.address+32+header.dictionaries*kDictionaryBytes;
        for(uint32_t i=0;i<header.extents;++i) {
            uint8_t extent[24];
            if(!input.Read(extent_start+i*24,extent,24))return CatalogResult::IoError;
            uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8;
            uint32_t count=uint16_t(extent[2]) | uint16_t(extent[3])<<8;
            uint32_t bytes=Word(extent+4),kind=Word(extent+12);
            uint64_t epoch=uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32;
            if(start<2 || start>=pool_end || !count || count>pool_end-start || !bytes || bytes>count*kSegmentBytes ||
               kind<1 || kind>4 || (kind==2 && (count!=1 || !epoch)) ||
               (kind!=2 && epoch!=(kind==3 ? kOutlineFirst : 0)) ||
               ((kind==3 || kind==4) && bytes%4))return CatalogResult::Corrupt;
            for(uint32_t j=start;j<start+count;++j)if(occupied.Contains(j))return CatalogResult::Corrupt;
            occupied.Add(start,count);
            forwards+=kind==3;reverses+=kind==4;
            if(forwards>1 || reverses>1)return CatalogResult::Corrupt;
            if(!check(i,extent))return CatalogResult::Corrupt;
        }
        for(uint32_t i=0;i<header.dictionaries;++i) {
            uint8_t dictionary[96];
            if(!input.Read(root.address+32+i*96,dictionary,96))return CatalogResult::IoError;
            uint32_t id=Word(dictionary),flags=Word(dictionary+4);
            uint16_t base=uint16_t(dictionary[8]) | uint16_t(dictionary[9])<<8;
            uint8_t name= dictionary[12];
            const uint8_t format = dictionary[11];
            if (FactoryName(id) && dictionary[10]) return CatalogResult::Corrupt;
            if (format == kFactoryAbsent) {
                const char *expected = FactoryName(id);
                bool has_pin = false;
                for (unsigned j = 64; j < sizeof(dictionary); ++j) has_pin |= dictionary[j] != 0;
                if (!expected || name != std::strlen(expected) ||
                    std::memcmp(dictionary + 16, expected, name) || !has_pin ||
                    flags || base != kNoExtent || dictionary[10]) return CatalogResult::Corrupt;
            } else if (format && (format != kUserSnapshot || base == kNoExtent ||
                                 dictionary[10] || FactoryName(id))) {
                return CatalogResult::Corrupt;
            }
            if(!id || id>=header.next_id || flags>1 || Word(dictionary+60) ||
               dictionary[10]>=64 || !name || name>44 ||
               dictionary[13] || dictionary[14] || dictionary[15])return CatalogResult::Corrupt;
            for(uint32_t j=0;j<i;++j)if(ids[j]==id)return CatalogResult::Corrupt;
            ids[i]=id;
            if (format == kUserSnapshot) {
                for (unsigned j = 64; j < sizeof(dictionary); ++j)
                    if (dictionary[j]) return CatalogResult::Corrupt;
            }
            Utf8 utf;
            for(uint32_t j=0;j<name;++j)if(!utf.Push(dictionary[16+j]))return CatalogResult::Corrupt;
            if(utf.remaining)return CatalogResult::Corrupt;
            for(uint32_t j=name;j<44;++j)if(dictionary[16+j])return CatalogResult::Corrupt;
            const uint8_t member = dictionary[10];
            if (base == kNoExtent) {
                if (member) return CatalogResult::Corrupt;
                base_members[i] = UINT16_MAX;
            } else {
                if (base >= header.extents) return CatalogResult::Corrupt;
                const uint16_t reference = (base << 6) | member;
                for (uint32_t j = 0; j < i; ++j)
                    if (base_members[j] == reference) return CatalogResult::Corrupt;
                base_members[i] = reference;
                uint8_t extent[24];
                if(!input.Read(extent_start+base*24,extent,24))return CatalogResult::IoError;
                if(Word(extent+12)!=uint32_t(ExtentKind::Base))return CatalogResult::Corrupt;
                bases.Add(base,1);
            }
        }
        // Every base extent needs a catalog member. A member pair is unique.
        for(uint32_t i=0;i<header.extents;++i) {
            uint8_t extent[24];
            if(!input.Read(extent_start+i*24,extent,24))return CatalogResult::IoError;
            if(Word(extent+12)==1 && !bases.Contains(i))return CatalogResult::Corrupt;
        }
        if(shape)*shape=header;
        return CatalogResult::Ok;
    }

    template <class CheckExtent>
    CatalogResult Validate(const CatalogRoot &root,CheckExtent check,CatalogShape *shape=nullptr) {
        if(!Bounds())return CatalogResult::Invalid;
        return ValidatePayload(device,capacity,root,check,shape,NativePoolEnd(device));
    }

    template <class ValidateRoot>
    CatalogResult OpenValidated(CatalogSelection &selection,ValidateRoot validate,
                                CatalogValidation mode = CatalogValidation::Independent,
                                bool newest_last = false) {
        selection={};
        write_blocked=true;
        trusted[0]={};trusted[1]={};
        CatalogRoot roots[2];CatalogResult states[2];
        bool valid[2]{};
        for(unsigned i=0;i<2;++i) {
            states[i]=Inspect(i,roots[i]);
            if(states[i]==CatalogResult::IoError || states[i]==CatalogResult::Invalid)return states[i];
        }
        const unsigned first = newest_last && roots[0].generation > roots[1].generation ? 1 : 0;
        for (unsigned pass = 0; pass < 2; ++pass) {
            const unsigned i = first ^ pass;
            if(states[i]==CatalogResult::Ok) {
                // Opt-in validators certify the extents described by the
                // payload. Matching mirrors describe the same flash bytes.
                if (mode != CatalogValidation::ReuseIdenticalPayload ||
                    !valid[1 - i] || !SamePayload(roots[1 - i], roots[i])) {
                    states[i]=validate(roots[i]);
                }
                if(states[i]==CatalogResult::IoError)return states[i];
                valid[i]=states[i]==CatalogResult::Ok;
            }
        }
        if(!valid[0] && !valid[1])return states[0]==CatalogResult::Corrupt || states[1]==CatalogResult::Corrupt?
            CatalogResult::Corrupt:CatalogResult::Empty;
        if(valid[0] && valid[1] && roots[0].generation==roots[1].generation)return CatalogResult::Corrupt;
        for(unsigned i=0;i<2;++i)if(valid[i])trusted[i]=roots[i];
        uint8_t slot=valid[1] && (!valid[0] || roots[1].generation>roots[0].generation)?1:0;
        selection={roots[slot],states[1-slot]==CatalogResult::Corrupt,slot};
        write_blocked=selection.quarantine;
        return selection.quarantine?CatalogResult::Quarantined:CatalogResult::Ok;
    }

    template <class CheckExtent>
    CatalogResult Open(CatalogSelection &selection,CheckExtent check) {
        return OpenValidated(selection,[&](const CatalogRoot &root){return Validate(root,check);});
    }

    // Source(offset,out,n) supplies an immutable serialized payload. Validate
    // source by streaming it through a separate read-only catalog view first.
    // Active selection is caller-provided; quarantine forbids publication.
    template <class Source,class CheckExtent>
    CatalogResult Publish(const CatalogSelection &current,uint32_t bytes,Source source,CheckExtent check,
                          CatalogSelection &published,const uint32_t *expected_crc=nullptr) {
        if(&current==&published)return CatalogResult::Invalid;
        published={};
        if(!Bounds() || bytes<32 || bytes%4 || bytes>capacity-32 || current.slot>1 || current.quarantine ||
           current.root.generation==UINT64_MAX)return CatalogResult::Invalid;
        const uint8_t slot=current.root.generation?uint8_t(1-current.slot):0;
        if(DestinationPinned(current))return CatalogResult::Busy;
        struct SourceDevice {
            Source &source;
            uint32_t bytes;
            bool Read(uint32_t at,uint8_t *out,uint32_t n) {
                return at<=bytes && n<=bytes-at && source(at,out,n);
            }
        } input{source,bytes};
        CatalogShape wanted;
        auto preflight_status=ValidatePayload(input,capacity,{1,0,bytes,0},check,&wanted,NativePoolEnd(device));
        if(preflight_status!=CatalogResult::Ok)return preflight_status;
        if(!current.root.generation) {
            for(unsigned i=0;i<2;++i)for(uint32_t at=0;at<capacity;at+=256) {
                if(!device.Read(slots[i]+at,buffer,256))return CatalogResult::IoError;
                for(auto byte:buffer)if(byte!=255)return CatalogResult::Corrupt;
            }
        } else {
            CatalogRoot active;
            auto status=Inspect(current.slot,active);
            if(status!=CatalogResult::Ok)return status;
            if(active.generation!=current.root.generation || active.crc!=current.root.crc ||
               active.address!=current.root.address || active.bytes!=current.root.bytes)return CatalogResult::Invalid;
            CatalogRoot alternate;
            status=Inspect(1-current.slot,alternate);
            if(status==CatalogResult::IoError || status==CatalogResult::Corrupt || status==CatalogResult::Invalid)return status;
            if(status==CatalogResult::Ok && alternate.generation>=active.generation)return CatalogResult::Invalid;
            CatalogShape existing;
            status=Validate(active,check,&existing);if(status!=CatalogResult::Ok)return status;
            if(wanted.next_id<existing.next_id)return CatalogResult::Invalid;
            // A removed identity must never be resurrected for another logical
            // dictionary while its old journal records can still exist.
            uint8_t id_bytes[4];
            for(uint32_t i=0;i<wanted.dictionaries;++i) {
                if(!source(32+i*96,id_bytes,4))return CatalogResult::IoError;
                const uint32_t id=Word(id_bytes);
                if(id>=existing.next_id)continue;
                bool present=false;
                for(uint32_t j=0;j<existing.dictionaries;++j) {
                    if(!device.Read(active.address+32+j*96,id_bytes,4))return CatalogResult::IoError;
                    if(Word(id_bytes)==id){present=true;break;}
                }
                if(!present)return CatalogResult::Invalid;
            }
            trusted[current.slot]=active;
        }
        if(!current.root.generation)Invalidate();
        trusted[slot]={}; // Erase/program failures must not leave a trusted old root.
        write_blocked=true;
        // A small root fits in the first erase sector. Inspect follows the
        // committed header length, so bytes left behind by a larger old root
        // are unreachable. Any future large publication erases the full slot.
        uint32_t erase_bytes = capacity;
        if (capacity >= kEraseSectorBytes && slots[slot] % kEraseSectorBytes == 0 &&
            bytes + kCatalogHeader <= kEraseSectorBytes) {
            erase_bytes = kEraseSectorBytes;
        }
        if (!device.EraseCatalog(slots[slot], erase_bytes)) return CatalogResult::IoError;
        uint32_t crc=0;
        for(uint32_t offset=0;offset<bytes;) {
            uint32_t n=bytes-offset;if(n>sizeof(buffer))n=sizeof(buffer);
            if(!source(offset,buffer,n))return CatalogResult::IoError;
            crc=Crc(crc,buffer,n);
            if(!Write(slots[slot]+32+offset,buffer,n))return CatalogResult::IoError;
            offset+=n;
        }
        // Exact mirrors must retain the source payload checksum. Reject a
        // changed source before writing a committed header for the new root.
        if(expected_crc && crc!=*expected_crc)return CatalogResult::Corrupt;
        const uint64_t generation=current.root.generation+1;
        uint8_t header[32];std::memcpy(header,"SQBCAT02",8);
        PutWord(header+8,uint32_t(generation));PutWord(header+12,uint32_t(generation>>32));
        PutWord(header+16,bytes);PutWord(header+20,crc);PutWord(header+24,Crc(0,header,24));PutWord(header+28,UINT32_MAX);
        if(!Write(slots[slot],header,32))return CatalogResult::IoError;
        CatalogRoot root{generation,slots[slot]+32,bytes,crc};
        uint32_t readback=0;
        for(uint32_t offset=0;offset<bytes;) {
            uint32_t n=bytes-offset;if(n>sizeof(buffer))n=sizeof(buffer);
            if(!device.Read(root.address+offset,buffer,n))return CatalogResult::IoError;
            readback=Crc(readback,buffer,n);offset+=n;
        }
        if(readback!=crc)return CatalogResult::Corrupt;
        auto status=Validate(root,check);if(status!=CatalogResult::Ok)return status;
        uint8_t copy[32];
        if(!device.Read(slots[slot],copy,32))return CatalogResult::IoError;
        if(std::memcmp(copy,header,32))return CatalogResult::Corrupt;
        PutWord(copy,kCommit);
        if(!Write(slots[slot]+28,copy,4))return CatalogResult::IoError;
        CatalogRoot verified;
        status=Inspect(slot,verified);if(status!=CatalogResult::Ok)return status;
        trusted[slot]=verified;
        write_blocked=false;
        published={verified,false,slot};return CatalogResult::Ok;
    }
};
} // namespace candidate_b
