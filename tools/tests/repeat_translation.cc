#include "engine.h"
#include "native_store_fixture.h"
#include "native_engine_backend.h"
#include "button_script_manager.h"
#include "dictionary/dictionary.h"
#include "dictionary/dictionary_list.h"
#include "key.h"
#include "clock.h"
#include "native_input_queue.h"
#include "native_collection_sync.h"
#include "processor/all_up.h"
#include "processor/jeff_modifiers.h"
#include "processor/repeat.h"
#include <cstdio>
#include <cstdlib>
#include <new>
#include <string>
#include <vector>

class Fixture final : public StenoDictionary {
public:
  Fixture() : StenoDictionary(2) {}
  const char *GetName() const override { return "repeat fixture"; }
  StenoDictionaryLookupResult Lookup(const StenoDictionaryLookup &q) const override {
    const char *text=nullptr;
    if (q.length==2 && q.strokes[0]==StenoStroke("HE") && q.strokes[1]==StenoStroke("HROE")) text="hello";
    if (q.length==2 && q.strokes[0]==StenoStroke("KAT") && q.strokes[1]==StenoStroke("TP")) text="red cat";
    if (q.length==1) {
      if(q.strokes[0]==StenoStroke("KAT"))text="cat";
      if(q.strokes[0]==StenoStroke("#"))text="=repeat_last_translation";
      if(q.strokes[0]==StenoStroke("HROE"))text="alone";
      if(q.strokes[0]==StenoStroke("S"))text="{&a}";
      if(q.strokes[0]==StenoStroke("T"))text="{&b}";
      if(q.strokes[0]==StenoStroke("-S"))text="{^s}";
      if(q.strokes[0]==StenoStroke("-F"))text="{.}";
    }
    return text ? StenoDictionaryLookupResult::CreateStaticString(text) : StenoDictionaryLookupResult::CreateInvalid();
  }
};
std::string output() {
  std::string result;uint32_t mods=0;
  for(const auto &event:Key::history) {
    auto code=event.code.value;
    if(code>=KeyCode::L_CTRL && code<=KeyCode::R_META) {
      if(event.isPress)mods|=1u<<(8+code-KeyCode::L_CTRL);else mods&=~(1u<<(8+code-KeyCode::L_CTRL));
      continue;
    }
    if(!event.isPress)continue;
    if(code==KeyCode::BACKSPACE){if(!result.empty())result.pop_back();continue;}
    auto c=KeyCode::ConvertToUnicode(code|mods);
    if(c && c<128)result+=char(c);
  }
  return result;
}
void check(const char *name,std::vector<const char*> sequence,const char *expected) {
 Fixture dictionary;StenoCompiledOrthography orthography(StenoOrthography::emptyOrthography);
 StenoEngine engine(dictionary,nullptr,orthography);Key::history.clear();
 for(auto s:sequence){StenoStroke stroke;stroke.Set(s);engine.Process(stroke);}
 auto actual=output();
 if(actual!=expected){fprintf(stderr,"FAIL %s: expected [%s], got [%s]\n",name,expected,actual.c_str());exit(1);}
 printf("PASS %s: [%s]\n",name,actual.c_str());
}
void check_disabled_dictionaries() {
  Fixture fixture;
  List<StenoDictionaryListEntry> entries;
  entries.Add(StenoDictionaryListEntry(&fixture, false));
  StenoDictionaryList dictionary(static_cast<List<StenoDictionaryListEntry> &&>(entries));
  StenoCompiledOrthography orthography(StenoOrthography::emptyOrthography);
  // Match the zero-initialized engine storage on the keyboard.
  alignas(StenoEngine) static unsigned char storage[sizeof(StenoEngine)]{};
  auto &engine = *new (storage) StenoEngine(dictionary, nullptr, orthography);
  Key::history.clear();
  engine.Process(StenoStroke("KAT"));
  engine.Process(StenoStroke("TP"));
  engine.Process(StenoStroke("*"));
  if (output() != "KAT") {
    fprintf(stderr, "FAIL disabled dictionaries: expected [KAT], got [%s]\n",
            output().c_str());
    exit(1);
  }
  puts("PASS disabled dictionaries: raw strokes and undo");
  engine.~StenoEngine();
}

