#include "user_dictionary_store.h"
#include "console.h"
#include "dictionary/dictionary.h"
#include "lapwing_flash.h"
#include "stroke_list_parser.h"
#include "user_dictionary_snapshot.h"
#include "xip_load.h"
#include "w25q_quad.h"
#include <stdlib.h>
#include <string.h>
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
namespace {
using namespace squid_user;
static_assert(BASE_DICTIONARY_LIMIT <= SLOT_START);
static_assert(SLOT_START + 2 * SLOT_BYTES <= 0xfff000);
static_assert(sizeof(StenoStroke) == 4);
constexpr uintptr_t XIP_BASE = DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi), qspi_mm);
bool wait_ready() {
  int64_t deadline = k_uptime_get() + 2000;
  do {
    nrfx_err_t rc = nrfx_qspi_mem_busy_check();
    if (rc != NRFX_ERROR_BUSY)
      return rc == NRFX_SUCCESS;
    k_msleep(1);
  } while (k_uptime_get() < deadline);
  return false;
}
class QspiDevice final : public Device {
public:
  bool mapped = false;
  bool Read(uint32_t address, void *data, size_t size) override {
    if (address > 0x1000000 || size > 0x1000000 - address)
      return false;
    if (mapped) {
      copy_xip_bytes(data, reinterpret_cast<const void *>(XIP_BASE + address),
                     size);
      return true;
    }
    auto *out = static_cast<uint8_t *>(data);
    alignas(4) uint8_t buffer[256];
    while (size) {
      uint32_t skip = address & 3;
      size_t n = size > sizeof(buffer) - skip ? sizeof(buffer) - skip : size;
      if (nrfx_qspi_read(buffer, (skip + n + 3) & ~size_t(3), address - skip) !=
          NRFX_SUCCESS)
        return false;
      memcpy(out, buffer + skip, n);
      address += n;
      out += n;
      size -= n;
    }
    return true;
  }
  bool Program(uint32_t address, const void *data, size_t size) override {
    if (address < SLOT_START || address > SLOT_START + 2 * SLOT_BYTES ||
        size > SLOT_START + 2 * SLOT_BYTES - address || (address & 3) ||
        (size & 3) || (reinterpret_cast<uintptr_t>(data) & 3))
      return false;
    return nrfx_qspi_write(data, size, address) == NRFX_SUCCESS && wait_ready();
  }
  bool Erase(uint32_t address) override {
    return address >= SLOT_START && address < SLOT_START + 2 * SLOT_BYTES &&
           EraseSector(address);
  }

private:
  static bool EraseSector(uint32_t address) {
    return !(address % SECTOR) &&
           nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB, address) == NRFX_SUCCESS &&
           wait_ready();
  }
} qspi_device;
Store store(qspi_device);
bool store_available = false;
uint32_t last_save_ms = 0;
Entry make_key(const StenoStroke *strokes, size_t length) {
  Entry e{};
  if (length <= MAX_STROKES) {
    e.length = length;
    memcpy(e.strokes, strokes, length * 4);
  }
  return e;
}
char *copy_text(const Entry &e) {
  char *text = static_cast<char *>(malloc(e.text_bytes));
  if (!text)
    return nullptr;
  if (!store.ReadText(e, 0, text, e.text_bytes)) {
    free(text);
    return nullptr;
  }
  return text;
}
bool same_text(const Entry &e, const char *text) {
  if (e.text_bytes != strlen(text) + 1)
    return false;
  char buffer[64];
  for (uint32_t off = 0; off < e.text_bytes;) {
    size_t n = e.text_bytes - off > sizeof(buffer) ? sizeof(buffer)
                                                   : e.text_bytes - off;
    if (!store.ReadText(e, off, buffer, n) || memcmp(buffer, text + off, n))
      return false;
    off += n;
  }
  return true;
}
class QspiUserDictionary final : public StenoDictionary {
public:
  QspiUserDictionary() : StenoDictionary(0) {}
  void Refresh() {
    maximumOutlineLength = store.Maximum();
    OnLookupDataChanged();
  }
  const char *GetName() const override { return "user_dictionary"; }
  StenoDictionaryLookupResult
  Lookup(const StenoDictionaryLookup &lookup) const override {
    Entry e{};
    if (!store_available || lookup.length > MAX_STROKES ||
        !store.Find(make_key(lookup.strokes, lookup.length), e))
      return StenoDictionaryLookupResult::CreateInvalid();
    char *text = copy_text(e);
    return text ? StenoDictionaryLookupResult::CreateDynamicString(text)
                : StenoDictionaryLookupResult::CreateInvalid();
  }
  const StenoDictionary *
  GetDictionaryForOutline(const StenoDictionaryLookup &lookup) const override {
    Entry e{};
    return store_available && lookup.length <= MAX_STROKES &&
                   store.Find(make_key(lookup.strokes, lookup.length), e)
               ? this
               : nullptr;
  }
  bool CanRemove() const override { return true; }
  bool Remove(const char *name, const StenoStroke *strokes,
              size_t length) override {
    return (!name || strcmp(name, GetName()) == 0) &&
           Edit(make_key(strokes, length), nullptr);
  }
  bool Edit(const Entry &key, const Entry *replacement, bool clear = false) {
    if (!store_available || !reactivate_after_xip())
      return false;
    const int64_t started = k_uptime_get();
    qspi_device.mapped = false;
    EditSource source(store, key, replacement, clear);
    bool ok = source.Prepare();
    if (ok && (clear || replacement || source.Exists()))
      ok = store.Save(source);
    qspi_device.mapped = true;
    last_save_ms = static_cast<uint32_t>(k_uptime_get() - started);
    if (ok)
      Refresh();
    return ok;
  }
  void ReverseLookup(StenoReverseDictionaryLookup &lookup) const override {
    uint32_t hash = hash_bytes(lookup.definition, lookup.definitionLength);
    for (uint32_t i = store.ReverseStart(hash); i < store.Count(); ++i) {
      Entry e{};
      if (!store.Get(i, true, e) || e.text_hash != hash)
        break;
      if (e.length < lookup.ignoreStrokeThreshold &&
          same_text(e, lookup.definition)) {
        StenoStroke strokes[MAX_STROKES];
        memcpy(strokes, e.strokes, e.length * 4);
        lookup.AddResult(strokes, e.length, this);
      }
    }
  }
  void PrintDictionary(PrintDictionaryContext &context) const override {
    if (context.HasName() && strcmp(context.GetName(), GetName()))
      return;
    for (uint32_t i = 0; i < store.Count(); ++i) {
      Entry e{};
      if (!store.Get(i, false, e))
        return;
      char *text = copy_text(e);
      if (!text)
        return;
      StenoStroke strokes[MAX_STROKES];
      memcpy(strokes, e.strokes, e.length * 4);
      context.Print(strokes, e.length, text);
      free(text);
    }
  }
  void PrintEntriesWithPartialOutline(
      PrintPartialOutlineContext &context) const override {
    for (uint32_t i = 0; i < store.Count() && !context.IsDone(); ++i) {
      Entry e{};
      if (!store.Get(i, false, e))
        return;
      if (e.length <= context.length)
        continue;
      StenoStroke strokes[MAX_STROKES];
      memcpy(strokes, e.strokes, e.length * 4);
      if (!StenoStroke::HasPartialOutline(strokes, context.strokes,
                                          context.length,
                                          e.length - context.length))
        continue;
      char *text = copy_text(e);
      if (!text)
        return;
      context.Add(strokes, e.length, text, this);
      free(text);
    }
  }
  void PrintEntriesWithPrefix(PrintPrefixContext &context) const override {
    for (uint32_t i = 0; i < store.Count() && !context.IsDone(); ++i) {
      Entry e{};
      if (!store.Get(i, false, e))
        return;
      if (e.text_bytes <= context.prefixLength + 1)
        continue;
      char *text = copy_text(e);
      if (!text)
        return;
      if (strncmp(text, context.prefix, context.prefixLength) == 0) {
        StenoStroke strokes[MAX_STROKES];
        memcpy(strokes, e.strokes, e.length * 4);
        context.Add(strokes, e.length, text, this);
      }
      free(text);
    }
  }
  void PrintInfo(int depth) const override {
    Console::Printf("%s%s\n%sFormat: QSPI compact snapshots v2\n%sEntries: "
                    "%u\n%sSnapshot bytes: %u/%u\n",
                    Spaces(depth), GetName(), Spaces(depth + 2),
                    Spaces(depth + 2), store.Count(), Spaces(depth + 2),
                    store.Used(), SLOT_BYTES);
  }
} dictionary;
StenoDictionary *parent = nullptr;
bool parse_outline(const char *line, StrokeListParser &parser) {
  const char *space = strchr(line, ' ');
  if (!space || !parser.Parse(space + 1) || !parser.GetCount() ||
      parser.GetCount() > MAX_STROKES) {
    Console::Printf("ERR Invalid outline\n\n");
    return false;
  }
  return true;
}
bool can_edit() {
  if (!store_available)
    Console::Printf("ERR Personal dictionary unavailable\n\n");
  return store_available;
}
void finish_edit(bool ok) {
  if (ok)
    Console::SendOk();
  else
    Console::Printf("ERR Personal dictionary full or save failed; previous "
                    "saved copy retained\n\n");
}
void add_entry(void *, const char *line) {
  StrokeListParser parser;
  if (!can_edit() || !parse_outline(line, parser))
    return;
  if (!*parser.failureOrEnd) {
    Console::Printf("ERR No translation specified\n\n");
    return;
  }
  size_t n = strlen(parser.failureOrEnd);
  if (n > MAX_TEXT) {
    Console::Printf("ERR Translation too long\n\n");
    return;
  }
  Entry e = make_key(parser.GetData(), parser.GetCount()), old{};
  if (store.Find(e, old) && same_text(old, parser.failureOrEnd)) {
    Console::SendOk();
    return;
  }
  e.text = parser.failureOrEnd;
  e.text_bytes = n + 1;
  e.text_hash = hash_bytes(e.text, n);
  finish_edit(dictionary.Edit(e, &e));
}
void remove_entry(void *, const char *line) {
  StrokeListParser parser;
  if (!can_edit() || !parse_outline(line, parser))
    return;
  if (*parser.failureOrEnd) {
    Console::Printf("ERR Unexpected text after outline\n\n");
    return;
  }
  finish_edit(
      dictionary.Edit(make_key(parser.GetData(), parser.GetCount()), nullptr));
}
void reset_entries(void *, const char *) {
  if (can_edit())
    finish_edit(dictionary.Edit(Entry{}, nullptr, true));
}
bool read_number(const char *&line, uint32_t &number) {
  if (*line++ != ' ' || *line < '0' || *line > '9')
    return false;
  number = 0;
  while (*line >= '0' && *line <= '9') {
    const uint32_t digit = *line++ - '0';
    if (number > (UINT32_MAX - digit) / 10)
      return false;
    number = number * 10 + digit;
  }
  return true;
}

