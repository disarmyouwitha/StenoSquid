#include "native_catalog.h"
#include "native_index_sink.h"
#include <cassert>
#include <vector>
#include <iostream>
#include "native_test_nor.h"
using namespace candidate_b;
using namespace native_test;
std::vector<uint8_t> payload(unsigned count=1) {
    std::vector<uint8_t> data(32+96*count,0);
    PutWord(data.data(),count+1);data[4]=uint8_t(count);
    for(unsigned i=0;i<count;++i) {
        auto *row=data.data()+32+i*96;
        PutWord(row,i+1);PutWord(row+4,1);
        row[8]=row[9]=255;row[12]=4;
        std::memcpy(row+16,"Base",4);
    }
    return data;
}
auto accept=[](uint32_t,const uint8_t *){return true;};
void faults(unsigned count) {
    Nor original; NativeCatalog<Nor> catalog(original);
    CatalogSelection empty,old;
    auto first=payload();
    assert(catalog.Publish(empty,first.size(),Source{first},accept,old)==CatalogResult::Ok);
    auto next=payload(count); next[32+16]='N';
    Nor dry=original;dry.operations=0;
    NativeCatalog<Nor> dryCatalog(dry);CatalogSelection final;
    assert(dryCatalog.Publish(old,next.size(),Source{next},accept,final)==CatalogResult::Ok);
    unsigned cases=0;
    for(uint32_t at=1;at<=dry.operations;++at)for(uint32_t tear:{0u,1u,127u,4095u}) {
        Nor trial=original;trial.operations=0;trial.cut=at;trial.tear=tear;
        NativeCatalog<Nor> operation(trial);CatalogSelection result;
        try { operation.Publish(old,next.size(),Source{next},accept,result);assert(false); } catch(const Cut &) {}
        trial.cut=0;
        const auto bytes=trial.data;
        auto status=operation.Open(result,accept);
        assert(status==CatalogResult::Ok);
        assert(result.root.generation==1 || result.root.generation==2);
        const auto &expected=result.root.generation==1?first:next;
        assert(result.root.bytes==expected.size());
        assert(std::memcmp(trial.data.data()+result.root.address,expected.data(),expected.size())==0);
        assert(trial.data==bytes);
        CatalogSelection retry;
        assert(operation.Publish(result,next.size(),Source{next},accept,retry)==CatalogResult::Ok);
        assert(operation.Open(retry,accept)==CatalogResult::Ok);
        ++cases;
    }
    std::cout<<"Native catalog "<<count<<" dictionaries: "<<cases<<" torn publication cases passed\n";
}
void ShrinkThenGrow() {
    Nor nor;
    NativeCatalog<Nor> catalog(nor);
    CatalogSelection empty, large, small, smaller, bridge, grown;
    const auto initial = payload(64);
    auto compact = payload(1);
    PutWord(compact.data(), 65);
    auto replacement = payload(64);
    PutWord(replacement.data(), 128);
    for (unsigned i = 1; i < 64; ++i) {
        PutWord(replacement.data() + 32 + 96 * i, 64 + i);
    }
    assert(catalog.Publish(empty, initial.size(), Source{initial}, accept, large) == CatalogResult::Ok);
    const auto old = nor.data;
    assert(catalog.Publish(large, compact.size(), Source{compact}, accept, small) == CatalogResult::Ok);
    assert(catalog.Publish(small, compact.size(), Source{compact}, accept, smaller) == CatalogResult::Ok);
    assert(smaller.slot == large.slot);
    assert(std::memcmp(nor.data.data() + 4096, old.data() + 4096, 65536 - 4096) == 0);
    NativeCatalog<Nor> reopened(nor);
    CatalogSelection selected;
    assert(reopened.Open(selected, accept) == CatalogResult::Ok);
    assert(selected.root.generation == smaller.root.generation && selected.root.bytes == compact.size());
    assert(reopened.Publish(selected, compact.size(), Source{compact}, accept, bridge) == CatalogResult::Ok);
    assert(reopened.Publish(bridge, replacement.size(), Source{replacement}, accept, grown) == CatalogResult::Ok);
    assert(grown.slot == large.slot && grown.root.bytes == replacement.size());
    assert(std::memcmp(nor.data.data() + grown.root.address, replacement.data(), replacement.size()) == 0);
}

