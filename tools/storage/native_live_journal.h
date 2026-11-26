// Candidate-B live overlay state. Caller serializes every method with catalog
// publication and the engine. Does not allocate, erase, format or clean journals.
#pragma once
#include "native_catalog.h"
#include "packed_index.h"
#include "binary_record.h"

namespace candidate_b {
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
void ReplayJournalFault(unsigned line, uint32_t address, unsigned status);
#endif
enum class LiveResult { More, Ready, Ok, Missing, Invalid, IoError, Corrupt,
                        NeedCheckpoint, NeedJournal, NeedRebuild, ReadOnly, Stale, Busy };
struct LiveView {
    CatalogRoot root{};
    // RAM-only key order; equal outlines retain sequence order (newest last).
    // Committed flash journals remain chronological and keep the same format.
    Tail tail{};
    SegmentMap journals{};
    uint32_t identities[64]{};
    uint16_t outline_lengths[64]{};
    uint32_t dictionary_count=0,forward_address=0,forward_count=0,token=0,highwater_address=0;
    uint64_t through=0,highwater=0;
};

// One lookup is offered to successive dictionaries in priority order. Cache
// record addresses by catalog slot for that outline and pinned journal version.
struct OutlineEdits {
    CatalogRoot root{};
    uint64_t highwater=0;
    uint32_t strokes[16]{}, addresses[64]{};
    uint8_t count=0;
    enum class State : uint8_t { Unloaded, Loaded, Dense };
    State tail=State::Unloaded, forward=State::Unloaded;

    // Javelin revisits recent outlines while resegmenting a passage. Retain a
    // few exact misses across those queries, under the same pinned version.
    static constexpr unsigned kMissCount=8;
    uint32_t misses[kMissCount][16]{};
    uint8_t miss_lengths[kMissCount]{}, next_miss=0;
};

template <class Device>
class LiveJournal {
    Device &device;
    NativeCatalog<Device> &catalog;
    NativeJournal<Device> journal;
    LiveView live;
    CatalogShape shape{};
    uint32_t dictionary_position=0,extent_position=0;
    uint32_t append_address=0,append_end=0;
    uint32_t maintenance_token=0;
    uint64_t last_epoch=0;
    JournalScan scan{};
    bool recovering=false,scanning=false,ready=false,sealed=false,quarantine=false,boundary_seen=false,maintenance=false;