int hex_digit(char value) {
  if (value >= '0' && value <= '9')
    return value - '0';
  if (value >= 'a' && value <= 'f')
    return value - 'a' + 10;
  return -1;
}

void bulk_import(void *, const char *line) {
  if (!can_edit())
    return;
  constexpr char command[] = "user_dictionary_import ";
  if (strncmp(line, command, sizeof(command) - 1) != 0) {
    Console::Printf("ERR Missing import operation\n\n");
    return;
  }
  const char *args = line + sizeof(command) - 1;
  if (strcmp(args, "abort") == 0) {
    store.AbortImport();
    Console::SendOk();
    return;
  }
  if (!reactivate_after_xip()) {
    Console::Printf("ERR QSPI activation failed\n\n");
    return;
  }
  qspi_device.mapped = false;
  bool ok = false;
  if (strncmp(args, "begin ", 6) == 0) {
    args += 5;
    uint32_t generation, count, bytes, maximum, crc;
    ok = read_number(args, generation) && read_number(args, count) &&
         read_number(args, bytes) && read_number(args, maximum) &&
         read_number(args, crc) && !*args &&
         store.BeginImport(generation, count, bytes, maximum, crc);
  } else if (strncmp(args, "data ", 5) == 0) {
    args += 4;
    uint32_t offset;
    alignas(4) uint8_t page[256];
    if (read_number(args, offset) && *args == ' ') {
      ++args;
      const size_t length = strlen(args);
      bool valid = length && !(length & 1) && length <= 2 * sizeof(page);
      for (size_t i = 0; valid && i < length / 2; ++i) {
        const int hi = hex_digit(args[2 * i]);
        const int lo = hex_digit(args[2 * i + 1]);
        valid = hi >= 0 && lo >= 0;
        if (valid)
          page[i] = (hi << 4) | lo;
      }
      ok = valid && store.ImportChunk(offset, page, length / 2);
    }
  } else if (strcmp(args, "commit") == 0) {
    ok = store.FinishImport();
    if (ok)
      dictionary.Refresh();
  }
  qspi_device.mapped = true;
  if (ok)
    Console::SendOk();
  else
    Console::Printf("ERR Import failed or dictionary changed. Retry from the "
                    "beginning.\n\n");
}

