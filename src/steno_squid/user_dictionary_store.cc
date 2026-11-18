#include "user_dictionary_store.h"
#include "dictionary_benchmark.h"
#include "storage_probe.h"
#include "engine_work.h"
#include "engine_stream.h"
#include "console.h"
#include "dictionary/dictionary.h"
#include "javelin_engine_lock.h"
#include "dictionary_flash.h"
#include "split_status.h"
#include "stroke_list_parser.h"
#include "user_dictionary_log.h"
#include "xip_load.h"
#include "w25q_quad.h"
#include <stdlib.h>
#include <string.h>
#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
// Stop an export promptly if its browser transport has failed.
#include "javelin_hid_transport.h"
namespace {
using namespace squid_user;
constexpr uint32_t REGION_END = REGION_START + SEG_COUNT * SEG_BYTES;
static_assert(BASE_DICTIONARY_LIMIT == REGION_START);
static_assert(REGION_START % SEG_BYTES == 0);
static_assert(REGION_END == 0xff0000);
static_assert(sizeof(StenoStroke) == 4);
constexpr uintptr_t XIP_BASE = DT_REG_ADDR_BY_NAME(DT_NODELABEL(qspi), qspi_mm);

class QspiDevice final : public Device {
public:
  bool mapped = false;
  bool Read(uint32_t address, void *data, size_t size) override {
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
    if (squid_bench_count_reads) { ++squid_bench_personal_reads; squid_bench_personal_bytes += size; }
#endif
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
    if (address < REGION_START || address > REGION_END ||
        size > REGION_END - address || (address & 3) || (size & 3) ||
        (reinterpret_cast<uintptr_t>(data) & 3))
      return false;
    return nrfx_qspi_write(data, size, address) == NRFX_SUCCESS && squid_qspi_wait_ready(2000) == NRFX_SUCCESS;
  }
  bool Erase(uint32_t address) override {
    return address >= REGION_START && address < REGION_END &&
           !(address % 4096) &&
           nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_4KB, address) == NRFX_SUCCESS &&
           squid_qspi_wait_ready(2000) == NRFX_SUCCESS;
  }
  // One segment. A 64 KiB erase is typically 150 ms on this chip family and
  // can take 2 s, so it gets a longer wait than a sector erase.
  bool EraseBlock(uint32_t address) override {
    return address >= REGION_START && address < REGION_END &&
           !(address % SEG_BYTES) &&
           nrfx_qspi_erase(NRF_QSPI_ERASE_LEN_64KB, address) == NRFX_SUCCESS &&
           squid_qspi_wait_ready(4000) == NRFX_SUCCESS;
  }
} qspi_device;
Store store(qspi_device);
bool store_available = false;
bool exporting;
// Flash writes go through the QSPI peripheral instead of the memory-mapped
// window. Hold one of these, under the engine lock, around every store call
// that can program or erase.
class WriteMode {
public:
  WriteMode() : ok(squid_qspi_reactivate()) {
    if (ok)
      qspi_device.mapped = false;
  }
  ~WriteMode() {
    if (ok)
      qspi_device.mapped = true;
  }
  explicit operator bool() const { return ok; }

private:
  bool ok;
};
// Statistics, segment cleaning and table rebuilds run in slices on their own
// low-priority queue, never on the system work queue that delivers keys. Each
// slice takes the engine lock for one Step() and then lets keys in.
constexpr int BACKGROUND_DELAY_MS = 8;

