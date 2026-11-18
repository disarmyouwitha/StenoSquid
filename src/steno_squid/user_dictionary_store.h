#pragma once

class Console;
class StenoDictionary;

StenoDictionary *steno_squid_user_dictionary_open();
void steno_squid_user_dictionary_register(Console &console, StenoDictionary &parent);

#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
// Read-only access under the existing engine lock. Never opens/formats storage.
StenoDictionary *steno_squid_user_dictionary_current();
#endif
