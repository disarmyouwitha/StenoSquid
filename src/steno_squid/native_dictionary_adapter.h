// Candidate-B development bridge. Not installed in the normal engine collection.
// Backend owns catalog pins, extent validation and scheduling. Returned lookup
// text is copied before releasing its pin so callers never retain QSPI pointers.
#pragma once
#include "dictionary/dictionary.h"
#include <cstdlib>

namespace candidate_b {
enum class EditState { Missing, Set, Remove, Reset, Error };
struct EditText { uint32_t address=0;uint16_t bytes=0; };
class DictionaryBackend {
public:
    virtual bool Acquire(uint32_t dictionary,uint32_t &token)=0;
    virtual void Release(uint32_t token)=0;
    virtual EditState Find(uint32_t token,uint32_t dictionary,
                           const StenoDictionaryLookup &lookup,EditText &text)=0;
    virtual bool Read(uint32_t token,const EditText &text,char *output)=0;
    // Base results must own RAM text or genuinely static internal ROM text, not
    // a pointer into a reclaimable/mutable QSPI extent. Called while pinned.
    virtual StenoDictionaryLookupResult Base(uint32_t token,uint32_t dictionary,
                                             const StenoDictionaryLookup &lookup)=0;
protected:
    ~DictionaryBackend()=default;
};

class NativeOverlayDictionary final : public StenoDictionary {
    DictionaryBackend &backend;
    uint32_t identity;
    const char *name;
    struct Guard {
        DictionaryBackend &backend;uint32_t token;
        ~Guard(){backend.Release(token);}
    };
public:
    NativeOverlayDictionary(DictionaryBackend &value,uint32_t id,const char *label)
        :StenoDictionary(16),backend(value),identity(id),name(label){}
    const char *GetName()const override{return name;}
    StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &lookup)const override {
        if(!lookup.strokes || !lookup.length || lookup.length>16)return StenoDictionaryLookupResult::CreateInvalid();
        uint32_t token=0;
        if(!backend.Acquire(identity,token))return StenoDictionaryLookupResult::CreateInvalid();
        Guard guard{backend,token};
        EditText text;
        const auto state=backend.Find(token,identity,lookup,text);
        if(state==EditState::Missing || state==EditState::Reset)return backend.Base(token,identity,lookup);
        // A per-dictionary tombstone falls through to LOWER PRIORITY dictionaries,
        // never to its own compact base. Corrupt reads also cannot expose its base.
        if(state!=EditState::Set || text.bytes>2047)return StenoDictionaryLookupResult::CreateInvalid();
        char *output=static_cast<char *>(std::malloc(size_t(text.bytes)+1));
        if(!output)return StenoDictionaryLookupResult::CreateInvalid();
        if(!backend.Read(token,text,output)){std::free(output);return StenoDictionaryLookupResult::CreateInvalid();}
        output[text.bytes]=0;
        return StenoDictionaryLookupResult::CreateDynamicString(output);
    }
};
} // namespace candidate_b