void check_native_store_engine() {
  NativeFixture storage;
  struct Bases final : candidate_b::NativeBaseProvider {
    bool ready=true;Fixture upper;
    bool Ready()const override{return ready;}
    void Fault()override{ready=false;}
    StenoDictionaryLookupResult Lookup(uint32_t id,const StenoDictionaryLookup &query)override {
      if(id==2)return upper.Lookup(query);
      if(id==3 && query.length==1 && query.strokes[0]==StenoStroke("KAT"))return StenoDictionaryLookupResult::CreateStaticString("lower");
      return StenoDictionaryLookupResult::CreateInvalid();
    }
  } bases;
  candidate_b::NativeEngineBackend<NativeDevice> backend(storage.store,bases);
  candidate_b::NativeOverlayDictionary personal(backend,1,"user_dictionary"),upper(backend,2,"Upper"),lower(backend,3,"Lower");
  List<StenoDictionaryListEntry> entries;for(auto *d:{&personal,&upper,&lower})entries.Add(StenoDictionaryListEntry(d,true));
  StenoDictionaryList root(static_cast<List<StenoDictionaryListEntry> &&>(entries));
  auto play=[&](std::vector<const char *> strokes,const char *expected) {
    StenoCompiledOrthography orthography(StenoOrthography::emptyOrthography);
    alignas(StenoEngine) unsigned char memory[sizeof(StenoEngine)]{};
    auto &engine=*new (memory) StenoEngine(root,nullptr,orthography);
    Key::history.clear();for(auto text:strokes){StenoStroke stroke;stroke.Set(text);engine.Process(stroke);}
    if(output()!=expected){fprintf(stderr,"FAIL native engine expected [%s], got [%s]\n",expected,output().c_str());exit(1);}
    engine.~StenoEngine();
  };
  play({"KAT","#"},"cat cat");
  auto edit=[&](uint32_t id,const char *outline,const char *text,uint8_t op=0) {
    StenoStroke key;key.Set(outline);assert(storage.Edit(id,key.GetKeyState(),text,op)==LiveResult::Ok);root.OnLookupDataChanged();
  };
  edit(1,"KAT","dog");play({"KAT","#","*"},"dog");
  edit(1,"KAT","",2);edit(2,"KAT","",1);play({"KAT"},"lower");
  edit(2,"KAT","",2);play({"KAT"},"cat");
  const uint32_t multi[]={StenoStroke("HE").GetKeyState(),StenoStroke("HROE").GetKeyState()};
  const uint8_t greeting[]={'g','r','e','e','t','i','n','g'};
  assert(storage.store.Edit(1,0,multi,2,greeting,sizeof(greeting))==LiveResult::Ok);root.OnLookupDataChanged();
  play({"HE","HROE","#","*"},"greeting");
  assert(storage.store.Start(NativeStore<NativeDevice>::Work::Checkpoint)==JobResult::More);
  while(storage.store.work!=NativeStore<NativeDevice>::Work::None) {
    auto result=upper.Lookup(StenoDictionaryLookup(reinterpret_cast<const StenoStroke *>(multi),1));result.Destroy();
    auto status=storage.store.Step(storage.Check());assert(status==JobResult::More || status==JobResult::Done);
  }
  storage.Open();root.OnLookupDataChanged();play({"HE","HROE"},"greeting");
  assert(storage.store.Enable(2,false,storage.Check())==CatalogResult::Ok);root.DisableDictionary("Upper");storage.Open();
  play({"KAT"},"lower");
  // Simulate warm reopen recovering persisted flags different from the live
  // collection, including all-disabled state and cache invalidation.
  struct Flag { const char *name;bool enabled; } flags[]={{"user_dictionary",false},{"Upper",true},{"Lower",false}};
  assert(StenoSquidSyncNativeCollection(root,3,[&](unsigned i)->const Flag &{return flags[i];}));
  play({"KAT"},"cat");
  flags[1].enabled=false;
  assert(StenoSquidSyncNativeCollection(root,3,[&](unsigned i)->const Flag &{return flags[i];}));
  play({"KAT"},"KAT");
  flags[0].enabled=flags[2].enabled=true;
  assert(StenoSquidSyncNativeCollection(root,3,[&](unsigned i)->const Flag &{return flags[i];}));
  edit(1,"KAT","fault target");
  LiveView broken;assert(storage.store.Snapshot(broken));RecordView corrupt;const uint32_t kat=StenoStroke("KAT").GetKeyState();
  assert(storage.store.live.FindSnapshot(broken,1,&kat,1,corrupt)==LiveResult::Ok);
  storage.nor.data[corrupt.text_address]^=1;assert(storage.store.Release(broken));root.OnLookupDataChanged();
  auto refused=root.Lookup(StenoDictionaryLookup(reinterpret_cast<const StenoStroke *>(&kat),1));
  assert(!refused.IsValid() && !bases.ready);refused.Destroy();
  puts("PASS native catalog engine: priority, personal/base overlays, tombstone/reset, multi-stroke, repeat/undo, pinned maintenance reads and persistent selection");
}