    LiveResult Fail(LiveResult result){recovering=ready=false;return result;}
    bool HasIdentity(const LiveView &view,uint32_t id)const {
        for(uint32_t i=0;i<view.dictionary_count;++i)if(view.identities[i]==id)return true;
        return false;
    }
    static bool HasLength(const LiveView &view,uint32_t id,uint8_t count) {
        for(uint32_t i=0;i<view.dictionary_count;++i)
            if(view.identities[i]==id)return view.outline_lengths[i]&(uint16_t(1)<<(count-1));
        return false;
    }
    void Observe(uint32_t id,uint8_t count) {
        for(uint32_t i=0;i<live.dictionary_count;++i)if(live.identities[i]==id) {
            live.outline_lengths[i]|=uint16_t(1)<<(count-1);return;
        }
    }
    bool Owns(const LiveView &view,uint32_t at)const {
        return at/kSegmentBytes<kLargePoolEnd && at%kSegmentBytes>=256 && !(at%4) && view.journals.Contains(at/kSegmentBytes);
    }
    LiveResult AppendTail(uint32_t address,uint32_t id,const uint32_t *strokes,uint8_t count) {
        if(live.tail.count==Tail::kCapacity)return LiveResult::NeedRebuild;
        uint32_t low=0,high=live.tail.count;
        while(low<high) {
            const uint32_t mid=low+(high-low)/2;JournalRecord record;JournalKey key;
            if(!Owns(live,live.tail.addresses[mid]))return LiveResult::Corrupt;
            const auto status=journal.ReadKey(live.tail.addresses[mid],record,key);
            if(status==JournalResult::IoError)return LiveResult::IoError;
            if(status!=JournalResult::Ok || record.sequence>live.highwater || record.sequence<=live.through)return LiveResult::Corrupt;
            const int order = CompareOutlineKey(key, id, strokes, count);
            if(order<=0)low=mid+1;else high=mid;
        }
        for(uint32_t i=live.tail.count;i>low;--i)live.tail.addresses[i]=live.tail.addresses[i-1];
        live.tail.addresses[low]=address;++live.tail.count;return LiveResult::Ok;
    }
    // Address flag above the supported pool offsets.
    static constexpr uint32_t kIndexedAddress = 1u << 31;
    LiveResult ReadLookupKey(const LiveView &view, uint32_t address, bool indexed,
                             JournalKey &key) {
        if (!Owns(view, address)) return LiveResult::Corrupt;
        JournalRecord record;
        const auto status = journal.ReadKey(address, record, key);
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
        if (status != JournalResult::Ok || record.sequence > view.highwater ||
                (record.sequence <= view.through) != indexed)
            ReplayJournalFault(__LINE__, address, unsigned(status));
#endif
        if (status == JournalResult::IoError) return LiveResult::IoError;
        if (status != JournalResult::Ok || record.sequence > view.highwater ||
            (record.sequence <= view.through) != indexed) return LiveResult::Corrupt;
        return LiveResult::Ok;
    }
    LiveResult VerifyLookup(const LiveView &view, uint32_t address, bool indexed, uint32_t id,
                            const uint32_t *strokes, uint8_t count, RecordView &result) {
        JournalRecord record;
        JournalKey key;
        const auto status = journal.Read(address, record, &key);
#ifdef CONFIG_ZMK_STENO_SQUID_REPLAY_DIAGNOSTICS
        if (status != JournalResult::Ok || record.sequence > view.highwater ||
                (record.sequence <= view.through) != indexed ||
                CompareOutlineKey(key, id, strokes, count))
            ReplayJournalFault(__LINE__, address, unsigned(status));
#endif
        if (status == JournalResult::IoError) return LiveResult::IoError;
        if (status != JournalResult::Ok || record.sequence > view.highwater ||
            (record.sequence <= view.through) != indexed ||
            CompareOutlineKey(key, id, strokes, count)) return LiveResult::Corrupt;
        result = {static_cast<Operation>(key.operation), address + 40u + 4u * key.count, key.text_bytes};
        return LiveResult::Ok;
    }

