#include "../../src/steno_squid/user_dictionary_snapshot.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <string>
#include <map>
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
    // Saves and imports must not hand the NOR driver a cross-page write.
    assert(n <= 256 && (a & 255u) + n <= 256);
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
// Python supplies a compiled snapshot body and its four metadata words.
static void test_host_import(const char *path) {
  FILE *file = fopen(path, "rb");
  assert(file);
  uint32_t count, size, maximum, crc;
  assert(fread(&count, 4, 1, file) == 1 && fread(&size, 4, 1, file) == 1 &&
         fread(&maximum, 4, 1, file) == 1 && fread(&crc, 4, 1, file) == 1);
  std::vector<uint8_t> body(size);
  assert(fread(body.data(), 1, size, file) == size);
  fclose(file);

  Rows original;
  original.Add({8}, "old");
  original.Prepare();
  Flash flash;
  Store store(flash);
  assert(store.Save(original));
  const auto baseline = flash.bytes;
  auto transfer = [&](Store &target, uint32_t checksum) {
    if (!target.BeginImport(target.Generation(), count, size, maximum,
                            checksum))
      return false;
    for (uint32_t offset = 0; offset < size; offset += 256)
      if (!target.ImportChunk(offset, body.data() + offset,
                              std::min(256u, size - offset)))
        return false;
    return target.FinishImport();
  };
  assert(!store.BeginImport(store.Generation() - 1, count, size, maximum, crc));
  assert(!transfer(store, crc ^ 1) && lookup(store, key(8)) == "old");
  flash.mutations = 0;
  assert(transfer(store, crc));
  const int operations = flash.mutations;
  assert(store.Count() == count && lookup(store, key(8)) == "new");
  Entry multi{};
  multi.length = 2;
  multi.strokes[0] = 8;
  multi.strokes[1] = 16;
  assert(lookup(store, multi) == "ça va");

  for (int failure = 0; failure < operations; ++failure) {
    flash.bytes = baseline;
    flash.mutations = 0;
    flash.fail = failure;
    Store before(flash);
    assert(before.Open() && !transfer(before, crc));
    Store reboot(flash);
    assert(reboot.Open() && lookup(reboot, key(8)) == "old");
  }
  flash.fail = -1;
  Store reboot(flash);
  assert(reboot.Open());
  assert(reboot.BeginImport(reboot.Generation(), count, size, maximum, crc));
  assert(!reboot.ImportChunk(256, body.data(), 256));
  assert(!reboot.FinishImport());
  assert(reboot.Save(original)); // An ordinary edit cancels the staged import.
  assert(!reboot.ImportChunk(0, body.data(), 256));
  printf("PASS: Python snapshot import, stale generation, invalid CRC, and %d "
         "interrupted writes\n",
         operations);
}

static uint32_t slot_base(const Flash &flash, bool newer) {
  Header h0{}, h1{};
  memcpy(&h0, flash.bytes.data() + SLOT_START, sizeof(h0));
  memcpy(&h1, flash.bytes.data() + SLOT_START + SLOT_BYTES, sizeof(h1));
  // An erased slot has no magic and a generation of 0xffffffff: it is never
  // the newer one.
  const bool valid0 = h0.magic == MAGIC, valid1 = h1.magic == MAGIC;
  const bool second_is_newer = valid0 && valid1 ? h1.generation > h0.generation : valid1;
  return second_is_newer == newer ? SLOT_START + SLOT_BYTES : SLOT_START;
}