void check_buffered_input_pipeline() {
  Fixture dictionary;StenoCompiledOrthography orthography(StenoOrthography::emptyOrthography);
  alignas(StenoEngine) unsigned char memory[sizeof(StenoEngine)]{};
  auto &engine=*new (memory) StenoEngine(dictionary,nullptr,orthography);
  StenoJeffModifiers jeff(engine);StenoAllUp all_up(jeff);StenoRepeat repeat(all_up);
  StenoProcessor processor(repeat);candidate_b::NativeInputQueue<16> queue;
  uint32_t now=Clock::GetMilliseconds();Key::history.clear();
  auto chord=[&](std::initializer_list<StenoKey> keys) {
    for(bool down:{true,false})for(auto key:keys) {
      assert(queue.Offer(int(key),down,++now)==candidate_b::InputOffer::Queued);
    }
  };
  auto drain=[&]() {
    candidate_b::NativeInputEvent event;assert(queue.BeginDrain());
    while(queue.Pop(event)) {
      Clock::AdvanceMilliseconds(event.when-Clock::GetMilliseconds());
      if(event.key==255) {
        repeat.Process(StenoKeyState(),StenoAction::CANCEL_ALL);
        new (&processor) StenoProcessor(repeat);
      } else processor.Process(StenoKey(event.key),event.pressed);
    }
    queue.FinishDrain();
  };
  queue.Pause();chord({StenoKey::KL,StenoKey::A,StenoKey::TR});
  chord({StenoKey::NUM1});queue.Resume();drain();assert(output()=="cat cat");
  queue.Pause();chord({StenoKey::STAR1});queue.Resume();drain();assert(output()=="cat");
  // A mode barrier cannot turn the pending half-chord into a word.
  queue.Pause();assert(queue.Offer(int(StenoKey::KL),true,++now)==candidate_b::InputOffer::Queued);
  queue.Cancel(++now);queue.Resume();drain();
  assert(queue.Offer(int(StenoKey::KL),false,++now)==candidate_b::InputOffer::Discarded);drain();
  assert(output()=="cat");
  queue.Pause();chord({StenoKey::HL,StenoKey::E});
  chord({StenoKey::HL,StenoKey::RL,StenoKey::O,StenoKey::E});queue.Resume();drain();
  assert(output()=="cat hello");
  // Overflow after a press was already consumed must cancel that held chord,
  // including its later release. Otherwise it could emit a different word.
  queue.Pause();queue.Offer(int(StenoKey::KL),true,++now);queue.Resume();drain();
  queue.Pause();
  for(unsigned i=0;i<10;++i)for(bool down:{true,false})queue.Offer(int(StenoKey::A),down,++now);
  queue.Offer(int(StenoKey::KL),false,++now);queue.Resume();drain();
  assert(queue.Overflows()==1 && output()=="cat hello");
  queue.Pause();chord({StenoKey::KL,StenoKey::A,StenoKey::TR});queue.Resume();drain();
  assert(output()=="cat hello cat");
  repeat.Process(StenoKeyState(),StenoAction::CANCEL_ALL);engine.~StenoEngine();
  puts("PASS buffered input through actual Repeat/AllUp/JeffModifiers/Javelin: cat, repeat, undo, mode barrier, multi-stroke and partial-chord overflow recovery");
}

int main(){
 alignas(4) static const uint8_t script[64]={0x4a,0x53,0x53,0x34};ButtonScriptManager::Initialize(script);
 check_native_store_engine();
 check_buffered_input_pipeline();
 check_disabled_dictionaries();
 check("empty history",{"#"},"");
 check("single stroke",{"KAT","#"},"cat cat");
 check("repeat twice and undo",{"KAT","#","#","*"},"cat cat");
 check("multi-stroke translation",{"HE","HROE","#"},"hello hello");
 check("multi-stroke undo",{"HE","HROE","#","*"},"hello");
 check("multi-word translation",{"KAT","TP","#"},"red cat red cat");
 check("suffix formatting",{"KAT","-S","#"},"catss");
 check("glue formatting",{"S","T","#"},"abb");
 check("punctuation",{"KAT","-F","#"},"cat..");
 std::vector<const char*> longRepeat={"KAT"};std::string longExpected="cat";
 for(int i=0;i<40;i++){longRepeat.push_back("#");longExpected+=" cat";}
 check("long repeat history",longRepeat,longExpected.c_str());
}