    LiveResult LoadOutline(const LiveView &view, OutlineEdits &edits, bool indexed) {
        const uint32_t count = indexed ? view.forward_count : view.tail.count;
        auto read = [&](uint32_t position, uint32_t &address, JournalKey &key) {
            if (indexed) {
                uint8_t raw[4];
                if (!device.Read(view.forward_address + position * 4, raw, sizeof(raw))) return LiveResult::IoError;
                address = Word(raw);
            } else {
                address = view.tail.addresses[position];
            }
            return ReadLookupKey(view, address, indexed, key);
        };
        uint32_t low = 0, high = count, first_address = 0;
        JournalKey first_key;
        while (low < high) {
            const uint32_t mid = low + (high - low) / 2;
            uint32_t address;
            JournalKey key;
            const auto status = read(mid, address, key);
            if (status != LiveResult::Ok) return status;
            const int order = CompareOutline(key, edits.strokes, edits.count);
            if (order == 0) {
                first_address = address;
                first_key = key;
            }
            if (order < 0) low = mid + 1;
            else high = mid;
        }
        if (!first_address) return LiveResult::Ok;
        // Walk the group in identity order. The tail can contain multiple
        // versions of one key, so jump to its last version on a duplicate.
        for (unsigned matched = 0; low < count; ++matched) {
            if (matched == 64) return LiveResult::Busy;
            const uint32_t identity = first_key.identity;
            uint32_t address = first_address;
            bool have_next = false;
            if (++low < count) {
                const auto status = read(low, first_address, first_key);
                if (status != LiveResult::Ok) return status;
                have_next = CompareOutline(first_key, edits.strokes, edits.count) == 0;
            }
            if (!indexed && have_next && first_key.identity == identity) {
                address = first_address;
                uint32_t end = count;
                for (++low; low < end;) {
                    const uint32_t mid = low + (end - low) / 2;
                    uint32_t probe;
                    JournalKey key;
                    const auto status = read(mid, probe, key);
                    if (status != LiveResult::Ok) return status;
                    if (CompareOutlineKey(key, identity, edits.strokes, edits.count) <= 0) {
                        address = probe;
                        low = mid + 1;
                    } else {
                        end = mid;
                    }
                }
                have_next = false;
                if (low < count) {
                    const auto status = read(low, first_address, first_key);
                    if (status != LiveResult::Ok) return status;
                    have_next = CompareOutline(first_key, edits.strokes, edits.count) == 0;
                }
            }
            for (uint32_t slot = 0; slot < view.dictionary_count; ++slot) {
                if (view.identities[slot] != identity) continue;
                // Any tail operation shadows the checkpoint, including Reset.
                if (!indexed || !edits.addresses[slot]) edits.addresses[slot] = indexed ? address | kIndexedAddress : address;
                break;
            }
            if (!have_next) break;
        }
        return LiveResult::Ok;
    }
    LiveResult FindIn(const LiveView &view,uint32_t id,const uint32_t *strokes,uint8_t count,
                      RecordView &result, bool skip_tail=false) {
        if(!id || !strokes || !count || count>16 || view.tail.count>256 || view.dictionary_count>64 ||
           view.forward_count>kMaxEntries || !HasIdentity(view,id))return LiveResult::Invalid;
        for(uint8_t i=0;i<count;++i)if(!ValidNativeStroke(strokes[i]))return LiveResult::Invalid;
        if(!HasLength(view,id,count))return LiveResult::Missing;
        bool io=false,corrupt=false,indexed=false;
        auto compare=[&](uint32_t address) {
            JournalKey key;
            const auto status = ReadLookupKey(view, address, indexed, key);
            if (status != LiveResult::Ok) {
                io = status == LiveResult::IoError;
                corrupt = !io;
                return 2;
            }
            return CompareOutlineKey(key, id, strokes, count);
        };
        auto read=[&](uint32_t offset,uint8_t *out,size_t n) {
            indexed=true;
            if(offset>view.forward_count*4 || n>view.forward_count*4-offset){corrupt=true;return false;}
            if(!device.Read(view.forward_address+offset,out,n)){io=true;return false;}return true;
        };
        uint32_t address=0;
        uint32_t low=0,high=skip_tail?0:view.tail.count;
        while(low<high) {
            const uint32_t mid=low+(high-low)/2;
            const int order=compare(view.tail.addresses[mid]);
            if(order<-1 || order>1)break;
            if(order<=0)low=mid+1;else high=mid;
        }
        Result found=Result::Missing;
        if(!io && !corrupt && low && compare(view.tail.addresses[low-1])==0) {
            address=view.tail.addresses[low-1];found=Result::Found;
        }
        if(!io && !corrupt && found==Result::Missing)found=FindOrdered(view.forward_count,read,compare,address);
        if(found==Result::Found && !io && !corrupt)
            return VerifyLookup(view,address,indexed,id,strokes,count,result);
        if(io)return LiveResult::IoError;
        if(corrupt || found==Result::Corrupt || found==Result::IoError)return LiveResult::Corrupt;
        return LiveResult::Missing;
    }
public:
    LiveJournal(Device &value,NativeCatalog<Device> &roots):device(value),catalog(roots),journal(value){}
    bool Ready()const{return ready;}
    uint64_t Highwater()const{return live.highwater;}
    // The caller holds the exclusive store lock for the whole walk.
    const LiveView &CurrentView() const { return live; }
    uint32_t TailCount()const{return live.tail.count;}
    const CatalogRoot &Root()const{return live.root;}
    uint8_t MaximumOutline(uint32_t id)const {
        for(uint32_t i=0;i<live.dictionary_count;++i)if(live.identities[i]==id) {
            const auto bits=live.outline_lengths[i];
            return bits?uint8_t(32-__builtin_clz(unsigned(bits))):0;
        }
        return 0;
    }