// Open validates only the slot it serves, and falls back only on failure.
static void test_open_slots() {
  Flash flash;
  Store writer(flash);
  Rows rows;
  rows.Add({8}, "cat");
  rows.Add({2, 4}, std::string(2047, 'x'));
  rows.Prepare();
  assert(writer.Save(rows) && writer.Save(rows)); // Both slots are valid.
  const auto both = flash.bytes;
  const uint32_t newer = slot_base(flash, true), older = slot_base(flash, false);
  Header newer_header{};
  memcpy(&newer_header, flash.bytes.data() + newer, sizeof(newer_header));

  flash.reads = 0;
  Store normal(flash);
  assert(normal.Open() && normal.Generation() == newer_header.generation);
  assert(normal.ValidCopies() == 1);
  const size_t two_valid_reads = flash.reads;
  // The same cost with the older slot erased proves it was never validated.
  memset(flash.bytes.data() + older, 255, SECTOR);
  flash.reads = 0;
  Store one(flash);
  assert(one.Open() && flash.reads == two_valid_reads);

  // A bad body in the older slot is not even looked at.
  flash.bytes = both;
  flash.bytes[older + SECTOR] ^= 1;
  Store unseen(flash);
  assert(unseen.Open() && unseen.Generation() == newer_header.generation);

  // A bad newer body, or header, falls back to the older snapshot.
  flash.bytes = both;
  flash.bytes[newer + SECTOR] ^= 1;
  Store fallback(flash);
  assert(fallback.Open() &&
         fallback.Generation() == newer_header.generation - 1 &&
         fallback.ValidCopies() == 1 && lookup(fallback, key(8)) == "cat");
  flash.bytes = both;
  flash.bytes[newer + 4] ^= 1; // Header version field.
  Store bad_header(flash);
  assert(bad_header.Open() &&
         bad_header.Generation() == newer_header.generation - 1);

  // Both bad: nothing is served.
  flash.bytes = both;
  flash.bytes[newer + SECTOR] ^= 1;
  flash.bytes[older + SECTOR] ^= 1;
  Store none(flash);
  Entry sink{};
  assert(!none.Open() && none.Count() == 0 && !none.Find(key(8), sink));
}

// Forward lookups must not rehash translation text; only validation does.
static void test_get_does_not_hash() {
  Flash flash;
  Store store(flash);
  Rows rows;
  rows.Add({8}, "cat");
  rows.Add({2, 4}, std::string(2047, 'x'));
  rows.Prepare();
  assert(store.Save(rows));
  Entry e{};
  for (uint32_t i = 0; i < store.Count(); ++i) {
    flash.reads = 0;
    assert(store.Get(i, false, e) && e.text_hash == 0);
    assert(flash.reads < 128); // Index + record + strokes, never the text.
  }
  Entry multi{};
  multi.length = 2;
  multi.strokes[0] = 2;
  multi.strokes[1] = 4;
  flash.reads = 0;
  assert(store.Find(multi, e) && e.text_bytes == 2048 && e.text_hash == 0);
  assert(flash.reads < 256);
  // The reverse index supplies the hash without reading text.
  flash.reads = 0;
  assert(store.Get(store.ReverseStart(hash_bytes("cat", 3)), true, e) &&
         e.text_hash == hash_bytes("cat", 3) && flash.reads < 128);
}

// Save: the stored CRC describes the bytes actually in flash, and a flash that
// silently drops bits is rejected before anything is published.
static void test_save_integrity() {
  Flash flash;
  Store store(flash);
  Rows rows;
  rows.Add({8}, "cat");
  rows.Add({2, 4}, "\xe7" "a va");
  rows.Prepare();
  assert(store.Save(rows));
  Header h{};
  const uint32_t base = slot_base(flash, true);
  memcpy(&h, flash.bytes.data() + base, sizeof(h));
  assert(h.body_crc ==
         crc32(0, flash.bytes.data() + base + SECTOR, h.used - SECTOR));
  assert(h.header_crc == crc32(0, &h, offsetof(Header, header_crc)));
  const uint32_t generation = store.Generation();
  flash.silent_corruption = true;
  assert(!store.Save(rows) && store.Generation() == generation);
  flash.silent_corruption = false;
  Store reboot(flash);
  assert(reboot.Open() && reboot.Generation() == generation);
}

