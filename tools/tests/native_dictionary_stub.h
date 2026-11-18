#pragma once
#include <cstdint>
#include <cstdlib>
struct StenoStroke {
    uint32_t value;explicit StenoStroke(uint32_t mask):value(mask){}
    uint32_t GetKeyState()const{return value;}
};
struct StenoDictionaryLookup {
    const StenoStroke *strokes;size_t length;
    StenoDictionaryLookup(const StenoStroke *p,size_t n):strokes(p),length(n){}
};
class StenoDictionaryLookupResult {
    char *text;
    StenoDictionaryLookupResult(char *value):text(value){}
public:
    bool IsValid()const{return text;}
    const char *GetText()const{return text;}
    void Destroy(){std::free(text);text=nullptr;}
    static StenoDictionaryLookupResult CreateInvalid(){return {nullptr};}
    static StenoDictionaryLookupResult CreateDynamicString(char *text){return {text};}
};
class StenoDictionary {
public:
    explicit StenoDictionary(size_t){}
    virtual StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &)const=0;
    virtual const char *GetName()const=0;
    virtual ~StenoDictionary()=default;
};