    // Attach only a fully validated selection from this catalog instance.
    // Opening/recovery is read-only and advances one metadata row or record/call.
    LiveResult Begin(const CatalogSelection &selection) {
        ready=recovering=scanning=false;live={};scan={};
        dictionary_position=extent_position=append_address=append_end=0;last_epoch=0;sealed=false;
        if(!catalog.IsTrusted(selection.root))return LiveResult::Invalid;
        uint8_t prefix[32];
        if(!device.Read(selection.root.address,prefix,32))return LiveResult::IoError;
        shape.next_id=Word(prefix);shape.dictionaries=uint16_t(prefix[4]) | uint16_t(prefix[5])<<8;
        shape.extents=uint16_t(prefix[6]) | uint16_t(prefix[7])<<8;
        shape.through=uint64_t(Word(prefix+8)) | uint64_t(Word(prefix+12))<<32;
        if(!shape.next_id || shape.dictionaries>64 || shape.extents>253 ||
           selection.root.bytes!=32u+shape.dictionaries*96u+shape.extents*24u)return LiveResult::Corrupt;
        live.root=selection.root;live.through=shape.through;quarantine=selection.quarantine;
        boundary_seen=shape.through==0;
        recovering=true;return LiveResult::More;
    }

    LiveResult RecoverStep() {
        if(ready)return LiveResult::Ready;
        if(!recovering)return LiveResult::Invalid;
        if(!catalog.IsLatestTrusted(live.root))return Fail(LiveResult::Stale);
        if(dictionary_position<shape.dictionaries) {
            uint8_t row[4];
            if(!device.Read(live.root.address+32+dictionary_position*96,row,4))return Fail(LiveResult::IoError);
            uint32_t id=Word(row);
            if(!id || id>=shape.next_id || HasIdentity(live,id))return Fail(LiveResult::Corrupt);
            live.identities[live.dictionary_count++]=id;++dictionary_position;return LiveResult::More;
        }
        if(scanning) {
            JournalRecord record;
            JournalKey key;
            const auto status = journal.Step(scan, record, &key);
            if(status==JournalResult::IoError)return Fail(LiveResult::IoError);
            if(status==JournalResult::Ok) {
                Observe(key.identity, key.count);
                live.highwater=record.sequence;live.highwater_address=record.address;
                if(record.sequence==live.through)boundary_seen=true;
                if(record.sequence>live.through) {
                    const auto added = AppendTail(record.address, key.identity, key.strokes, key.count);
                    if(added!=LiveResult::Ok)return Fail(added);
                }
                return LiveResult::More;
            }
            if(status!=JournalResult::End && status!=JournalResult::Sealed)return Fail(LiveResult::Corrupt);
            sealed=status==JournalResult::Sealed;append_address=scan.next;append_end=scan.end;
            scanning=false;return LiveResult::More;
        }
        if(extent_position<shape.extents) {
            uint8_t extent[24];
            uint32_t at=live.root.address+32+shape.dictionaries*96+extent_position++*24;
            if(!device.Read(at,extent,24))return Fail(LiveResult::IoError);
            uint32_t start=uint16_t(extent[0]) | uint16_t(extent[1])<<8,kind=Word(extent+12);
            if(kind==uint32_t(ExtentKind::Forward)) {
                uint32_t bytes=Word(extent+4);
                if(live.forward_count || bytes%4 || bytes/4>kMaxEntries)return Fail(LiveResult::Corrupt);
                live.forward_address=start*kSegmentBytes;live.forward_count=bytes/4;
            }
            if(kind!=uint32_t(ExtentKind::Journal))return LiveResult::More;
            const uint64_t epoch=uint64_t(Word(extent+16)) | uint64_t(Word(extent+20))<<32;
            if(!epoch || epoch<=last_epoch || start<2 || start>=NativePoolEnd(device) || live.journals.Contains(start))return Fail(LiveResult::Corrupt);
            const auto status=journal.BeginScan(start,epoch,live.highwater,scan);
            if(status!=JournalResult::Ok)return Fail(status==JournalResult::IoError?LiveResult::IoError:LiveResult::Corrupt);
            live.journals.Add(start,1);last_epoch=epoch;scanning=true;return LiveResult::More;
        }
        if(!boundary_seen || live.through>live.highwater || (live.forward_count && !live.through))return Fail(LiveResult::Corrupt);
        recovering=false;ready=true;return LiveResult::Ready;
    }