static void test_page_edits() {
  Flash flash;
  Store store(flash);
  Rows initial;
  initial.Add({1}, "original");
  initial.Prepare();
  assert(store.Save(initial));
  std::map<uint32_t, std::string> expected{{1, "original"}};
  uint32_t random = 1234567;
  for (unsigned step = 0; step < 150; ++step) {
    random = random * 1664525u + 1013904223u;
    const uint32_t stroke = random % 37 + 1;
    const bool erase = step % 4 == 0;
    std::string text(random % 2047 + 1, char('a' + step % 26));
    Entry entry = key(stroke, text.c_str());
    EditSource edit(store, entry, erase ? nullptr : &entry);
    assert(edit.Prepare() && store.Save(edit));
    if (erase) expected.erase(stroke);
    else expected[stroke] = text;
    Store reboot(flash);
    assert(reboot.Open() && reboot.Count() == expected.size());
    for (const auto &row : expected) assert(lookup(reboot, key(row.first)) == row.second);
  }
  // Guard the intended controller-call saving on a larger personal edit.
  Rows rows;
  for (uint32_t i = 1; i <= 1000; ++i) rows.Add({i}, "personal word");
  rows.Prepare();
  assert(store.Save(rows));
  Entry replacement = key(500, "changed word");
  EditSource edit(store, replacement, &replacement);
  flash.mutations = 0;
  flash.reads = 0;
  assert(edit.Prepare() && store.Save(edit));
  assert(flash.mutations < 250); // old path needed more than 5,000
  assert(flash.reads < 400000); // relocation avoids rebuilding reverse offsets by search
  // A source's relocation hint is never trusted instead of final validation.
  struct BadOffsets : Rows {
    bool RecordOffset(const Entry &, uint32_t &offset) const override {
      offset = 0;
      return true;
    }
  } bad;
  bad.Add({1}, "one"); bad.Add({2}, "two"); bad.Prepare();
  const uint32_t generation = store.Generation();
  assert(!store.Save(bad) && store.Generation() == generation);
  Store recovered(flash);
  assert(recovered.Open() && recovered.Count() == 1000);
  printf("PASS: variable-length page edits, reboot validation, batching budget and untrusted offsets\n");
}

int main(int argc, char **argv) {
  if (argc == 2)
    test_host_import(argv[1]);
  test_page_edits();
  test_open_slots();
  test_get_does_not_hash();
  test_save_integrity();
  // On the host crc32 is the portable routine; the device build swaps in
  // Zephyr's crc32_ieee_update, which must give these same values.
  assert(crc32_portable(0, "123456789", 9) == 0xcbf43926);
  assert(crc32(0, "123456789", 9) == 0xcbf43926);
  assert(crc32_portable(0, "", 0) == 0 &&
         crc32_portable(0, "a", 1) == 0xe8b7be43);
  assert(crc32_portable(crc32_portable(0, "1234", 4), "56789", 5) ==
         0xcbf43926);
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
  flash.bytes = good;
  memcpy(&bad, flash.bytes.data() + newest, sizeof(bad));
  Index wrong_hash{};
  const uint32_t reverse_start = newest + SECTOR + bad.count * 8;
  memcpy(&wrong_hash, flash.bytes.data() + reverse_start, sizeof(wrong_hash));
  wrong_hash.hash--; // remains sorted before the other equal-text entries
  memcpy(flash.bytes.data() + reverse_start, &wrong_hash, sizeof(wrong_hash));
  bad.body_crc = crc32(0, flash.bytes.data() + newest + SECTOR, bad.used - SECTOR);
  bad.header_crc = crc32(0, &bad, offsetof(Header, header_crc));
  memcpy(flash.bytes.data() + newest, &bad, sizeof(bad));
  Store bad_reverse(flash);
  assert(bad_reverse.Open() && bad_reverse.Count() == 0);
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