void RefuseUnsupportedForwardOrder() {
    Nor nor;
    NativeCatalog<Nor> catalog(nor);
    CatalogSelection empty, selected;
    auto data = payload();
    data[6] = 1;
    data.resize(data.size() + 24);
    const auto offset = data.size() - 24;
    auto *extent = data.data() + offset;
    extent[0] = 2;
    extent[2] = 1;
    PutWord(extent + 4, 4);
    PutWord(extent + 12, 3);
    PutWord(extent + 16, kOutlineFirst);
    assert(catalog.Publish(empty, data.size(), Source{data}, accept, selected) == CatalogResult::Ok);

    // Make a committed catalog with correct CRCs and the old zero marker.
    auto *header = nor.data.data();
    auto *stored = header + 32;
    PutWord(stored + offset + 16, 0);
    PutWord(header + 20, Crc(0, stored, data.size()));
    PutWord(header + 24, Crc(0, header, 24));
    const auto bytes = nor.data;
    NativeCatalog<Nor> reopened(nor);
    CatalogRoot root;
    assert(reopened.Inspect(0, root) == CatalogResult::Ok);
    assert(reopened.Open(selected, accept) == CatalogResult::Corrupt);
    assert(nor.data == bytes);
}

void ReuseIdenticalCatalogValidation() {
    Nor nor;
    NativeCatalog<Nor> catalog(nor);
    CatalogSelection empty, first, second, selected;
    auto data = payload();
    assert(catalog.Publish(empty, data.size(), Source{data}, accept, first) == CatalogResult::Ok);
    assert(catalog.Publish(first, data.size(), Source{data}, accept, second) == CatalogResult::Ok);
    unsigned validations = 0;
    auto validate = [&](const CatalogRoot &root) {
        ++validations;
        return catalog.Validate(root, accept);
    };

    assert(catalog.OpenValidated(selected, validate) == CatalogResult::Ok && validations == 2);
    validations = 0;
    const auto reopen = [&] {
        return catalog.OpenValidated(selected, validate, CatalogValidation::ReuseIdenticalPayload);
    };
    assert(reopen() == CatalogResult::Ok && validations == 1);
    assert(selected.root.generation == second.root.generation);
    assert(catalog.IsTrusted(first.root) && catalog.IsTrusted(second.root));

    data[32 + 16] = 'N';
    CatalogSelection third;
    assert(catalog.Publish(second, data.size(), Source{data}, accept, third) == CatalogResult::Ok);
    validations = 0;
    assert(reopen() == CatalogResult::Ok && validations == 2);
    assert(selected.root.generation == third.root.generation);
    nor.data[third.root.address + 32 + 16] ^= 1;
    validations = 0;
    assert(reopen() == CatalogResult::Quarantined && validations == 1);
    assert(selected.quarantine && selected.root.generation == second.root.generation);
    assert(!catalog.CanWrite(selected.root));
}

