#include "lapwing_dictionary.h"

StenoDictionaryLookupResult lapwing_copy_lookup_text(const uint8_t *text) {
  return StenoDictionaryLookupResult::CreateStaticString((const char *)text);
}
