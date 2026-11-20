#pragma once

class Console;
class StenoDictionary;

StenoDictionary *steno_squid_user_dictionary_open();
void steno_squid_user_dictionary_register(Console &console, StenoDictionary &parent);
