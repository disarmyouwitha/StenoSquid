#include "../../src/steno_squid/user_dictionary_snapshot.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
using namespace squid_user;
struct Flash : Device {
  std::vector<uint8_t> bytes = std::vector<uint8_t>(16 * 1024 * 1024, 255);
  int mutations = 0, fail = -1;
  bool silent_corruption = false;
  size_t erased = 0, written = 0, reads = 0;
  bool Read(uint32_t a, void *p, size_t n) override {
    assert(a + n <= bytes.size());
    memcpy(p, bytes.data() + a, n);
    reads += n;
    return true;
  }
  bool Program(uint32_t a, const void *p, size_t n) override {
    assert(a >= SLOT_START && a + n <= SLOT_START + 2 * SLOT_BYTES &&
           !(a & 3) && !(n & 3));
    bool failure = mutations++ == fail;
    size_t actual = failure ? n / 2 : n;
    const auto *b = (const uint8_t *)p;
    for (size_t i = 0; i < actual; ++i) {
      assert((bytes[a + i] & b[i]) == b[i]);
      bytes[a + i] &= b[i];
    }
    if (silent_corruption) {
      for (size_t i = 0; i < actual; ++i)
        if (bytes[a + i]) {
          bytes[a + i] &= bytes[a + i] - 1;
          break;
        }
    }
    written += actual;
    return !failure;
  }
  bool Erase(uint32_t a) override {
    assert(a >= SLOT_START && a + SECTOR <= SLOT_START + 2 * SLOT_BYTES &&
           !(a % SECTOR));
    bool failure = mutations++ == fail;
    memset(bytes.data() + a, 255, failure ? SECTOR / 2 : SECTOR);
    erased += failure ? SECTOR / 2 : SECTOR;
    return !failure;
  }
};
struct Rows : Source {
  std::vector<std::vector<uint32_t>> keys;
  std::vector<std::string> texts;
  std::vector<Entry> entries;
  std::vector<uint32_t> forward, reverse;
  void Add(std::vector<uint32_t> key, std::string text) {
    keys.push_back(key);
    texts.push_back(text);
  }
  void Prepare() {
    entries.clear();
    forward.clear();
    reverse.clear();
    for (size_t i = 0; i < keys.size(); ++i) {
      Entry e{};
      e.length = keys[i].size();
      memcpy(e.strokes, keys[i].data(), e.length * 4);
      e.text = texts[i].c_str();
      e.text_bytes = texts[i].size() + 1;
      e.text_hash = hash_bytes(e.text, e.text_bytes - 1);
      entries.push_back(e);
      forward.push_back(i);
      reverse.push_back(i);
    }
    std::sort(forward.begin(), forward.end(), [&](auto a, auto b) {
      return compare(entries[a], entries[b], false) < 0;
    });
    std::sort(reverse.begin(), reverse.end(), [&](auto a, auto b) {
      return compare(entries[a], entries[b], true) < 0;
    });
  }
  uint32_t Count() const override { return entries.size(); }
  bool Get(uint32_t i, bool rev, Entry &e) override {
    if (i >= Count())
      return false;
    e = entries[(rev ? reverse : forward)[i]];
    return true;
  }
};
static Entry key(uint32_t k, const char *text = nullptr) {
  Entry e{};
  e.length = 1;
  e.strokes[0] = k;
  e.text = text;
  if (text) {
    e.text_bytes = strlen(text) + 1;
    e.text_hash = hash_bytes(text, e.text_bytes - 1);
  }
  return e;
}
static std::string lookup(Store &s, Entry k) {
  Entry found{};
  if (!s.Find(k, found))
    return "<missing>";
  std::string text(found.text_bytes, '\0');
  assert(s.ReadText(found, 0, text.data(), text.size()));
  text.pop_back();
  return text;
}
int main() {
  assert(crc32(0, "123456789", 9) == 0xcbf43926);
  Flash flash;
  Store store(flash);
  assert(!store.Open());
  Rows source;
  source.Add({8}, "cat");
  source.Add({2, 4}, "ça va");
  source.Add({7}, "cat");
  source.Add({9}, "{#Control_L(a)}");
  source.Prepare();
  assert(store.Save(source));
  assert(store.Count() == 4 && store.Maximum() == 2);
  assert(flash.erased == 2 * SECTOR);
  assert(store.Used() < SECTOR + 256);
  assert(lookup(store, key(8)) == "cat");
  Entry multi{};
  multi.length = 2;
  multi.strokes[0] = 2;
  multi.strokes[1] = 4;
  assert(lookup(store, multi) == "ça va");
  uint32_t start = store.ReverseStart(hash_bytes("cat", 3));
  Entry reverse{};
  assert(store.Get(start, true, reverse) &&
         reverse.text_hash == hash_bytes("cat", 3));
  assert(store.Get(start + 1, true, reverse) &&
         reverse.text_hash == hash_bytes("cat", 3));
  assert(store.Save(source)); // Both slots now contain valid (dirty) snapshots.
  const auto baseline = flash.bytes;
  Entry replacement = key(8, "Bob");
  EditSource edit(store, replacement, &replacement);
  assert(edit.Prepare() && edit.Exists());
  flash.mutations = 0;
  assert(store.Save(edit));
  int operations = flash.mutations;
  assert(lookup(store, key(8)) == "Bob" && store.Count() == 4);
  for (int fail = 0; fail < operations; ++fail) {
    flash.bytes = baseline;
    flash.fail = -1;
    Store before(flash);
    assert(before.Open());
    EditSource update(before, replacement, &replacement);
    assert(update.Prepare());
    flash.mutations = 0;
    flash.fail = fail;
    assert(!before.Save(update));
    assert(lookup(before, key(8)) == "cat");
    flash.fail = -1;
    Store reboot(flash);
    assert(reboot.Open());
    assert(lookup(reboot, key(8)) == "cat");
  }
  flash.bytes = baseline;
  flash.fail = -1;
  Store silent(flash);
  assert(silent.Open());
  EditSource broken_write(silent, replacement, &replacement);
  assert(broken_write.Prepare());
  flash.silent_corruption = true;
  assert(!silent.Save(broken_write));
  flash.silent_corruption = false;
  assert(lookup(silent, key(8)) == "cat");
  flash.bytes = baseline;
  flash.fail = -1;
  Store current(flash);
  assert(current.Open());
  // Repeated replacements compact old text instead of consuming extra space.
  for (int i = 0; i < 40; ++i) {
    EditSource update(current, replacement, &replacement);
    assert(update.Prepare() && current.Save(update));
  }
  uint32_t compact = current.Used();
  assert(compact < SECTOR + 256);
  EditSource remove(current, replacement, nullptr);
  assert(remove.Prepare() && remove.Exists() && current.Save(remove));
  assert(lookup(current, key(8)) == "<missing>" && current.Count() == 3);
  Entry newrow = key(3, "new");
  EditSource add(current, newrow, &newrow);
  assert(add.Prepare() && current.Save(add));
  assert(lookup(current, key(3)) == "new" && current.Count() == 4);
  EditSource clear(current, Entry{}, nullptr, true);
  assert(clear.Prepare() && current.Save(clear));
  Store empty(flash);
  assert(empty.Open() && empty.Count() == 0 && empty.Maximum() == 0);
  // Whole-chip legacy region must never be changed by the new writer.
  assert(std::equal(flash.bytes.begin() + 0xf00000, flash.bytes.end(),
                    baseline.begin() + 0xf00000));
  Rows large;
  for (uint32_t i = 1; i <= 90000; ++i)
    large.Add({i}, "word");
  large.Prepare();
  assert(empty.Save(large));
  assert(empty.Count() == 90000);
  assert(lookup(empty, key(11999)) == "word");
  auto good = flash.bytes;
  uint32_t generation = empty.Generation();
  Rows too_large;
  for (uint32_t i = 1; i <= (SLOT_BYTES - SECTOR) / 36 + 1; ++i)
    too_large.Add({i}, "word");
  too_large.Prepare();
  flash.mutations = 0;
  assert(!empty.Save(too_large));
  assert(flash.mutations == 0 && empty.Generation() == generation);
  // Corrupt newest body: boot must select the preceding complete snapshot.
  Header h0{}, h1{};
  memcpy(&h0, flash.bytes.data() + SLOT_START, sizeof(h0));
  memcpy(&h1, flash.bytes.data() + SLOT_START + SLOT_BYTES, sizeof(h1));
  uint32_t newest =
      h0.generation > h1.generation ? SLOT_START : SLOT_START + SLOT_BYTES;
  flash.bytes[newest + SECTOR] ^= 1;
  Store recovery(flash);
  assert(recovery.Open() && recovery.Count() == 0);
  flash.bytes = good;
  // CRC-valid malicious offsets must still be rejected structurally.
  Header bad{};
  memcpy(&bad, flash.bytes.data() + newest, sizeof(bad));
  Index invalid{0, 0xfffffffcu};
  memcpy(flash.bytes.data() + newest + SECTOR, &invalid, sizeof(invalid));
  bad.body_crc =
      crc32(0, flash.bytes.data() + newest + SECTOR, bad.used - SECTOR);
  bad.header_crc = crc32(0, &bad, offsetof(Header, header_crc));
  memcpy(flash.bytes.data() + newest, &bad, sizeof(bad));
  Store corrupt(flash);
  assert(corrupt.Open() && corrupt.Count() == 0);
  // Maximum outline and browser-sized translation survive without a full RAM
  // mirror.
  Rows limits;
  limits.Add(std::vector<uint32_t>(16, 123), std::string(2047, 'x'));
  limits.Prepare();
  Store limit_store(flash);
  assert(limit_store.Save(limits));
  Entry longest = limits.entries[0];
  assert(lookup(limit_store, longest) == std::string(2047, 'x'));
  Entry oversized = key(13);
  std::string huge(2048, 'y');
  oversized.text = huge.c_str();
  oversized.text_bytes = huge.size() + 1;
  EditSource invalid_edit(limit_store, oversized, &oversized);
  assert(invalid_edit.Prepare());
  flash.mutations = 0;
  assert(!limit_store.Save(invalid_edit) && flash.mutations == 0);
  printf("PASS: CRUD, UTF-8, multi-stroke, reverse duplicates, compaction, %d "
         "interrupted-save points, 90,000 entries, capacity, corruption "
         "rollback\n",
         operations);
}
