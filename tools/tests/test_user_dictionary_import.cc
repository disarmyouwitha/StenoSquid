// Host harness for the personal dictionary bulk import: the real store
// (src/steno_squid/user_dictionary_log.h) on a fake NOR flash, driven over
// stdin and stdout by tools/dictionaries/test_import_personal.py, which sends
// it the images tools/dictionaries/segment_image.py builds.
//
// Build: c++ -std=c++17 -O2 -Wall -Wextra -Werror -I src/steno_squid
//            tools/tests/test_user_dictionary_import.cc -o test_import
// Run:   test_import <segments>     (one command per line, one reply per line)
//
// The import and status lines are the firmware's console commands with the
// same replies. The rest are test controls; keys and texts travel as hex.
#include "user_dictionary_log.h"

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

using namespace squid_user;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,             \
                   #condition);                                                \
      std::exit(1);                                                            \
    }                                                                          \
  } while (0)

// Enforces the NOR rules the firmware's device has: a program stays within
// one 256-byte page and may only clear bits; erases are whole aligned units.
struct Nor final : Device {
  explicit Nor(uint32_t segments) : bytes(segments * SEG_BYTES, 0xff) {}
  std::vector<uint8_t> bytes;
  uint64_t block_erases = 0;

  bool Read(uint32_t address, void *data, size_t size) override {
    CHECK(address + size <= bytes.size());
    memcpy(data, &bytes[address], size);
    return true;
  }
  bool Program(uint32_t address, const void *data, size_t size) override {
    CHECK(address + size <= bytes.size());
    CHECK((address & 255u) + size <= 256);
    CHECK(!(address & 3) && !(size & 3)); // The QSPI driver's alignment rule.
    const auto *in = static_cast<const uint8_t *>(data);
    for (size_t i = 0; i < size; ++i) {
      CHECK((bytes[address + i] & in[i]) == in[i]);
      bytes[address + i] = in[i];
    }
    return true;
  }
  bool Erase(uint32_t address) override { return Wipe(address, 4096); }
  bool EraseBlock(uint32_t address) override {
    ++block_erases;
    return Wipe(address, SEG_BYTES);
  }
  bool Wipe(uint32_t address, uint32_t size) {
    CHECK(address % size == 0 && address + size <= bytes.size());
    memset(&bytes[address], 0xff, size);
    return true;
  }
};

static std::string ToHex(const void *data, size_t size) {
  static const char digits[] = "0123456789abcdef";
  const auto *bytes = static_cast<const uint8_t *>(data);
  std::string out;
  for (size_t i = 0; i < size; ++i) {
    out += digits[bytes[i] >> 4];
    out += digits[bytes[i] & 15];
  }
  return out.empty() ? "-" : out;
}
static bool FromHex(const std::string &text, std::vector<uint8_t> &out) {
  out.clear();
  if (text == "-")
    return true;
  if (text.size() & 1)
    return false;
  for (size_t i = 0; i < text.size(); i += 2) {
    int value = 0;
    for (int j = 0; j < 2; ++j) {
      const char c = text[i + j];
      const int digit = c >= '0' && c <= '9'   ? c - '0'
                        : c >= 'a' && c <= 'f' ? c - 'a' + 10
                                               : -1;
      if (digit < 0)
        return false;
      value = value * 16 + digit;
    }
    out.push_back(static_cast<uint8_t>(value));
  }
  return true;
}
static bool ToKey(const std::string &hex, Entry &e) {
  std::vector<uint8_t> key;
  if (!FromHex(hex, key) || key.empty() || (key.size() & 3) ||
      key.size() > 4 * MAX_STROKES)
    return false;
  e.length = static_cast<uint32_t>(key.size() / 4);
  memcpy(e.strokes, key.data(), key.size());
  return true;
}
static void Settle(Store &store) {
  for (int i = 0; i < 2000000 && store.Step(); ++i) {
  }
  CHECK(!store.Step());
}
static const char *Reply(Store::Result result) {
  switch (result) {
  case Store::Result::OK:
  case Store::Result::UNCHANGED:
  case Store::Result::NOT_FOUND:
    return "OK";
  case Store::Result::BUSY:
    return "ERR Personal dictionary busy. Try again shortly";
  case Store::Result::FULL:
    return "ERR Personal dictionary full";
  default:
    return "ERR Personal dictionary save failed";
  }
}
static std::string Status(Store &store) {
  std::ostringstream out;
  out << "{\"version\":3,\"available\":true,\"entries\":";
  if (store.StatsReady())
    out << store.Entries();
  else
    out << "\"counting\"";
  out << ",\"free_segments\":" << store.FreeSegments()
      << ",\"record_segments\":" << store.RecordSegments()
      << ",\"dead_bytes\":" << store.DeadBytes()
      << ",\"fwd_slots_used\":" << store.SlotsUsed(0)
      << ",\"fwd_slots\":" << store.Slots(0)
      << ",\"rev_slots_used\":" << store.SlotsUsed(1)
      << ",\"rev_slots\":" << store.Slots(1)
      << ",\"integrity_errors\":" << store.IntegrityErrors()
      << ",\"next_sequence\":" << store.NextSequence()
      << ",\"max_strokes\":" << store.MaxStrokes() << ",\"state\":\""
      << (store.Importing()    ? "importing"
          : store.Recovering() ? "recovering"
          : store.Rebuilding() ? "rebuilding"
          : store.Cleaning()   ? "cleaning"
                               : "ready")
      << "\",\"bulk_import\":2}";
  return out.str();
}
static std::string Import(Store &store, std::istringstream &in) {
  static const char *const failed =
      "ERR Import failed or dictionary changed. Retry from the beginning.";
  std::string operation;
  in >> operation;
  if (operation == "abort") {
    store.ImportAbort();
    return "OK";
  }
  if (operation == "begin") {
    uint32_t records, forward, reverse;
    std::string option;
    if (!(in >> records >> forward >> reverse))
      return "ERR Invalid import request";
    const bool replace = (in >> option) && option == "replace";
    store.ImportAbort();
    Store::ImportPlan plan;
    const Store::Result result =
        store.ImportBegin(records, forward, reverse, replace, plan);
    if (result == Store::Result::FULL)
      return "ERR Not enough free space";
    if (result == Store::Result::BUSY)
      return Reply(result);
    if (result != Store::Result::OK)
      return "ERR Import could not start";
    std::ostringstream out;
    out << "{\"first_sequence\":" << plan.first_sequence
        << ",\"fwd_epoch\":" << plan.fwd_epoch
        << ",\"rev_epoch\":" << plan.rev_epoch << ",\"segments\":[";
    for (uint32_t n = 0; n < plan.total; ++n)
      out << (n ? "," : "") << static_cast<unsigned>(plan.segments[n]);
    out << "]}";
    return out.str();
  }
  if (operation == "data") {
    uint32_t segment, offset;
    std::string hex;
    std::vector<uint8_t> raw;
    if (!(in >> segment >> offset >> hex) || hex == "-" ||
        !FromHex(hex, raw) || raw.size() > IMPORT_CHUNK)
      return failed;
    // The firmware decodes into a word-aligned buffer.
    alignas(4) uint8_t chunk[IMPORT_CHUNK];
    memcpy(chunk, raw.data(), raw.size());
    return store.ImportChunk(segment, offset, chunk,
                             static_cast<uint32_t>(raw.size()))
               ? "OK"
               : failed;
  }
  if (operation == "finish") {
    const bool ok = store.ImportFinish();
    Settle(store);
    return ok ? "OK" : failed;
  }
  return failed;
}

