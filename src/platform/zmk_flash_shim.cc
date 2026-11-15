#include "flash.h"

// Persistent dictionaries use dedicated QSPI storage on ZMK.
void Flash::EraseBlockInternal(const void *, size_t) {}
void Flash::WriteBlockInternal(const void *, const void *, size_t) {}
