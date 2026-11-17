// Host tests for the personal dictionary segment store
// (src/steno_squid/user_dictionary_log.h) against a fake NOR flash that
// enforces clear-only programming and can lose power mid-operation.
//
// Build: c++ -std=c++17 -O2 -Wall -Wextra -Werror -I src/steno_squid
//            tools/tests/test_user_dictionary_log.cc -o test_log && ./test_log
#include "user_dictionary_log.h"

#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <random>
#include <string>
#include <vector>

using namespace squid_user;
using Key = std::vector<uint32_t>;
using Model = std::map<Key, std::string>;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);         \
      std::exit(1);                                                            \
    }                                                                          \
  } while (0)

struct Nor final : Device {
  explicit Nor(uint32_t segments)
      : bytes(segments * SEG_BYTES, 0xff), erases(segments, 0) {}
  std::vector<uint8_t> bytes;
  std::vector<uint32_t> erases;
  uint64_t programs = 0, erase_calls = 0, reads = 0;
  long fuse = -1; // Writes left before power is lost; -1 = never.
  bool dead = false;
  std::mt19937 rng{12345};

  bool Trip() {
    if (dead)
      return true;
    if (fuse < 0)
      return false;
    if (fuse-- == 0)
      dead = true;
    return dead;
  }
  bool Read(uint32_t address, void *data, size_t size) override {
    if (dead)
      return false;
    CHECK(address + size <= bytes.size());
    ++reads;
    memcpy(data, &bytes[address], size);
    return true;
  }
  bool Program(uint32_t address, const void *data, size_t size) override {
    CHECK(address + size <= bytes.size());
    CHECK((address & 255u) + size <= 256);
    const auto *in = static_cast<const uint8_t *>(data);
    const bool torn = Trip();
    if (dead && !torn)
      return false;
    for (size_t i = 0; i < size; ++i) {
      uint8_t next = bytes[address + i] & in[i];
      if (!torn)
        CHECK(next == in[i]); // A program may only clear bits.
      else                    // Power lost: only some of the bits landed.
        next = bytes[address + i] & (in[i] | static_cast<uint8_t>(rng()));
      bytes[address + i] = next;
    }
    ++programs;
    return !torn;
  }
  bool Erase(uint32_t address) override { return Wipe(address, 4096); }
  bool EraseBlock(uint32_t address) override {
    ++erases[address / SEG_BYTES];
    return Wipe(address, SEG_BYTES);
  }
  bool Wipe(uint32_t address, uint32_t size) {
    CHECK(address % size == 0 && address + size <= bytes.size());
    const bool torn = Trip();
    if (dead && !torn)
      return false;
    for (uint32_t i = 0; i < size; ++i)
      bytes[address + i] =
          torn ? bytes[address + i] | static_cast<uint8_t>(rng()) : 0xff;
    ++erase_calls;
    return !torn;
  }
};

static Entry MakeEntry(const Key &key, const std::string &text) {
  Entry e{};
  e.length = static_cast<uint32_t>(key.size());
  for (size_t i = 0; i < key.size(); ++i)
    e.strokes[i] = key[i];
  e.text = text.data();
  e.text_bytes = static_cast<uint32_t>(text.size());
  return e;
}
static std::string TextOf(Store &store, const Entry &e) {
  std::string text(e.text_bytes, '\0');
  CHECK(store.ReadText(e, 0, text.data(), text.size()));
  return text;
}
static void Settle(Store &store) {
  // A slice may report "nothing to do" just before a refused step schedules
  // a rebuild, so require two idle answers in a row.
  for (int i = 0; i < 2000000; ++i)
    if (!store.Step() && !store.Step())
      return;
  std::printf("never settles: rebuilding=%d cleaning=%d free=%u dead=%u\n",
              store.Rebuilding(), store.Cleaning(), store.FreeSegments(),
              store.DeadBytes());
  CHECK(false);
}
static Model Dump(Store &store) {
  Model out;
  Store::Iterator it;
  Entry e{};
  while (store.Next(it, e)) {
    const Key key(e.strokes, e.strokes + e.length);
    CHECK(!out.count(key)); // One live record per outline.
    out[key] = TextOf(store, e);
  }
  return out;
}
static void CheckAgainst(Store &store, const Model &model) {
  CHECK(Dump(store) == model);
  for (const auto &[key, text] : model) {
    Entry e{};
    CHECK(store.Find(key.data(), static_cast<uint32_t>(key.size()), e));
    CHECK(TextOf(store, e) == text);
    Store::ReverseCursor cursor;
    bool found = false;
    uint32_t matches = 0, expected = 0;
    while (store.ReverseNext(cursor, text.data(),
                             static_cast<uint32_t>(text.size()), e)) {
      ++matches;
      found |= Key(e.strokes, e.strokes + e.length) == key;
    }
    for (const auto &other : model)
      expected += other.second == text;
    CHECK(found && matches == expected);
  }
  Store::VerifyReport report;
  CHECK(store.Verify(report));
  CHECK(report.live == model.size() && !report.bad_crc && !report.unreachable);
  if (store.StatsReady())
    CHECK(store.Entries() == model.size());
}

