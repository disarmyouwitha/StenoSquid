#include "native_dictionary_adapter.h"
#include <cassert>
#include <cstring>
#include <string>
#include <iostream>
using namespace candidate_b;
struct Backend final : DictionaryBackend {
    EditState state=EditState::Set;
    unsigned pins=0,acquires=0,releases=0,bases=0;
    bool available=true,readable=true;
    uint16_t size=6;
    std::string contents="ça va";
    bool Acquire(uint32_t id,uint32_t &token)override {
        assert(id==7);++acquires;
        if(!available)return false;
        token=++pins;return true;
    }
    void Release(uint32_t token)override {
        assert(token && pins);--pins;++releases;contents="mutated after pin";
    }
    EditState Find(uint32_t,uint32_t,const StenoDictionaryLookup &,EditText &text)override {
        assert(pins);text={123,size};return state;
    }
    bool Read(uint32_t,const EditText &text,char *out)override {
        assert(pins && text.bytes==size);
        if(!readable)return false;
        assert(contents.size()>=text.bytes);
        std::memcpy(out,contents.data(),text.bytes);return true;
    }
    StenoDictionaryLookupResult Base(uint32_t,uint32_t,const StenoDictionaryLookup &)override {
        assert(pins);++bases;char *text=static_cast<char *>(std::malloc(5));std::memcpy(text,"base",5);
        return StenoDictionaryLookupResult::CreateDynamicString(text);
    }
};
int main() {
    Backend backend;NativeOverlayDictionary dictionary(backend,7,"Medical");
    assert(!std::strcmp(dictionary.GetName(),"Medical"));
    const StenoStroke stroke(8);
    StenoDictionaryLookup lookup(&stroke,1);
    auto result=dictionary.Lookup(lookup);
    assert(result.IsValid() && !std::strcmp(result.GetText(),"ça va") && !backend.pins);
    assert(backend.contents=="mutated after pin");result.Destroy();
    for(auto state:{EditState::Missing,EditState::Reset}) {
        backend.state=state;result=dictionary.Lookup(lookup);
        assert(result.IsValid() && !std::strcmp(result.GetText(),"base") && !backend.pins);result.Destroy();
    }
    unsigned bases=backend.bases;
    for(auto state:{EditState::Remove,EditState::Error}) {
        backend.state=state;result=dictionary.Lookup(lookup);
        assert(!result.IsValid() && backend.bases==bases && !backend.pins);result.Destroy();
    }
    backend.state=EditState::Set;backend.size=2048;
    result=dictionary.Lookup(lookup);assert(!result.IsValid() && !backend.pins);result.Destroy();
    backend.size=2047;backend.contents=std::string(2047,'x');
    result=dictionary.Lookup(lookup);assert(result.IsValid() && std::strlen(result.GetText())==2047 && !backend.pins);result.Destroy();
    backend.size=0;backend.contents="";
    result=dictionary.Lookup(lookup);assert(result.IsValid() && !*result.GetText() && !backend.pins);result.Destroy();
    backend.readable=false;result=dictionary.Lookup(lookup);assert(!result.IsValid() && !backend.pins);result.Destroy();
    backend.available=false;unsigned releases=backend.releases;
    result=dictionary.Lookup(lookup);assert(!result.IsValid() && backend.releases==releases);result.Destroy();
    unsigned acquires=backend.acquires;
    for(unsigned n:{0u,17u}) {
        result=dictionary.Lookup(StenoDictionaryLookup(&stroke,n));
        assert(!result.IsValid() && backend.acquires==acquires);result.Destroy();
    }
    result=dictionary.Lookup(StenoDictionaryLookup(nullptr,1));assert(!result.IsValid() && backend.acquires==acquires);result.Destroy();
    std::cout<<"Native Javelin adapter: pinned RAM-copy lifetime, tombstone/reset/base fallthrough, UTF-8, empty/long text and error cleanup passed\n";
}
