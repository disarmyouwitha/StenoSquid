#!/usr/bin/env python3
"""Exercise the actual word-answer lookup with active/disabled dictionaries."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'src/engine_init.cc').read_text()
start = source.index('static bool practice_correct(')
brace = source.index('{', start)
depth, end = 1, brace + 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
function = source[start:end]
prefix = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#include <strings.h>
#include <string>
#include "split_status.h"
struct StenoStroke {
  std::string outline;
  void Set(const char *s) { outline=s; }
  bool operator==(const StenoStroke &other) const { return outline==other.outline; }
};
struct StenoDictionaryLookup { StenoDictionaryLookup(const StenoStroke *,unsigned) {} };
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
static struct { steno_squid_practice_drill drill=STENO_SQUID_PRACTICE_WORDS; } practice;
static unsigned practice_prompt() { return 0; }
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
  practice.drill=STENO_SQUID_PRACTICE_LEFT;
  const unsigned lookups=active.calls;
  stroke.Set("S");assert(practice_correct(stroke));
  stroke.Set("K");assert(!practice_correct(stroke));
  assert(active.calls==lookups); // Raw hand drills never touch dictionaries.
  puts("Practice lookup: alternate active outlines, Lapwing fallback, unavailable base and raw hand drills passed");
}
'''
with tempfile.TemporaryDirectory(prefix='steno-squid-lookup-') as directory:
    output=Path(directory)
    (output/'zephyr').mkdir()
    (output/'zephyr/toolchain.h').write_text('#define BUILD_ASSERT(x,...) static_assert(x)\n')
    (output/'test.cc').write_text(prefix+function+cases)
    subprocess.run(['clang++','-std=c++17','-Wall','-Wextra','-Werror','-I'+str(output),
                    '-I'+str(ROOT/'src/steno_squid'),str(output/'test.cc'),'-o',str(output/'test')],check=True)
    subprocess.run([str(output/'test')],check=True)
