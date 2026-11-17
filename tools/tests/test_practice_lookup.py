#!/usr/bin/env python3
"""Exercise the actual word-answer lookup with active/disabled dictionaries."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'src/steno_squid/practice_engine.cc').read_text(encoding="utf-8")
start = source.index('static bool practice_correct(')
brace = source.index('{', start)
depth, end = 1, brace + 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
function = source[start:end]
first_start=source.index('static bool practice_first_stroke_correct(')
first_end=source.index('\n}\n',first_start)+2
function=source[first_start:first_end]+'\n'+function
prefix = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include <strings.h>
#include <string>
#include "split_status.h"
#include "practice_types.h"
#include "practice_words.h"
#include "practice_multi.h"
#include "practice_build.h"
#include "practice_view.h"
struct StenoStroke {
  std::string outline;
  void Set(const char *s) { outline=s; }
  bool operator==(const StenoStroke &other) const { return outline==other.outline; }
};
static unsigned outline_count;
struct StenoDictionaryLookup { StenoDictionaryLookup(const StenoStroke *,unsigned count) { outline_count=count; } };
struct Result {
  const char *text;
  bool IsValid() const { return text; }
  const char *GetText() const { return text; }
  void Destroy() {}
};
struct StenoDictionary {
  const char *text=nullptr;
  mutable unsigned calls=0;
  Result Lookup(const StenoDictionaryLookup &) const { calls++;return {text}; }
};
static StenoDictionary active, base;
static bool base_available=true;
struct Engine { StenoDictionary &GetDictionary() { return active; } };
namespace StenoEngine { static struct { Engine value; } container; }
static struct { steno_squid_practice_drill drill=STENO_SQUID_PRACTICE_WORDS; StenoStroke multi_strokes[2]; unsigned build_stage=0; } practice;
static unsigned prompt_id;
static unsigned practice_prompt() { return prompt_id; }
static const StenoDictionary *practice_fallback_dictionary() { return base_available?&base:nullptr; }
'''
cases = r'''
int main() {
  StenoStroke stroke;stroke.Set("KAT");
  active.text="cat";base.text="wrong";
  assert(practice_correct(stroke) && active.calls==1 && base.calls==0);
  active.text="wrong";base.text="cat";
  assert(practice_correct(stroke) && base.calls==1);
  active.text=nullptr;
  assert(practice_correct(stroke)); // All engine dictionaries can be disabled.
  active.text="wrong";base_available=false;
  assert(!practice_correct(stroke));
  active.text="CAT";
  assert(practice_correct(stroke)); // Capitalization is not an error.
  active.text=nullptr;
  assert(!practice_correct(stroke));
  practice.drill=STENO_SQUID_PRACTICE_TROUBLE;
  base_available=true;base.text="cat";
  assert(practice_correct(stroke)); // Trouble uses word translation/fallback too.
  practice.drill=STENO_SQUID_PRACTICE_BUILD;
  stroke.Set("KA");assert(!practice_correct(stroke));
  stroke.Set("K");assert(practice_correct(stroke));
  practice.build_stage=1;
  stroke.Set("K");assert(!practice_correct(stroke));
  stroke.Set("KA");assert(practice_correct(stroke));
  practice.build_stage=2;
  stroke.Set("KAT");assert(practice_correct(stroke));
  practice.drill=STENO_SQUID_PRACTICE_MULTI;
  stroke.Set("K");assert(!practice_first_stroke_correct(stroke));
  stroke.Set("HE");assert(practice_first_stroke_correct(stroke));
  active.text="hello";
  assert(practice_correct(stroke) && outline_count==2);
  active.text="wrong";base.text="hello";
  assert(practice_correct(stroke));
  active.text=nullptr;base_available=false;
  assert(!practice_correct(stroke));
  practice.drill=STENO_SQUID_PRACTICE_COLUMNS;
  unsigned calls=active.calls;
  stroke.Set("TK");assert(practice_correct(stroke));
  stroke.Set("TKP");assert(!practice_correct(stroke));
  assert(active.calls==calls);
  prompt_id=8;stroke.Set("AO");assert(practice_correct(stroke));
  stroke.Set("A");assert(!practice_correct(stroke));
  prompt_id=9;stroke.Set("EU");assert(practice_correct(stroke));
  stroke.Set("E");assert(!practice_correct(stroke));
  assert(active.calls==calls);
  prompt_id=0;
  practice.drill=STENO_SQUID_PRACTICE_LEFT;
  const unsigned lookups=active.calls;
  stroke.Set("S");assert(practice_correct(stroke));
  stroke.Set("K");assert(!practice_correct(stroke));
  const auto check_vowel = [&](steno_squid_practice_drill drill, const char *vowel,
                               const char *pair) {
    practice.drill=drill;
    const unsigned count=steno_squid_practice_prompt_count(drill);
    for(prompt_id=0;prompt_id<count;++prompt_id)
      if(std::string(steno_squid_practice_outline(drill,prompt_id))==vowel)
        break;
    assert(prompt_id<count);
    stroke.Set(vowel);assert(practice_correct(stroke));
    stroke.Set(pair);assert(!practice_correct(stroke));
  };
  check_vowel(STENO_SQUID_PRACTICE_LEFT,"A","AO");
  check_vowel(STENO_SQUID_PRACTICE_LEFT,"O","AO");
  check_vowel(STENO_SQUID_PRACTICE_RIGHT,"E","EU");
  check_vowel(STENO_SQUID_PRACTICE_RIGHT,"U","EU");
  assert(active.calls==lookups); // Raw hand drills never touch dictionaries.
  puts("Practice lookup: alternate active outlines, Lapwing fallback, unavailable base and raw hand drills passed");
}
'''
with tempfile.TemporaryDirectory(prefix='steno-squid-lookup-') as directory:
    output=Path(directory)
    (output/'zephyr').mkdir()
    (output/'zephyr/toolchain.h').write_text('#define BUILD_ASSERT(x,...) static_assert(x)\n', encoding="utf-8")
    (output/'test.cc').write_text(prefix+function+cases, encoding="utf-8")
    subprocess.run(['clang++','-std=c++17','-Wall','-Wextra','-Werror','-I'+str(output),
                    '-I'+str(ROOT/'src/steno_squid'),str(output/'test.cc'),'-o',str(output/'test')],check=True)
    subprocess.run([str(output/'test')],check=True)