struct Workload {
  std::mt19937 rng;
  uint32_t keys, max_text;
  Workload(uint32_t seed, uint32_t keys, uint32_t max_text)
      : rng(seed), keys(keys), max_text(max_text) {}
  Key RandomKey() {
    const uint32_t id = rng() % keys;
    Key key(1 + id % 3);
    for (size_t i = 0; i < key.size(); ++i)
      key[i] = id * 2654435761u + static_cast<uint32_t>(i) * 97u + 1;
    return key;
  }
  std::string RandomText() {
    // A small pool, so several outlines share a translation.
    const uint32_t id = rng() % 64;
    std::string text(1 + (id * 37) % max_text, 'a');
    for (size_t i = 0; i < text.size(); ++i)
      text[i] = static_cast<char>('a' + (id + i * 7) % 26);
    return text;
  }
};

// One random operation, applied to both. Returns false if the store refused
// for lack of space (the model is then left unchanged).
static bool Apply(Store &store, Model &model, Workload &w, Model *before,
                  Model *after) {
  const Key key = w.RandomKey();
  const uint32_t roll = w.rng() % 100;
  if (before)
    *before = model;
  if (roll < 70) {
    const std::string text = w.RandomText();
    Model next = model;
    next[key] = text;
    if (after)
      *after = next;
    Store::Result r = store.Add(MakeEntry(key, text));
    if (r == Store::Result::BUSY) {
      Settle(store);
      r = store.Add(MakeEntry(key, text));
    }
    if (r == Store::Result::OK || r == Store::Result::UNCHANGED) {
      model = next;
      return true;
    }
    return false;
  }
  Model next = model;
  next.erase(key);
  if (after)
    *after = next;
  Store::Result r = store.Remove(key.data(), static_cast<uint32_t>(key.size()));
  if (r == Store::Result::BUSY) {
    Settle(store);
    r = store.Remove(key.data(), static_cast<uint32_t>(key.size()));
  }
  if (r == Store::Result::OK ||
      (r == Store::Result::NOT_FOUND && !model.count(key))) {
    model = next;
    return true;
  }
  return false;
}

static void TestBasics() {
  Nor nor(12);
  Store store(nor, 0, 12);
  CHECK(store.Open());
  Settle(store);
  const Key cat{1, 2}, dog{7};
  CHECK(store.Add(MakeEntry(cat, "cat")) == Store::Result::OK);
  CHECK(store.Add(MakeEntry(cat, "cat")) == Store::Result::UNCHANGED);
  CHECK(store.Add(MakeEntry(dog, "cat")) == Store::Result::OK);
  CHECK(store.Add(MakeEntry(cat, "kitten")) == Store::Result::OK);
  Model model{{cat, "kitten"}, {dog, "cat"}};
  CheckAgainst(store, model);
  CHECK(store.MaxStrokes() == 2);
  CHECK(store.Remove(dog.data(), 1) == Store::Result::OK);
  CHECK(store.Remove(dog.data(), 1) == Store::Result::NOT_FOUND);
  model.erase(dog);
  Store reopened(nor, 0, 12);
  CHECK(reopened.Open());
  Settle(reopened);
  CheckAgainst(reopened, model);
  CHECK(reopened.Reset() == Store::Result::OK);
  CheckAgainst(reopened, {});
  Store again(nor, 0, 12);
  CHECK(again.Open());
  CheckAgainst(again, {});
  // Limits.
  Key longest(MAX_STROKES, 5);
  const std::string big(MAX_TEXT, 'x');
  CHECK(again.Add(MakeEntry(longest, big)) == Store::Result::OK);
  CheckAgainst(again, {{longest, big}});
  std::printf("basics ok\n");
}