int main() {
    faults(1);faults(64);
    ShrinkThenGrow();
    ReuseIdenticalCatalogValidation();
    RefuseUnsupportedForwardOrder();
    // First provisioning can leave no committed root, never a fabricated root.
    auto initial=payload();Nor dryInitial;NativeCatalog<Nor> initialCatalog(dryInitial);
    CatalogSelection virgin,initialized;
    assert(initialCatalog.Publish(virgin,initial.size(),Source{initial},accept,initialized)==CatalogResult::Ok);
    unsigned initialCases=0;
    for(unsigned point=1;point<=dryInitial.operations;++point)for(unsigned tear:{0u,1u,127u,4095u}) {
        Nor interrupted;interrupted.cut=point;interrupted.tear=tear;NativeCatalog<Nor> instance(interrupted);
        CatalogSelection result;
        try {instance.Publish(virgin,initial.size(),Source{initial},accept,result);assert(false);}catch(const Cut &){}
        interrupted.cut=0;const auto snapshot=interrupted.data;
        const auto status=instance.Open(result,accept);
        assert(status==CatalogResult::Empty || status==CatalogResult::Ok);
        assert(interrupted.data==snapshot);++initialCases;
    }
    std::cout<<"Native first catalog: "<<initialCases<<" torn provisioning cases passed\n";
    Nor nor;NativeCatalog<Nor> catalog(nor);CatalogSelection empty,first,second,output;
    auto data=payload(2);
    assert(catalog.Publish(empty,data.size(),Source{data},accept,first)==CatalogResult::Ok);
    assert(first.root.generation==1 && first.slot==0);
    CatalogShape shape;
    assert(catalog.Validate(first.root,accept,&shape)==CatalogResult::Ok && shape.dictionaries==2);
    const auto before=nor.data;
    auto invalid=data;PutWord(invalid.data()+32+96,1); // Duplicate stable ID.
    assert(catalog.Publish(first,invalid.size(),Source{invalid},accept,second)==CatalogResult::Corrupt);
    assert(nor.data==before);
    nor.fail_read=true;
    assert(catalog.Open(output,accept)==CatalogResult::IoError);
    nor.fail_read=false;nor.fail_erase=true;
    assert(catalog.Publish(first,data.size(),Source{data},accept,second)==CatalogResult::IoError);
    assert(nor.data==before);
    nor.fail_erase=false;nor.fail_program=true;
    assert(catalog.Publish(first,data.size(),Source{data},accept,second)==CatalogResult::IoError);
    nor.fail_program=false;
    assert(catalog.Open(output,accept)==CatalogResult::Ok && output.root.generation==1);
    invalid=data;invalid[32+12]=45;
    assert(catalog.Publish(first,invalid.size(),Source{invalid},accept,second)==CatalogResult::Corrupt);
    assert(nor.data==before);
    assert(catalog.Publish(first,data.size(),Source{data},accept,second)==CatalogResult::Ok);
    const auto stale=first;
    const auto beforeStale=nor.data;
    assert(catalog.Publish(stale,data.size(),Source{data},accept,output)==CatalogResult::Invalid);
    assert(nor.data==beforeStale);
    assert(catalog.Publish(second,data.size(),Source{data},accept,first)==CatalogResult::Ok);
    const auto saved=nor.data;
    assert(catalog.Publish(stale,data.size(),Source{data},accept,output)==CatalogResult::Invalid);
    assert(nor.data==saved);
    // Corrupt newer committed contents: fallback is readable but quarantined.
    nor.data[first.root.address+16]^=1;
    assert(catalog.Open(output,accept)==CatalogResult::Quarantined);
    assert(output.root.generation==2 && output.quarantine);
    const auto corruptBytes=nor.data;
    assert(catalog.Publish(output,data.size(),Source{data},accept,first)==CatalogResult::Invalid);
    assert(nor.data==corruptBytes);
    // Unknown nonblank flash is not implicitly formatted on first publication.
    Nor unknown;unknown.data[500]=0;NativeCatalog<Nor> unrecognized(unknown);
    const auto unknownBytes=unknown.data;
    assert(unrecognized.Publish(empty,data.size(),Source{data},accept,first)==CatalogResult::Corrupt);
    assert(unknown.data==unknownBytes);
    // Two base extents cannot overlap or be unowned; base-reference typing checked.
    auto owned=payload();owned.resize(32+96+24,0);owned[6]=1;owned[32+8]=0;owned[32+9]=0;
    auto *extent=owned.data()+32+96;
    extent[0]=2;extent[2]=1;PutWord(extent+4,128);PutWord(extent+12,1);
    Nor fresh;NativeCatalog<Nor> freshCatalog(fresh);
    assert(freshCatalog.Publish(empty,owned.size(),Source{owned},accept,first)==CatalogResult::Ok);
    owned[32+8]=owned[32+9]=255;
    const auto ownedBytes=fresh.data;
    assert(freshCatalog.Publish(first,owned.size(),Source{owned},accept,second)==CatalogResult::Corrupt);
    assert(fresh.data==ownedBytes);
    // Catalog slots cannot be recycled while any reader still uses their root.
    Nor pinNor;NativeCatalog<Nor> pinned(pinNor);CatalogSelection a,b,c;
    auto pinData=payload();
    assert(pinned.Publish(empty,pinData.size(),Source{pinData},accept,a)==CatalogResult::Ok);
    uint32_t tokens[5]{};
    const auto pinReads=pinNor.reads;
    for(unsigned i=0;i<4;++i)assert(pinned.AcquirePin(a.root,tokens[i]));
    assert(pinNor.reads==pinReads); // No metadata CRC/read work per dictionary lookup.
    assert(!pinned.AcquirePin(a.root,tokens[4]) && !tokens[4]);
    assert(pinned.Publish(a,pinData.size(),Source{pinData},accept,b)==CatalogResult::Ok);
    auto pinnedBytes=pinNor.data;
    assert(pinned.Publish(b,pinData.size(),Source{pinData},accept,c)==CatalogResult::Busy);
    assert(pinNor.data==pinnedBytes);
    unsigned visits=0;
    assert(pinned.VisitPins([&](const CatalogRoot &root){++visits;return root.generation==1;})==CatalogResult::Ok && visits==4);
    for(unsigned i=0;i<4;++i){assert(pinned.ReleasePin(tokens[i]));assert(!pinned.ReleasePin(tokens[i]));}
    assert(pinned.Publish(b,pinData.size(),Source{pinData},accept,c)==CatalogResult::Ok);
    assert(!pinned.AcquirePin(a.root,tokens[0]));
    NativeCatalog<Nor> reopenedPins(pinNor);
    assert(!reopenedPins.AcquirePin(c.root,tokens[0]));
    CatalogSelection reopenedSelection;
    assert(reopenedPins.Open(reopenedSelection,accept)==CatalogResult::Ok);
    assert(reopenedPins.AcquirePin(c.root,tokens[0]));
    assert(reopenedPins.ReleasePin(tokens[0]));
    // Stable ID allocation is monotonic across deletion and cannot resurrect an
    // earlier ID whose old overlays might survive in journal/retained roots.
    Nor idsNor;NativeCatalog<Nor> idsCatalog(idsNor);CatalogSelection idsA,idsB,idsC;
    auto idsData=payload(2);
    assert(idsCatalog.Publish(empty,idsData.size(),Source{idsData},accept,idsA)==CatalogResult::Ok);
    auto removed=payload(1);PutWord(removed.data(),3);
    assert(idsCatalog.Publish(idsA,removed.size(),Source{removed},accept,idsB)==CatalogResult::Ok);
    auto idBytes=idsNor.data;
    assert(idsCatalog.Publish(idsB,idsData.size(),Source{idsData},accept,idsC)==CatalogResult::Invalid);
    assert(idsNor.data==idBytes);
    auto lowered=payload(1);
    assert(idsCatalog.Publish(idsB,lowered.size(),Source{lowered},accept,idsC)==CatalogResult::Invalid);
    assert(idsNor.data==idBytes);
    PutWord(idsData.data(),4);PutWord(idsData.data()+32+96,3);
    assert(idsCatalog.Publish(idsB,idsData.size(),Source{idsData},accept,idsC)==CatalogResult::Ok);
    owned[32+8]=owned[32+9]=0;
    assert(freshCatalog.Publish(first,owned.size(),Source{owned},[](uint32_t,const uint8_t *){return false;},second)==CatalogResult::Corrupt);
    assert(fresh.data==ownedBytes);
    // Explicit reset cannot leave old validated roots usable through the cache.
    uint32_t resetPin=0;
    assert(idsCatalog.AcquirePin(idsC.root,resetPin));
    idsCatalog.Invalidate();
    assert(!idsCatalog.HasPin(resetPin,idsC.root) && !idsCatalog.CanWrite(idsC.root));
    assert(idsCatalog.ReleasePin(resetPin));
    assert(idsNor.EraseCatalog(0,65536) && idsNor.EraseCatalog(65536,65536));
    CatalogSelection resetRoot;
    assert(idsCatalog.Publish(empty,initial.size(),Source{initial},accept,resetRoot)==CatalogResult::Ok);
    assert(resetRoot.root.generation==1 && idsCatalog.CanWrite(resetRoot.root));
    assert(!idsCatalog.IsTrusted(idsC.root));
    // Streaming staged index writes verify each page and do not publish roots.
    Nor indexNor;IndexSink<Nor> sink(indexNor);
    assert(sink.Begin(3*65536,4096));
    for(uint32_t i=0;i<200;++i)assert(sink.Append(2*65536+256+i*4));
    assert(sink.Finish() && sink.Finish() && sink.Finished());
    assert(sink.Count()==200 && sink.Bytes()==800);
    assert(sink.Checksum()==Crc(0,indexNor.data.data()+3*65536,800));
    assert(!sink.Append(2*65536+256));
    assert(!sink.Begin(3*65536+1,4096));
    IndexSink<Nor> limited(indexNor);
    assert(limited.Begin(4*65536,4));
    assert(limited.Append(2*65536+256));
    assert(!limited.Append(2*65536+260));
    assert(!limited.Finish()); // Capacity refusal cannot publish a truncated table.
    // Every partial page is unpublished; no interrupted sink can advance a root.
    unsigned indexCases=0;
    for(unsigned point=1;point<=4;++point)for(unsigned tear:{0u,1u,127u,255u}) {
        Nor trial;trial.cut=point;trial.tear=tear;
        IndexSink<Nor> interrupted(trial);
        assert(interrupted.Begin(3*65536,4096));
        try {
            for(uint32_t i=0;i<200;++i)assert(interrupted.Append(2*65536+256+i*4));
            interrupted.Finish();assert(false);
        }catch(const Cut &){}
        assert(!interrupted.Finished());
        ++indexCases;
    }
    std::cout<<"Native index sink: CRC/readback, page buffering, capacity refusal; "<<indexCases<<" torn-page cases passed\n";
    std::cout<<"Native catalog: streamed schema, ownership, preflight, stale roots, quarantine and unknown-data refusal passed\n";
}
