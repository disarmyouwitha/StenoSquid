// Candidate-B development core. No Zephyr dependencies or automatic formatting.
// The catalog owns extents; caller serializes writes and pins reader generations.
#pragma once
#include <cstdint>
#include <cstring>

namespace candidate_b {
constexpr uint32_t kSegmentBytes = 65536, kPageBytes = 256;
constexpr uint32_t kCommit = 0x434f4d54;
enum class JournalResult { Ok, End, Sealed, Corrupt, IoError, Full, Invalid };
inline uint32_t Word(const uint8_t *p) {
    return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
inline void PutWord(uint8_t *p, uint32_t n) {
    for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(n >> (8*i));
}
inline uint32_t Crc(uint32_t crc, const uint8_t *p, uint32_t n) {
    crc = ~crc;
    for (uint32_t i = 0; i < n; ++i) {
        crc ^= p[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    return ~crc;
}
struct Utf8 {
    uint32_t value = 0, minimum = 0;
    uint8_t remaining = 0;
    bool Push(uint8_t byte) {
        if (!remaining) {
            if (byte < 128) return byte != 0;
            if (byte >= 0xc2 && byte <= 0xdf) { remaining=1; value=byte&31; minimum=128; }
            else if (byte >= 0xe0 && byte <= 0xef) { remaining=2; value=byte&15; minimum=2048; }
            else if (byte >= 0xf0 && byte <= 0xf4) { remaining=3; value=byte&7; minimum=65536; }
            else return false;
        } else {
            if ((byte & 0xc0) != 0x80) return false;
            value = (value << 6) | (byte & 63);
            if (!--remaining && (value < minimum || value > 0x10ffff ||
                                (value >= 0xd800 && value <= 0xdfff))) return false;
        }
        return true;
    }
};

struct JournalRecord {
    uint64_t sequence = 0;
    uint32_t address = 0, span = 0, payload_bytes = 0;
};
struct JournalScan {
    uint64_t previous = 0;
    uint32_t next = 0, end = 0;
    bool ready = false;
};

// Device supplies Read(address,out,size), Program(address,data,size), and
// Erase(segment). Only explicit Initialize erases. All buffers are fixed size.
template <class Device>
class NativeJournal {
    Device &device;
    uint8_t buffer[256]{};

    bool Write(uint32_t address, const uint8_t *data, uint32_t size) {
        while (size) {
            uint32_t n = kPageBytes - address % kPageBytes;
            if (n > size) n = size;
            if (!device.Program(address, data, n)) return false;
            address += n; data += n; size -= n;
        }
        return true;
    }

public:
    explicit NativeJournal(Device &value) : device(value) {}

    JournalResult Initialize(uint32_t segment, uint64_t epoch) {
        if (segment < 2 || segment >= 255 || !epoch) return JournalResult::Invalid;
        if (!device.Erase(segment)) return JournalResult::IoError;
        uint8_t header[32]{};
        std::memcpy(header, "SQJSEG01", 8);
        PutWord(header+8, uint32_t(epoch)); PutWord(header+12, uint32_t(epoch>>32));
        PutWord(header+16, Crc(0, header, 16));
        if (!Write(segment*kSegmentBytes, header, 32)) return JournalResult::IoError;
        return Open(segment, epoch);
    }

    JournalResult Open(uint32_t segment, uint64_t epoch) {
        if (segment < 2 || segment >= 255 || !epoch) return JournalResult::Invalid;
        uint8_t header[32];
        if (!device.Read(segment*kSegmentBytes, header, 32)) return JournalResult::IoError;
        if (std::memcmp(header, "SQJSEG01", 8) || Word(header+8) != uint32_t(epoch) ||
            Word(header+12) != uint32_t(epoch>>32) || Word(header+16) != Crc(0, header, 16) ||
            Word(header+20) || Word(header+24) || Word(header+28)) return JournalResult::Corrupt;
        return JournalResult::Ok;
    }

    JournalResult BeginScan(uint32_t segment, uint64_t epoch, uint64_t previous,
                            JournalScan &scan) {
        scan = {};
        const auto status = Open(segment, epoch);
        if (status != JournalResult::Ok) return status;
        scan = {previous, segment*kSegmentBytes+kPageBytes, (segment+1)*kSegmentBytes, true};
        return JournalResult::Ok;
    }

    // One committed record per call, at most 2119 payload bytes streamed through
    // fixed workspace. Caller can yield/cancel between steps. IO errors leave
    // cursor unchanged; committed corruption never looks like an unfinished tail.
    JournalResult Step(JournalScan &scan, JournalRecord &record) {
        if (!scan.ready || scan.next > scan.end || scan.end % kSegmentBytes ||
            scan.end > 255*kSegmentBytes || scan.end < 3*kSegmentBytes ||
            scan.next < scan.end-kSegmentBytes+kPageBytes) return JournalResult::Invalid;
        if (scan.end-scan.next < 32) return JournalResult::End;
        const auto status=Read(scan.next,record);
        if (status!=JournalResult::Ok) return status;
        if (record.sequence<=scan.previous) return JournalResult::Corrupt;
        scan.previous=record.sequence;
        scan.next+=record.span;
        return JournalResult::Ok;
    }

    JournalResult Read(uint32_t address, JournalRecord &record) {
        const uint32_t offset = address % kSegmentBytes;
        if (address / kSegmentBytes < 2 || address / kSegmentBytes >= 255 || address % 4 ||
            offset < kPageBytes || offset > kSegmentBytes-32) return JournalResult::Invalid;
        uint8_t header[32];
        if (!device.Read(address, header, 32)) return JournalResult::IoError;
        bool blank = true;
        for (auto byte : header) blank &= byte == 255;
        if (blank) return JournalResult::End;
        if (Word(header+28) != kCommit) return JournalResult::Sealed;
        uint32_t span=Word(header+4), size=Word(header+16);
        uint64_t sequence=uint64_t(Word(header+8)) | uint64_t(Word(header+12))<<32;
        if (std::memcmp(header,"SQJR",4) || !sequence || span%4 || span<44 ||
            span>kSegmentBytes-offset || size<12 || size>2119 ||
            span != ((size+35)&~3u) || Word(header+24)!=Crc(0,header,24)) return JournalResult::Corrupt;
        uint32_t crc=Crc(0, header+8, 8), position=0;
        uint8_t count=0, op=0;
        uint32_t text_start=0;
        Utf8 utf;
        while (position < size) {
            uint32_t n=size-position;
            if (n>sizeof(buffer)) n=sizeof(buffer);
            if (!device.Read(address+32+position,buffer,n)) return JournalResult::IoError;
            crc=Crc(crc,buffer,n);
            if (!position) {
                op=buffer[4]; count=buffer[5];
                uint16_t text=uint16_t(buffer[6]) | uint16_t(buffer[7])<<8;
                text_start=8u+4u*count;
                if (!Word(buffer) || op>2 || count<1 || count>16 || text>2047 ||
                    (op && text) || size!=text_start+text) return JournalResult::Corrupt;
                for (uint8_t i=0;i<count;++i) {
                    const uint32_t mask=Word(buffer+8+4*i);
                    if (!mask || mask>>25) return JournalResult::Corrupt;
                }
            }
            for (uint32_t i=0;i<n;++i)
                if (position+i>=text_start && !utf.Push(buffer[i])) return JournalResult::Corrupt;
            position+=n;
        }
        if (utf.remaining || crc!=Word(header+20)) return JournalResult::Corrupt;
        record={sequence,address,span,size};
        return JournalResult::Ok;
    }

    // Caller preflights capacity/tail and supplies an exclusive, verified blank
    // append location. Program the commit only after complete payload readback.
    // After ANY write failure, discard writer state and rescan before retry.
    JournalResult Append(uint32_t address, uint64_t sequence, uint32_t identity,
                         uint8_t operation, const uint32_t *strokes, uint8_t count,
                         const uint8_t *text, uint16_t length) {
        const uint32_t offset=address%kSegmentBytes, size=8u+4u*count+length;
        const uint32_t span=(32u+size+3)&~3u;
        if (address/kSegmentBytes<2 || address/kSegmentBytes>=255 || address%4 ||
            offset<256 || !sequence || !identity || operation>2 || count<1 || count>16 ||
            !strokes || length>2047 || (length && !text) || (operation && length)) return JournalResult::Invalid;
        if (span>kSegmentBytes-offset) return JournalResult::Full;
        for (uint8_t i=0;i<count;++i) if (!strokes[i] || strokes[i]>>25) return JournalResult::Invalid;
        Utf8 utf;
        for (uint16_t i=0;i<length;++i) if (!utf.Push(text[i])) return JournalResult::Invalid;
        if (utf.remaining) return JournalResult::Invalid;
        for (uint32_t at=0;at<span;) {
            uint32_t n=span-at; if (n>sizeof(buffer)) n=sizeof(buffer);
            if (!device.Read(address+at,buffer,n)) return JournalResult::IoError;
            for (uint32_t i=0;i<n;++i) if (buffer[i]!=255) return JournalResult::Sealed;
            at+=n;
        }
        uint8_t prefix[72]{};
        PutWord(prefix,identity); prefix[4]=operation; prefix[5]=count;
        prefix[6]=uint8_t(length); prefix[7]=uint8_t(length>>8);
        for (uint8_t i=0;i<count;++i) PutWord(prefix+8+4*i,strokes[i]);
        uint8_t header[32];
        std::memcpy(header,"SQJR",4); PutWord(header+4,span);
        PutWord(header+8,uint32_t(sequence)); PutWord(header+12,uint32_t(sequence>>32));
        PutWord(header+16,size);
        uint32_t crc=Crc(0,header+8,8);
        crc=Crc(crc,prefix,8+4*count); crc=Crc(crc,text,length);
        PutWord(header+20,crc); PutWord(header+24,Crc(0,header,24)); PutWord(header+28,UINT32_MAX);
        if (!Write(address,header,32) || !Write(address+32,prefix,8+4*count) ||
            !Write(address+40+4*count,text,length)) return JournalResult::IoError;
        // Verify exact header, then stream CRC over flash before committing.
        if (!device.Read(address,buffer,32) || std::memcmp(buffer,header,32)) return JournalResult::IoError;
        uint32_t check=Crc(0,header+8,8);
        for (uint32_t at=0;at<size;) {
            uint32_t n=size-at; if (n>sizeof(buffer)) n=sizeof(buffer);
            if (!device.Read(address+32+at,buffer,n)) return JournalResult::IoError;
            check=Crc(check,buffer,n); at+=n;
        }
        if (check!=crc) return JournalResult::IoError;
        PutWord(header,kCommit);
        if (!Write(address+28,header,4)) return JournalResult::IoError;
        JournalRecord record;
        return Read(address,record);
    }
};
} // namespace candidate_b