// Random operations in a small region, so cleaning and rebuilds run often.
static void TestModel(uint32_t seed, int operations, bool background = true) {
  Nor nor(12);
  auto store = std::make_unique<Store>(nor, 0, 12);
  CHECK(store->Open());
  Model model;
  Workload w(seed, 600, 1500);
  int refused = 0;
  for (int i = 0; i < operations; ++i) {
    if (!Apply(*store, model, w, nullptr, nullptr))
      ++refused;
    if (background && i % 7 == 0)
      store->Step();
    if (i % 5000 == 4999)
      store->RequestRebuild();
    if (i % 2500 == 2499) {
      CheckAgainst(*store, model);
      store = std::make_unique<Store>(nor, 0, 12);
      CHECK(store->Open());
      CheckAgainst(*store, model);
    }
  }
  Settle(*store);
  CheckAgainst(*store, model);
  uint64_t total = 0;
  uint32_t most = 0;
  for (uint32_t count : nor.erases) {
    total += count;
    most = count > most ? count : most;
  }
  CHECK(most <= 3 * (total / nor.erases.size() + 1)); // Even wear.
  CHECK(refused * 50 < operations); // Space was there to be reclaimed.
  std::printf("model seed %u ok: %d ops, %d refused, %zu live, %llu erases\n",
              seed, operations, refused, model.size(),
              static_cast<unsigned long long>(total));
}

// Cuts power after every individual write of an operation in turn.
static void TestPowerFail(uint32_t seed, int warmup, int cases) {
  Nor base(12);
  Model model;
  Workload w(seed, 300, 1200);
  {
    Store store(base, 0, 12);
    CHECK(store.Open());
    for (int i = 0; i < warmup; ++i) {
      Apply(store, model, w, nullptr, nullptr);
      if (i % 11 == 0)
        store.Step();
    }
    CheckAgainst(store, model);
  }
  long trials = 0;
  for (int c = 0; c < cases; ++c) {
    const std::mt19937 saved_rng = w.rng;
    const int kind = c % 4; // 0,1: add/remove  2: background step  3: reset
    Model committed = model;
    for (long fuse = 0;; ++fuse) {
      Nor nor = base;
      nor.fuse = fuse;
      nor.rng.seed(static_cast<uint32_t>(seed * 1000003u + c * 131u + fuse));
      w.rng = saved_rng;
      Model before = model, after = model, scratch = model;
      {
        Store store(nor, 0, 12);
        if (store.Open()) {
          if (kind == 3) {
            after.clear();
            store.Reset();
          } else if (kind == 2) {
            if (c % 8 == 2)
              store.RequestRebuild();
            for (int i = 0; i < 40 && !nor.dead; ++i)
              store.Step();
          } else {
            Apply(store, scratch, w, &before, &after);
          }
        }
      }
      const bool tripped = nor.dead;
      nor.dead = false;
      nor.fuse = -1;
      Store store(nor, 0, 12);
      CHECK(store.Open());
      const Model now = Dump(store);
      CHECK(now == before || now == after);
      Settle(store);
      CheckAgainst(store, now);
      // The store must still accept writes.
      const Key probe{0xabcdef01u};
      Store::Result r = store.Add(MakeEntry(probe, "probe"));
      if (r == Store::Result::BUSY) {
        Settle(store);
        r = store.Add(MakeEntry(probe, "probe"));
      }
      CHECK(r == Store::Result::OK || r == Store::Result::FULL);
      ++trials;
      if (!tripped) { // The operation ran to completion: adopt its result.
        committed = now;
        base = nor;
        Store keep(base, 0, 12);
        CHECK(keep.Open());
        keep.Remove(probe.data(), 1);
        break;
      }
    }
    model = committed;
    model.erase(Key{0xabcdef01u});
  }
  std::printf("power-fail seed %u ok: %d operations, %ld cut points\n", seed,
              cases, trials);
}