int main(int argc, char **argv) {
  const uint32_t segments =
      argc > 1 ? static_cast<uint32_t>(std::atoi(argv[1])) : SEG_COUNT;
  CHECK(segments >= 8 && segments <= SEG_COUNT);
  Nor nor(segments);
  auto store = std::make_unique<Store>(nor, 0, segments);
  CHECK(store->Open());
  Settle(*store);
  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream in(line);
    std::string command, a, b;
    in >> command;
    std::string reply = "ERR Unknown command";
    if (command == "user_dictionary_status") {
      reply = Status(*store);
    } else if (command == "user_dictionary_import") {
      reply = Import(*store, in);
    } else if (command == "user_dictionary_verify") {
      Store::VerifyReport report;
      CHECK(store->Verify(report));
      reply = "{\"live\":" + std::to_string(report.live) + ",\"bad_crc\":" +
              std::to_string(report.bad_crc) + ",\"unreachable\":" +
              std::to_string(report.unreachable) + "}";
    } else if (command == "add" && (in >> a >> b)) {
      Entry e{};
      std::vector<uint8_t> text;
      if (ToKey(a, e) && FromHex(b, text) && text.size() <= MAX_TEXT) {
        e.text = reinterpret_cast<const char *>(text.data());
        e.text_bytes = static_cast<uint32_t>(text.size());
        if (!e.text)
          e.text = "";
        reply = Reply(store->Add(e));
        Settle(*store);
      }
    } else if (command == "find" && (in >> a)) {
      Entry key{}, e{};
      if (ToKey(a, key)) {
        reply = "MISSING";
        if (store->Find(key.strokes, key.length, e)) {
          std::string text(e.text_bytes, '\0');
          CHECK(store->ReadText(e, 0, text.data(), text.size()));
          reply = "TEXT " + ToHex(text.data(), text.size());
        }
      }
    } else if (command == "reverse" && (in >> a)) {
      // The outlines stored for one translation, as the engine asks for them.
      std::vector<uint8_t> text;
      if (FromHex(a, text)) {
        Store::ReverseCursor cursor;
        Entry e{};
        reply = "KEYS";
        while (store->ReverseNext(cursor,
                                  reinterpret_cast<const char *>(text.data()),
                                  static_cast<uint32_t>(text.size()), e))
          reply += " " + ToHex(e.strokes, 4 * e.length);
      }
    } else if (command == "export") {
      // Insertion order, as print_dictionary walks it.
      Store::Iterator it;
      Entry e{};
      while (store->Next(it, e)) {
        std::string text(e.text_bytes, '\0');
        CHECK(store->ReadText(e, 0, text.data(), text.size()));
        std::cout << ToHex(e.strokes, 4 * e.length) << ' '
                  << ToHex(text.data(), text.size()) << '\n';
      }
      reply = "END";
    } else if (command == "reopen") {
      // A power cycle: all RAM state is rebuilt from the flash.
      store = std::make_unique<Store>(nor, 0, segments);
      CHECK(store->Open());
      Settle(*store);
      reply = "OK";
    } else if (command == "erases") {
      reply = std::to_string(nor.block_erases);
    }
    std::cout << reply << std::endl;
  }
  return 0;
}