    // Caller has published a metadata-only edit under the same exclusive lease.
    // Compare immutable ownership and identity order before retaining live cursors.
    bool Retarget(const CatalogSelection &next) {
        if(!ready || maintenance || next.quarantine || !catalog.CanWrite(next.root))return false;
        uint8_t old_prefix[32],new_prefix[32];
        if(!device.Read(live.root.address,old_prefix,32) || !device.Read(next.root.address,new_prefix,32) ||
           std::memcmp(old_prefix,new_prefix,32) || next.root.bytes!=live.root.bytes)return false;
        for(uint32_t i=0;i<live.dictionary_count;++i) {
            uint8_t id[4];
            if(!device.Read(next.root.address+32+i*96,id,4) || Word(id)!=live.identities[i])return false;
        }
        for(uint32_t i=0;i<shape.extents;++i) {
            uint8_t a[24],b[24];const uint32_t offset=32+shape.dictionaries*96+i*24;
            if(!device.Read(live.root.address+offset,a,24) || !device.Read(next.root.address+offset,b,24) ||
               std::memcmp(a,b,24))return false;
        }
        live.root=next.root;return true;
    }

    LiveResult Append(uint32_t id,uint8_t operation,const uint32_t *strokes,uint8_t count,
                       const uint8_t *text,uint16_t bytes) {
        if(!ready)return LiveResult::Invalid;
        if(maintenance)return LiveResult::Busy;
        if(quarantine || !catalog.CanWrite(live.root))return catalog.IsLatestTrusted(live.root)?LiveResult::ReadOnly:LiveResult::Stale;
        if(!HasIdentity(live,id) || !ValidEdit(id,operation,strokes,count,text,bytes))return LiveResult::Invalid;
        if(live.tail.count==256)return LiveResult::NeedCheckpoint;
        const uint32_t span=(32u+8u+4u*count+bytes+3)&~3u;
        if(sealed || !append_address || append_address>append_end ||
           span>append_end-append_address)return LiveResult::NeedJournal;
        if(live.highwater==UINT64_MAX)return LiveResult::Corrupt;
        const auto status=journal.Append(append_address,live.highwater+1,id,operation,strokes,count,text,bytes);
        if(status==JournalResult::Invalid)return LiveResult::Invalid;
        if(status==JournalResult::Full)return LiveResult::NeedJournal;
        // A write/readback failure has an ambiguous commit outcome. Stop writes
        // and queries until recovery reconstructs it; never retry at this cursor.
        if(status!=JournalResult::Ok)return Fail(status==JournalResult::IoError?LiveResult::IoError:LiveResult::Corrupt);
        const auto added=AppendTail(append_address,id,strokes,count);
        if(added!=LiveResult::Ok)return Fail(added==LiveResult::NeedRebuild?LiveResult::Corrupt:added);
        Observe(id,count);
        ++live.highwater;live.highwater_address=append_address;
        append_address+=span;
        return LiveResult::Ok;
    }