static void TestRecovery() {
  Nor nor(12);
  Model model;
  Workload w(99, 300, 400);
  {
    Store store(nor, 0, 12);
    CHECK(store.Open());
    for (int i = 0; i < 3000; ++i)
      Apply(store, model, w, nullptr, nullptr);
    Settle(store);
    CheckAgainst(store, model);
  }
  for (uint8_t kind : {META, FORWARD, REVERSE}) {
    Nor damaged = nor;
    for (uint32_t s = 0; s < 12; ++s)
      if (damaged.bytes[s * SEG_BYTES + 4] == kind &&
          damaged.bytes[s * SEG_BYTES] == 0x5a)
        for (uint32_t i = 0; i < SEG_BYTES; ++i)
          damaged.bytes[s * SEG_BYTES + i] = static_cast<uint8_t>(i * 31 + 7);
    Store store(damaged, 0, 12);
    CHECK(store.Open());
    Settle(store);
    CheckAgainst(store, model);
    CHECK(store.Add(MakeEntry(Key{42}, "after")) == Store::Result::OK);
  }
  std::printf("recovery ok\n");
}

// Costs must not grow with the size of the dictionary.
static void TestConstantCost(uint32_t entries) {
  Nor nor(SEG_COUNT);
  Store store(nor, 0, SEG_COUNT);
  CHECK(store.Open());
  const std::string text = "translation.";
  uint32_t added = 0;
  for (uint32_t i = 0; i < entries; ++i) {
    const Key key{i * 2654435761u + 1, i + 1};
    Store::Result r = store.Add(MakeEntry(key, text));
    if (r == Store::Result::BUSY) {
      Settle(store);
      r = store.Add(MakeEntry(key, text));
    }
    CHECK(r == Store::Result::OK);
    ++added;
    if (i % 64 == 0)
      store.Step();
  }
  Settle(store);
  CHECK(store.Entries() == added);
  uint64_t worst_add = 0, worst_remove = 0;
  for (uint32_t i = 0; i < 200; ++i) {
    const Key key{0x80000000u + i, 77};
    const uint64_t p0 = nor.programs, e0 = nor.erase_calls;
    Store::Result r = store.Add(MakeEntry(key, text));
    if (r == Store::Result::BUSY) {
      Settle(store);
      continue;
    }
    CHECK(r == Store::Result::OK);
    if (nor.erase_calls == e0 && nor.programs - p0 > worst_add)
      worst_add = nor.programs - p0;
    const uint64_t p1 = nor.programs;
    CHECK(store.Remove(key.data(), 2) == Store::Result::OK);
    if (nor.programs - p1 > worst_remove)
      worst_remove = nor.programs - p1;
  }
  CHECK(worst_add <= 6 && worst_remove == 1);
  Store reopened(nor, 0, SEG_COUNT);
  const uint64_t r0 = nor.reads;
  CHECK(reopened.Open());
  const uint64_t open_reads = nor.reads - r0;
  CHECK(open_reads < 20000);
  Entry e{};
  const Key first{1, 1};
  CHECK(reopened.Find(first.data(), 2, e));
  std::printf("constant cost ok: %u entries, add <= %llu programs, remove %llu,"
              " open %llu reads, %u free segments\n",
              added, static_cast<unsigned long long>(worst_add),
              static_cast<unsigned long long>(worst_remove),
              static_cast<unsigned long long>(open_reads),
              reopened.FreeSegments());
}

int main(int argc, char **argv) {
  const bool quick = argc > 1 && std::string(argv[1]) == "--quick";
  if (argc > 1 && std::string(argv[1]) == "--capacity") { // Just the big fill.
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    TestConstantCost(190000);
    std::printf("PASS\n");
    return 0;
  }
  TestBasics();
  TestModel(1, quick ? 20000 : 150000);
  TestModel(2, quick ? 20000 : 150000);
  TestModel(5, quick ? 20000 : 150000, false);
  TestPowerFail(3, 2500, quick ? 40 : 240);
  TestPowerFail(4, 6000, quick ? 40 : 240);
  TestRecovery();
  TestConstantCost(quick ? 20000 : 190000);
  std::printf("PASS\n");
  return 0;
}