void background_step(k_work *);
K_WORK_DELAYABLE_DEFINE(background_work, background_step);
void kick_background() {
  if (store_available)
    k_work_schedule_for_queue(&steno_squid_engine_queue, &background_work,
                              K_MSEC(BACKGROUND_DELAY_MS));
}
void background_step(k_work *) {
  bool more = false;
  {
    // False while the flash is asleep. The next edit or status request kicks
    // the work again.
    const JavelinEngineLock lock;
    if (lock && store_available && !exporting) {
      WriteMode write;
      more = write && store.Step();
    }
  }
  if (more)
    kick_background();
}
Entry make_key(const StenoStroke *strokes, size_t length) {
  Entry e{};
  if (length <= MAX_STROKES) {
    e.length = length;
    memcpy(e.strokes, strokes, length * 4);
  }
  return e;
}
// Lookup results are owned by the engine (CreateDynamicString), so a lookup
// hit must hand over its own allocation. Stored text has no terminator.
char *copy_text(const Entry &e) {
  char *text = static_cast<char *>(malloc(e.text_bytes + 1));
  if (!text)
    return nullptr;
  if (!store.ReadText(e, 0, text, e.text_bytes)) {
    free(text);
    return nullptr;
  }
  text[e.text_bytes] = '\0';
  return text;
}
// One buffer reused across a whole dictionary walk instead of an allocation
// per entry.
class TextBuffer {
public:
  ~TextBuffer() { free(data); }
  const char *Read(const Entry &e) {
    if (e.text_bytes + 1 > size) {
      free(data);
      size = 0;
      data = static_cast<char *>(malloc(e.text_bytes + 1));
      if (!data)
        return nullptr;
      size = e.text_bytes + 1;
    }
    if (!store.ReadText(e, 0, data, e.text_bytes))
      return nullptr;
    data[e.text_bytes] = '\0';
    return data;
  }

private:
  char *data = nullptr;
  uint32_t size = 0;
};
enum class Edit { ADD, REMOVE, RESET };
Store::Result edit(Edit kind, const Entry &e);
class QspiUserDictionary final : public StenoDictionary {
public:
  QspiUserDictionary() : StenoDictionary(0) {}
  void Refresh() {
    maximumOutlineLength = store.MaxStrokes();
    OnLookupDataChanged();
  }
  const char *GetName() const override { return "user_dictionary"; }
  StenoDictionaryLookupResult
  Lookup(const StenoDictionaryLookup &lookup) const override {
    const Entry key = make_key(lookup.strokes, lookup.length);
    Entry e{};
    if (!store_available || lookup.length > MAX_STROKES ||
        !store.Find(key.strokes, key.length, e))
      return StenoDictionaryLookupResult::CreateInvalid();
    char *text = copy_text(e);
    return text ? StenoDictionaryLookupResult::CreateDynamicString(text)
                : StenoDictionaryLookupResult::CreateInvalid();
  }
  const StenoDictionary *
  GetDictionaryForOutline(const StenoDictionaryLookup &lookup) const override {
    const Entry key = make_key(lookup.strokes, lookup.length);
    Entry e{};
    return store_available && lookup.length <= MAX_STROKES &&
                   store.Find(key.strokes, key.length, e)
               ? this
               : nullptr;
  }
  bool CanRemove() const override { return true; }
  bool Remove(const char *name, const StenoStroke *strokes,
              size_t length) override {
    return (!name || strcmp(name, GetName()) == 0) && length &&
           length <= MAX_STROKES &&
           edit(Edit::REMOVE, make_key(strokes, length)) == Store::Result::OK;
  }
  void ReverseLookup(StenoReverseDictionaryLookup &lookup) const override {
    if (!store_available)
      return;
    Store::ReverseCursor cursor;
    Entry e{};
    while (store.ReverseNext(cursor, lookup.definition,
                             lookup.definitionLength, e)) {
      if (e.length < lookup.ignoreStrokeThreshold) {
        StenoStroke strokes[MAX_STROKES];
        memcpy(strokes, e.strokes, e.length * 4);
        lookup.AddResult(strokes, e.length, this);
      }
    }
  }
  // Insertion order, not sorted.
  void PrintDictionary(PrintDictionaryContext &context) const override {
    if (context.HasName() && strcmp(context.GetName(), GetName()))
      return;
    struct ExportSession {
      ExportSession() { exporting = true; dictionary_flash_read_session(true); }
      ~ExportSession() { exporting = false; dictionary_flash_read_session(false); kick_background(); }
    } session;
    StenoSquidExportWriter writer(steno_squid_javelin_response_active());
    TextBuffer buffer;
    Store::Iterator it;
    Entry e{};
    while (store_available && !writer.Failed() && !steno_squid_javelin_response_cancelled() && store.Next(it, e)) {
      const char *text = buffer.Read(e);
      if (!text)
        return;
      StenoStroke strokes[MAX_STROKES];
      memcpy(strokes, e.strokes, e.length * 4);
      context.Print(strokes, e.length, text);
    }
  }
  void PrintEntriesWithPartialOutline(
      PrintPartialOutlineContext &context) const override {
    TextBuffer buffer;
    Store::Iterator it;
    Entry e{};
    while (store_available && !context.IsDone() && store.Next(it, e)) {
      if (e.length <= context.length)
        continue;
      StenoStroke strokes[MAX_STROKES];
      memcpy(strokes, e.strokes, e.length * 4);
      if (!StenoStroke::HasPartialOutline(strokes, context.strokes,
                                          context.length,
                                          e.length - context.length))
        continue;
      const char *text = buffer.Read(e);
      if (!text)
        return;
      context.Add(strokes, e.length, text, this);
    }
  }
  void PrintEntriesWithPrefix(PrintPrefixContext &context) const override {
    TextBuffer buffer;
    Store::Iterator it;
    Entry e{};
    while (store_available && !context.IsDone() && store.Next(it, e)) {
      if (e.text_bytes <= context.prefixLength)
        continue;
      const char *text = buffer.Read(e);
      if (!text)
        return;
      if (strncmp(text, context.prefix, context.prefixLength) == 0) {
        StenoStroke strokes[MAX_STROKES];
        memcpy(strokes, e.strokes, e.length * 4);
        context.Add(strokes, e.length, text, this);
      }
    }
  }
  void PrintInfo(int depth) const override {
    Console::Printf("%s%s\n%sFormat: QSPI segment log\n", Spaces(depth),
                    GetName(), Spaces(depth + 2));
    Console::Printf("%sEntries: %u\n%sFree segments: %u/%u\n",
                    Spaces(depth + 2), store.Entries(), Spaces(depth + 2),
                    store.FreeSegments(), SEG_COUNT);
  }
} dictionary;
// Every add, remove and reset goes through here, under the engine lock.
Store::Result edit(Edit kind, const Entry &e) {
  if (!store_available)
    return Store::Result::FAILED;
  Store::Result result;
  {
    WriteMode write;
    if (!write)
      return Store::Result::FAILED;
    result = kind == Edit::ADD      ? store.Add(e)
             : kind == Edit::REMOVE ? store.Remove(e.strokes, e.length)
                                    : store.Reset();
  }
  if (result == Store::Result::OK)
    dictionary.Refresh();
  // A full table is rebuilt in the background before a busy edit can succeed.
  kick_background();
  return result;
}
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
    steno_squid_user_dictionary_open();
  if (!store_available)
    Console::Printf("ERR Personal dictionary unavailable\n\n");
  return store_available;
}
void finish_edit(Store::Result result) {
  switch (result) {
  case Store::Result::OK:
  case Store::Result::UNCHANGED:
  case Store::Result::NOT_FOUND:
    Console::SendOk();
    break;
  case Store::Result::BUSY:
    Console::Printf("ERR Personal dictionary busy. Try again shortly\n\n");
    break;
  case Store::Result::FULL:
    Console::Printf("ERR Personal dictionary full\n\n");
    break;
  default:
    Console::Printf("ERR Personal dictionary save failed\n\n");
    break;
  }
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
  Entry e = make_key(parser.GetData(), parser.GetCount());
  e.text = parser.failureOrEnd;
  e.text_bytes = n;
  finish_edit(edit(Edit::ADD, e));
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
      edit(Edit::REMOVE, make_key(parser.GetData(), parser.GetCount())));
}
void reset_entries(void *, const char *) {
  if (can_edit())
    finish_edit(edit(Edit::RESET, Entry{}));
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

// user_dictionary_import begin <records> <forward> <reverse> [replace]
//                        data <segment_index> <offset> <hex, at most 512 bytes>
//                        finish | abort
// The host builds whole segments (tools/dictionaries/segment_image.py). Nothing
// changes until finish, unless begin had to reset to make room for `replace`.
void bulk_import(void *, const char *line) {
  if (!can_edit())
    return;
  constexpr char command[] = "user_dictionary_import ";
  if (strncmp(line, command, sizeof(command) - 1) != 0) {
    Console::Printf("ERR Missing import operation\n\n");
    return;
  }
  const char *args = line + sizeof(command) - 1;
  WriteMode write;
  if (!write) {
    steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, 0);
    Console::Printf("ERR QSPI activation failed\n\n");
    return;
  }
  if (strcmp(args, "abort") == 0) {
    store.ImportAbort();
    steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, 0);
    kick_background();
    Console::SendOk();
    return;
  }
  if (strncmp(args, "begin ", 6) == 0) {
    args += 5;
    uint32_t records, forward, reverse;
    bool replace = false;
    bool ok = read_number(args, records) && read_number(args, forward) &&
              read_number(args, reverse);
    if (ok && strcmp(args, " replace") == 0)
      replace = true;
    else
      ok = ok && !*args;
    if (!ok) {
      Console::Printf("ERR Invalid import request\n\n");
      return;
    }
    // A new import replaces one the host abandoned.
    store.ImportAbort();
    steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, 30000);
    Store::ImportPlan plan;
    const Store::Result result =
        store.ImportBegin(records, forward, reverse, replace, plan);
    // `replace` may have reset the dictionary even when the import cannot start.
    if (replace)
      dictionary.Refresh();
    if (result != Store::Result::OK) {
      steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, 0);
      kick_background();
      if (result == Store::Result::FULL)
        Console::Printf("ERR Not enough free space\n\n");
      else if (result == Store::Result::BUSY)
        Console::Printf("ERR Personal dictionary busy. Try again shortly\n\n");
      else
        Console::Printf("ERR Import could not start\n\n");
      return;
    }
    Console::Printf("{\"first_sequence\":%u,\"fwd_epoch\":%u,\"rev_epoch\":%u,",
                    plan.first_sequence, static_cast<uint32_t>(plan.fwd_epoch),
                    static_cast<uint32_t>(plan.rev_epoch));
    Console::Printf("\"segments\":[");
    for (uint32_t n = 0; n < plan.total; ++n)
      Console::Printf(n ? ",%u" : "%u",
                      static_cast<uint32_t>(plan.segments[n]));
    Console::Printf("]}\n\n");
    return;
  }
  bool ok = false;
  const bool finish = strcmp(args, "finish") == 0;
  if (strncmp(args, "data ", 5) == 0) {
    args += 4;
    uint32_t segment, offset;
    // Static: the command runs on the HID thread's 4 KiB stack, and always
    // under the engine lock.
    alignas(4) static uint8_t chunk[IMPORT_CHUNK];
    if (read_number(args, segment) && read_number(args, offset) &&
        *args == ' ') {
      ++args;
      const size_t length = strlen(args);
      bool valid = length && !(length & 1) && length <= 2 * sizeof(chunk);
      for (size_t i = 0; valid && i < length / 2; ++i) {
        const int hi = hex_digit(args[2 * i]);
        const int lo = hex_digit(args[2 * i + 1]);
        valid = hi >= 0 && lo >= 0;
        if (valid)
          chunk[i] = (hi << 4) | lo;
      }
      ok = valid && store.ImportChunk(segment, offset, chunk, length / 2);
    }
  } else if (finish) {
    steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, 1800000);
    // On failure the store has already dropped the uploaded segments.
    ok = store.ImportFinish();
    if (ok)
      dictionary.Refresh();
    kick_background();
  }
  steno_squid_upload_status(STENO_SQUID_MODE_IMPORT, ok && !finish ? 30000 : 0);
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
const char *store_state() {
  return store.Importing()    ? "importing"
         : store.Recovering() ? "recovering"
         : store.Rebuilding() ? "rebuilding"
         : store.Cleaning()   ? "cleaning"
                              : "ready";
}
void store_status(void *, const char *) {
  Console::Printf("{\"version\":3,\"available\":%s,",
                  store_available ? "true" : "false");
  if (store.StatsReady())
    Console::Printf("\"entries\":%u,", store.Entries());
  else
    Console::Printf("\"entries\":\"counting\",");
  Console::Printf("\"free_segments\":%u,\"record_segments\":%u,"
                  "\"dead_bytes\":%u,",
                  store.FreeSegments(), store.RecordSegments(),
                  store.DeadBytes());
  Console::Printf("\"fwd_slots_used\":%u,\"fwd_slots\":%u,"
                  "\"rev_slots_used\":%u,\"rev_slots\":%u,",
                  store.SlotsUsed(0), store.Slots(0), store.SlotsUsed(1),
                  store.Slots(1));
  Console::Printf("\"integrity_errors\":%u,\"next_sequence\":%u,"
                  "\"max_strokes\":%u,",
                  store.IntegrityErrors(), store.NextSequence(),
                  store.MaxStrokes());
  Console::Printf("\"state\":\"%s\",\"bulk_import\":2}\n\n", store_state());
  // Resume background work that stopped while the flash was asleep.
  kick_background();
}
// Reads every record. Programs nothing, so it can use the mapped window.
void store_verify(void *, const char *) {
  if (!can_edit())
    return;
  Store::VerifyReport report;
  steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 600000);
  const bool ok = store.Verify(report);
  steno_squid_upload_status(STENO_SQUID_MODE_VERIFY, 0);
  if (ok)
    Console::Printf("{\"live\":%u,\"bad_crc\":%u,\"unreachable\":%u}\n\n",
                    report.live, report.bad_crc, report.unreachable);
  else
    Console::Printf("ERR Verify could not read the flash\n\n");
}
void store_rebuild(void *, const char *) {
  if (!can_edit())
    return;
  store.RequestRebuild();
  kick_background();
  Console::SendOk();
}
} // namespace
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
StenoDictionary *steno_squid_user_dictionary_current() {
  return store_available ? &dictionary : nullptr;
}
#endif
StenoDictionary *steno_squid_user_dictionary_open() {
  {
    WriteMode write;
    if (!write)
      return nullptr;
    // A blank or old-format region is formatted here.
    store_available = store.Open();
  }
  dictionary.Refresh();
  if (store_available) {
    kick_background();
  }
  // Keep the wrapper in the collection so a later successful open needs no rebind.
  return &dictionary;
}
void steno_squid_user_dictionary_register(Console &console,
                                          StenoDictionary &root) {
  parent = &root;
  dictionary.SetParentRecursively(parent);
#define UPSTREAM_COMMAND(name)
#define ENGINE_COMMAND(name, help, handler)
#define USER_COMMAND(name, help, handler, usb_only) console.RegisterCommand(name, help, handler, nullptr);
#include "console_commands.def"
#undef UPSTREAM_COMMAND
#undef ENGINE_COMMAND
#undef USER_COMMAND
}