    LiveResult Snapshot(LiveView &view) {
        if(view.token || !ready)return LiveResult::Invalid;
        uint32_t token=0;
        if(!catalog.AcquirePin(live.root,token))return LiveResult::Busy;
        view=live;view.token=token;return LiveResult::Ok;
    }
    LiveResult BeginMaintenance(LiveView &view) {
        if(maintenance)return LiveResult::Busy;
        if(!ready || !catalog.CanWrite(live.root))return LiveResult::ReadOnly;
        const auto result=Snapshot(view);
        if(result==LiveResult::Ok){maintenance=true;maintenance_token=view.token;}
        return result;
    }
    bool EndMaintenance(LiveView &view) {
        if(!maintenance || !view.token || view.token!=maintenance_token)return false;
        if(!catalog.ReleasePin(view.token))return false;
        view={};maintenance=false;maintenance_token=0;return true;
    }
    bool Release(LiveView &view) {
        if(!view.token || view.token==maintenance_token)return false;
        const bool released=catalog.ReleasePin(view.token);view={};return released;
    }
    LiveResult FindLive(uint32_t id,const uint32_t *strokes,uint8_t count,RecordView &result) {
        if(!ready)return LiveResult::Invalid;
        if(!catalog.IsLatestTrusted(live.root))return LiveResult::Stale;
        const auto status=FindIn(live,id,strokes,count,result);
        return status==LiveResult::IoError || status==LiveResult::Corrupt?Fail(status):status;
    }
    LiveResult FindSnapshot(const LiveView &view,uint32_t id,const uint32_t *strokes,uint8_t count,RecordView &result) {
        if(!catalog.HasPin(view.token,view.root))return LiveResult::Invalid;
        return FindIn(view,id,strokes,count,result);
    }
    LiveResult FindOutline(const LiveView &view, OutlineEdits &edits, uint32_t id,
                           const uint32_t *strokes, uint8_t count, RecordView &result) {
        if (!catalog.HasPin(view.token, view.root) || !strokes || !count || count > 16 ||
            view.dictionary_count > 64 || view.tail.count > Tail::kCapacity ||
            view.forward_count > kMaxEntries) return LiveResult::Invalid;
        uint32_t slot = 0;
        while (slot < view.dictionary_count && view.identities[slot] != id) ++slot;
        if (slot == view.dictionary_count) return LiveResult::Invalid;
        if (!(view.outline_lengths[slot] & (uint16_t(1) << (count - 1)))) return LiveResult::Missing;
        for (unsigned i = 0; i < count; ++i)
            if (!ValidNativeStroke(strokes[i])) return LiveResult::Invalid;

        const auto &root = view.root;
        const auto bytes = count * sizeof(*strokes);
        if (edits.root.generation != root.generation || edits.root.address != root.address ||
            edits.root.crc != root.crc || edits.root.bytes != root.bytes ||
            edits.highwater != view.highwater) {
            edits.root = root;
            edits.highwater = view.highwater;
            edits.count = 0;
            edits.next_miss = 0;
            std::memset(edits.miss_lengths, 0, sizeof(edits.miss_lengths));
        }
        if (edits.count != count || std::memcmp(edits.strokes, strokes, bytes)) {
            for (unsigned i = 0; i < OutlineEdits::kMissCount; ++i)
                if (edits.miss_lengths[i] == count && !std::memcmp(edits.misses[i], strokes, bytes))
                    return LiveResult::Missing;
            edits.count = count;
            std::memcpy(edits.strokes, strokes, bytes);
            std::memset(edits.addresses, 0, sizeof(edits.addresses));
            edits.tail = edits.forward = OutlineEdits::State::Unloaded;
        }
        using State = OutlineEdits::State;
        auto load = [&](State &state, bool indexed) {
            if (state != State::Unloaded) return LiveResult::Ok;
            const auto status = LoadOutline(view, edits, indexed);
            if (status != LiveResult::Ok && status != LiveResult::Busy) {
                edits.count = 0;
                return status;
            }
            state = status == LiveResult::Busy ? State::Dense : State::Loaded;
            return LiveResult::Ok;
        };
        const auto tail = load(edits.tail, false);
        if (tail != LiveResult::Ok) return tail;
        if (edits.tail == State::Dense) return FindIn(view, id, strokes, count, result);
        const uint32_t recent = edits.addresses[slot];
        if (recent && !(recent & kIndexedAddress))
            return VerifyLookup(view, recent, false, id, strokes, count, result);

        const bool first_forward = edits.forward == State::Unloaded;
        const auto forward = load(edits.forward, true);
        if (forward != LiveResult::Ok) return forward;
        if (edits.forward == State::Dense) return FindIn(view, id, strokes, count, result, true);
        if (first_forward) {
            bool any_edit = false;
            for (uint32_t address : edits.addresses) any_edit |= address != 0;
            if (!any_edit) {
                const unsigned miss = edits.next_miss;
                std::memcpy(edits.misses[miss], strokes, bytes);
                edits.miss_lengths[miss] = count;
                edits.next_miss = (miss + 1) % OutlineEdits::kMissCount;
            }
        }
        const uint32_t address = edits.addresses[slot] & ~kIndexedAddress;
        return address ? VerifyLookup(view, address, true, id, strokes, count, result) : LiveResult::Missing;
    }
    LiveResult ReadText(const LiveView &view,const RecordView &text,uint8_t *out,uint16_t bytes) {
        if(!catalog.HasPin(view.token,view.root) || bytes!=text.text_bytes || text.operation!=Operation::Set ||
           bytes>2047 || (bytes && !out))return LiveResult::Invalid;
        if(!bytes)return LiveResult::Ok;
        if(!Owns(view,text.text_address) || bytes>kSegmentBytes-text.text_address%kSegmentBytes)return LiveResult::Corrupt;
        return device.Read(text.text_address,out,bytes)?LiveResult::Ok:LiveResult::IoError;
    }
};
} // namespace candidate_b
