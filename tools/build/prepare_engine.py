#!/usr/bin/env python3
"""Apply the pinned engine's Zephyr compatibility edits portably and idempotently."""
import re,sys
from pathlib import Path
root=Path(sys.argv[1])
for relative,old,new in [
 ('dictionary/user_dictionary.h','constexpr StenoUserDictionaryData(const uint8_t','StenoUserDictionaryData(const uint8_t'),
 ('orthography.h','#define USE_ORTHOGRAPHY_CACHE 1','#define USE_ORTHOGRAPHY_CACHE 0')]:
 p=root/relative;text=p.read_text();updated=text.replace(old,new)
 if updated!=text:p.write_text(updated)
pattern=re.compile(r'(?:^|\n)(TEST_BEGIN\(.*?TEST_END)',re.DOTALL)
count=0
for p in root.rglob('*.cc'):
 text=p.read_text();updated=pattern.sub('',text)
 if updated!=text:p.write_text(updated);count+=1
print(f'Engine prepared; removed test blocks in {count} files')

# Prefix/partial-outline responses now receive short-lived RAM copies of QSPI
# strings. The native context must own its previous definition rather than keep
# a pointer after the dictionary frees the copy.
p = root / 'dictionary/dictionary.h'
text = p.read_text()
if '~LookupDictionaryContext()' not in text:
 text = text.replace('  void Add(const StenoStroke *strokes, size_t length, const char *definition,',
                     '  ~LookupDictionaryContext() { free(lastDefinition); }\n\n  void Add(const StenoStroke *strokes, size_t length, const char *definition,', 1)
 text = text.replace('  const char *lastDefinition = "";', '  char *lastDefinition = nullptr;')
 p.write_text(text)
p = root / 'dictionary/dictionary.cc'
text = p.read_text()
old = '''  if (!Str::Eq(definition, lastDefinition)) {
    lastDefinition = definition;
    Console::Printf(",t: %Y", definition);
  }'''
new = '''  if (!lastDefinition || !Str::Eq(definition, lastDefinition)) {
    char *copy = (char *)malloc(strlen(definition) + 1);
    if (copy) { strcpy(copy, definition); }
    free(lastDefinition);
    lastDefinition = copy;
    Console::Printf(",t: %Y", definition);
  }'''
if old in text:
 p.write_text(text.replace(old, new, 1))
elif new not in text:
 raise SystemExit('Pinned dictionary response context changed; review QSPI text ownership patch')