void lookup_outline(void *, const char *line) {
  StrokeListParser parser;
  if (!parse_outline(line, parser))
    return;
  StenoDictionaryLookup lookup(parser.GetData(), parser.GetCount());
  List<const StenoDictionary *> dictionaries;
  parent->GetDictionariesForOutline(dictionaries, lookup);
  Console::Printf("[");
  bool first = true;
  for (const auto *entry : dictionaries) {
    auto result = entry->Lookup(lookup);
    if (result.IsValid()) {
      Console::Printf(first ? "{t: %Y,d: %Y%s}" : ",{t: %Y,d: %Y%s}",
                      result.GetText(), entry->GetName(),
                      entry->CanRemove() ? ",r: 1" : "");
      first = false;
    }
    result.Destroy();
  }
  Console::Printf("]\n\n");
}
void store_status(void *, const char *) {
  Console::Printf(
      "{\"format\":2,\"available\":%s,\"entries\":%u,\"used_bytes\":%u,"
      "\"slot_bytes\":%u,\"generation\":%u,\"last_save_ms\":%u,"
      "\"ram_mirror_bytes\":0,\"valid_copies\":%u,\"bulk_import\":1}\n\n",
      store_available ? "true" : "false", store.Count(), store.Used(),
      SLOT_BYTES, store.Generation(), last_save_ms, store.ValidCopies());
}
bool erased_headers() {
  const uint32_t addresses[]{SLOT_START, SLOT_START + SLOT_BYTES};
  for (uint32_t address : addresses) {
    uint32_t words[8];
    if (!qspi_device.Read(address, words, sizeof(words)))
      return false;
    for (uint32_t word : words)
      if (word != 0xffffffffu)
        return false;
  }
  return true;
}
} // namespace
StenoDictionary *steno_squid_user_dictionary_open() {
  if (!reactivate_after_xip())
    return nullptr;
  qspi_device.mapped = false;
  // A fresh device commits its first snapshot on the first edit.
  store_available = store.Open() || erased_headers();
  qspi_device.mapped = true;
  dictionary.Refresh();
  return store_available ? &dictionary : nullptr;
}
void steno_squid_user_dictionary_register(Console &console,
                                          StenoDictionary &root) {
  parent = &root;
  console.RegisterCommand("user_dictionary_import", "Import a QSPI snapshot",
                          bulk_import, nullptr);
  console.RegisterCommand("user_dictionary_status",
                          "Read personal snapshot usage", store_status,
                          nullptr);
  dictionary.SetParentRecursively(parent);
  console.RegisterCommand("lookup_stroke",
                          "Look up an outline in the dictionaries",
                          lookup_outline, nullptr);
  console.RegisterCommand("add_user_entry", "Save a personal translation",
                          add_entry, nullptr);
  console.RegisterCommand("remove_user_entry", "Remove a personal translation",
                          remove_entry, nullptr);
  console.RegisterCommand("reset_user_dictionary",
                          "Clear personal translations", reset_entries,
                          nullptr);
}
