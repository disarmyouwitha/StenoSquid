#pragma once
// Pointer-free little-endian snapshots; serialized externally by the engine
// lock.
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#ifdef __ZEPHYR__
#include <zephyr/sys/crc.h>
#endif
namespace squid_user {
constexpr uint32_t SLOT_START = 0x600000, SLOT_BYTES = 4 * 1024 * 1024,
                   SECTOR = 4096;
constexpr uint32_t MAX_STROKES = 16, MAX_TEXT = 2047, MAGIC = 0x3255445a,
                   VERSION = 2;
struct Header {
  uint32_t magic, version, generation, count, used, maximum, body_crc,
      header_crc;
};
struct Index {
  uint32_t hash, offset;
};
struct Entry {
  uint32_t strokes[MAX_STROKES]{};
  uint32_t length = 0, text_bytes = 0, text_hash = 0, text_address = 0;
  const char *text =
      nullptr; // Pending browser edit only; saved records use offsets.
};
// Standard CRC-32 (IEEE, reflected), chainable: crc32(crc32(0, a), b) covers
// a then b. The portable form is what host tests compile; the device uses
// Zephyr's crc32_ieee_update (CONFIG_CRC) with identical results.
inline uint32_t crc32_portable(uint32_t previous, const void *data, size_t n) {
  uint32_t crc = ~previous;
  const auto *p = static_cast<const uint8_t *>(data);
  while (n--) {
    crc ^= *p++;
    for (int i = 0; i < 8; ++i)
      crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
  }
  return ~crc;
}
inline uint32_t crc32(uint32_t previous, const void *data, size_t n) {
#ifdef __ZEPHYR__
  return crc32_ieee_update(previous, static_cast<const uint8_t *>(data), n);
#else
  return crc32_portable(previous, data, n);
#endif
}
inline uint32_t hash_bytes(const void *data, size_t n) {
  uint32_t h = 2166136261u;
  const auto *p = static_cast<const uint8_t *>(data);
  while (n--) {
    h ^= *p++;
    h *= 16777619u;
  }
  return h;
}
inline uint32_t outline_hash(const Entry &e) {
  return hash_bytes(e.strokes, e.length * 4);
}
inline int key_compare(const Entry &a, const Entry &b) {
  uint32_t ah = outline_hash(a), bh = outline_hash(b);
  if (ah != bh)
    return ah < bh ? -1 : 1;
  if (a.length != b.length)
    return a.length < b.length ? -1 : 1;
  for (uint32_t i = 0; i < a.length; ++i)
    if (a.strokes[i] != b.strokes[i])
      return a.strokes[i] < b.strokes[i] ? -1 : 1;
  return 0;
}
inline int compare(const Entry &a, const Entry &b, bool reverse) {
  if (reverse && a.text_hash != b.text_hash)
    return a.text_hash < b.text_hash ? -1 : 1;
  return key_compare(a, b);
}
inline uint32_t record_bytes(const Entry &e) {
  return 8 + 4 * e.length + ((e.text_bytes + 3) & ~3u);
}
struct Device {
  virtual bool Read(uint32_t address, void *data, size_t size) = 0;
  virtual bool Program(uint32_t address, const void *data, size_t size) = 0;
  virtual bool Erase(uint32_t address) = 0;
  virtual ~Device() = default;
};
struct Source {
  virtual uint32_t Count() const = 0;
  // The same records in outline order and text-hash/outline order.
  virtual bool Get(uint32_t index, bool reverse, Entry &entry) = 0;
  // Optional packed-record position, relative to the records area. A source
  // that retains record order can relocate offsets instead of searching the
  // newly written forward index for every reverse entry. Validate still checks
  // the complete resulting indexes before publication.
  virtual bool RecordOffset(const Entry &, uint32_t &) const { return false; }
  virtual ~Source() = default;
};
class Store : public Source {
public:
  explicit Store(Device &device) : device(device) {}
  uint32_t Count() const override { return header.count; }
  uint32_t Used() const { return active < 0 ? 0 : header.used; }
  uint32_t Maximum() const { return header.maximum; }
  uint32_t Generation() const { return header.generation; }
  // Slots fully verified so far. Open verifies only the one it serves, so this
  // is 1 after a successful boot even if the older slot is also intact.
  unsigned ValidCopies() const { return valid_copies; }
  // Fully validates (CRC plus index check) the newer slot and serves it. The
  // older slot is validated only if the newer one is missing or invalid, so a
  // snapshot is never served unvalidated and the common boot does half the
  // work.
  bool Open() {
    AbortImport();
    active = -1;
    header = {};
    valid_copies = 0;
    Header candidates[2]{};
    bool usable[2]{};
    for (int slot = 0; slot < 2; ++slot)
      usable[slot] = device.Read(Base(slot), &candidates[slot],
                                 sizeof(candidates[slot])) &&
                     ValidHeader(candidates[slot]);
    // Ties keep slot 0, as a strictly-newer comparison would.
    const int first = usable[0] && usable[1]
                          ? (static_cast<int32_t>(candidates[1].generation -
                                                  candidates[0].generation) > 0
                                 ? 1
                                 : 0)
                          : (usable[0] ? 0 : 1);
    for (int n = 0; n < 2; ++n) {
      const int slot = n ? 1 - first : first;
      if (!usable[slot] || !Validate(Base(slot), candidates[slot]))
        continue;
      active = slot;
      header = candidates[slot];
      valid_copies = 1;
      break;
    }
    return active >= 0;
  }
  bool Get(uint32_t i, bool reverse, Entry &e) override {
    return active >= 0 && GetAt(Base(active), header, i, reverse, e);
  }
  bool RecordOffset(const Entry &e, uint32_t &offset) const override {
    if (active < 0)
      return false;
    const uint32_t start =
        Base(active) + SECTOR + Count() * 16 + 8 + e.length * 4;
    if (e.text_address < start || e.text_address >= Base(active) + header.used)
      return false;
    offset = e.text_address - start;
    return true;
  }
  bool ReadText(const Entry &e, uint32_t offset, void *data, size_t n) {
    if (offset > e.text_bytes || n > e.text_bytes - offset)
      return false;
    if (e.text) {
      memcpy(data, e.text + offset, n);
      return true;
    }
    return device.Read(e.text_address + offset, data, n);
  }
  bool Find(const Entry &key, Entry &e) {
    return active >= 0 && FindAt(Base(active), header, key, e);
  }
  uint32_t ReverseStart(uint32_t hash) {
    uint32_t lo = 0, hi = Count();
    Index idx{};
    while (lo < hi) {
      uint32_t mid = lo + (hi - lo) / 2;
      if (!ReadIndex(Base(active), header, mid, true, idx))
        return Count();
      if (idx.hash < hash)
        lo = mid + 1;
      else
        hi = mid;
    }
    return lo;
  }
  // Bulk imports write a host-built body into the inactive slot. The
  // published header is always constructed and verified by firmware.
  bool BeginImport(uint32_t generation, uint32_t count, uint32_t bytes,
                   uint32_t maximum, uint32_t body_crc) {
    Header candidate{MAGIC,
                     VERSION,
                     header.generation + 1,
                     count,
                     SECTOR + bytes,
                     maximum,
                     body_crc,
                     0};
    if (generation != Generation() || bytes > SLOT_BYTES - SECTOR ||
        !ValidShape(candidate) || !count || !maximum)
      return false;
    import = {};
    valid_copies = active < 0 ? 0 : 1;
    if (!device.Erase(Base(Inactive())))
      return false;
    import = candidate;
    import_offset = SECTOR;
    return true;
  }

  bool ImportChunk(uint32_t offset, const void *bytes, uint32_t size) {
    if (!import.magic || offset != import_offset - SECTOR || !size ||
        size > 256 || (size & 3) || size > import.used - import_offset ||
        (size != 256 && size != import.used - import_offset))
      return false;
    const uint32_t address = Base(Inactive()) + import_offset;
    if ((import_offset % SECTOR == 0 && !device.Erase(address)) ||
        !WriteVerified(address, bytes, size)) {
      import = {};
      return false;
    }
    import_offset += size;
    return true;
  }

  bool FinishImport() {
    if (!import.magic || import_offset != import.used)
      return false;
    Header candidate = import;
    import = {};
    return Validate(Base(Inactive()), candidate) && Publish(candidate);
  }

  void AbortImport() { import = {}; }

  // Erase only occupied sectors; invalidate target header first, commit last.
  // Failure never changes the currently active slot.
  bool Save(Source &source) {
    AbortImport();
    Header next{
        MAGIC, VERSION, header.generation + 1, source.Count(), SECTOR, 0, 0, 0};
    if (next.count > (SLOT_BYTES - SECTOR) / 32)
      return false;
    next.used += next.count * 16;
    Entry e{}, previous{};
    for (uint32_t i = 0; i < next.count; ++i) {
      if (!source.Get(i, false, e) || !ValidEntry(e) ||
          (i && key_compare(previous, e) >= 0))
        return false;
      if (record_bytes(e) > SLOT_BYTES - next.used)
        return false;
      next.used += record_bytes(e);
      if (e.length > next.maximum)
        next.maximum = e.length;
      previous = e;
    }
    int target = active == 0 ? 1 : 0;
    uint32_t base = Base(target);
    valid_copies = active < 0 ? 0 : 1;
    for (uint32_t offset = 0; offset < next.used; offset += SECTOR)
      if (!device.Erase(base + offset))
        return false;
    uint32_t offset = SECTOR + next.count * 16;
    // Separate sequential streams: both indexes and the packed records. Each
    // stream fills physical page boundaries and verifies every flushed byte.
    // Buffers live only during a save; no dictionary-sized RAM allocation.
    PageWriter forward(*this, base + SECTOR);
    PageWriter records(*this, base + offset);
    for (uint32_t i = 0; i < next.count; ++i) {
      if (!source.Get(i, false, e))
        return false;
      Index idx{outline_hash(e), offset};
      uint32_t record[]{e.length, e.text_bytes};
      if (!forward.Append(&idx, sizeof(idx)) ||
          !records.Append(record, sizeof(record)) ||
          !records.Append(e.strokes, e.length * 4) || !records.Text(e))
        return false;
      offset += record_bytes(e);
    }
    if (!forward.Flush() || !records.Flush())
      return false;
    // Reuse the forward stream's page buffer for the reverse index.
    forward.Reset(base + SECTOR + next.count * 8);
    for (uint32_t i = 0; i < next.count; ++i) {
      if (!source.Get(i, true, e) || (i && compare(previous, e, true) >= 0))
        return false;
      uint32_t relative;
      if (!source.RecordOffset(e, relative)) {
        Entry mapped{};
        if (!FindAt(base, next, e, mapped))
          return false;
        relative = mapped.text_address - base - 8 - mapped.length * 4 - SECTOR -
                   next.count * 16;
      }
      if (relative >= next.used - SECTOR - next.count * 16)
        return false;
      Index idx{e.text_hash, SECTOR + next.count * 16 + relative};
      if (!forward.Append(&idx, sizeof(idx)))
        return false;
      previous = e;
    }
    if (!forward.Flush())
      return false;
    // Integrity chain: WriteVerified read back every programmed byte, so flash
    // holds exactly what was intended; this single read-back pass computes the
    // CRC of what is really in flash for the header; Validate (structure only)
    // then checks ordering, offsets and both indexes. Open and imports, whose
    // bytes were not just verified here, re-check the CRC.
    if (!BodyCrc(base, next, next.body_crc) || !Validate(base, next, false))
      return false;
    return Publish(next);
  }

private:
  class PageWriter {
  public:
    PageWriter(Store &store, uint32_t address)
        : store(store), address(address) {}
    void Reset(uint32_t next) { address = next; }
    bool Append(const void *data, uint32_t size) {
      const auto *bytes = static_cast<const uint8_t *>(data);
      while (size) {
        uint32_t n = Room();
        if (n > size)
          n = size;
        memcpy(buffer + used, bytes, n);
        used += n;
        bytes += n;
        size -= n;
        if (!Room() && !Flush())
          return false;
      }
      return true;
    }
    bool Text(const Entry &e) {
      for (uint32_t off = 0; off < e.text_bytes;) {
        uint32_t n = Room();
        if (n > e.text_bytes - off)
          n = e.text_bytes - off;
        if (!store.ReadText(e, off, buffer + used, n))
          return false;
        used += n;
        off += n;
        if (!Room() && !Flush())
          return false;
      }
      const uint32_t padding = 0xffffffffu;
      return Append(&padding, (0u - e.text_bytes) & 3u);
    }
    bool Flush() {
      if (!used)
        return true;
      if (!store.WriteVerified(address, buffer, used))
        return false;
      address += used;
      used = 0;
      return true;
    }

  private:
    uint32_t Room() const { return sizeof(buffer) - (address & 255u) - used; }
    Store &store;
    uint32_t address, used = 0;
    alignas(4) uint8_t buffer[256];
  };
  Device &device;
  int active = -1;
  unsigned valid_copies = 0;
  Header header{}, import{};
  uint32_t import_offset = 0;
  int Inactive() const { return active == 0 ? 1 : 0; }
  static uint32_t Base(int slot) { return SLOT_START + slot * SLOT_BYTES; }
  bool Publish(Header &next) {
    const int target = Inactive();
    next.header_crc = crc32(0, &next, offsetof(Header, header_crc));
    if (!WriteVerified(Base(target), &next, sizeof(next)))
      return false;
    ++valid_copies;
    active = target;
    header = next;
    return true;
  }
  static bool ValidShape(const Header &h) {
    return h.used >= SECTOR && h.used <= SLOT_BYTES && !(h.used & 3) &&
           h.count <= (h.used - SECTOR) / 32 && h.maximum <= MAX_STROKES;
  }
  static bool ValidHeader(const Header &h) {
    return h.magic == MAGIC && h.version == VERSION &&
           h.header_crc == crc32(0, &h, offsetof(Header, header_crc)) &&
           ValidShape(h);
  }
  static bool ValidEntry(const Entry &e) {
    return e.length && e.length <= MAX_STROKES && e.text_bytes &&
           e.text_bytes <= MAX_TEXT + 1;
  }
  bool ReadIndex(uint32_t base, const Header &h, uint32_t i, bool reverse,
                 Index &idx) {
    return i < h.count &&
           device.Read(base + SECTOR + (reverse ? h.count * 8 : 0) + i * 8,
                       &idx, sizeof(idx));
  }
  bool GetAt(uint32_t base, const Header &h, uint32_t i, bool reverse,
             Entry &e) {
    Index idx{};
    uint32_t record[2]{};
    if (!ReadIndex(base, h, i, reverse, idx) ||
        idx.offset < SECTOR + h.count * 16 || (idx.offset & 3) ||
        idx.offset > h.used - 8 ||
        !device.Read(base + idx.offset, record, sizeof(record)))
      return false;
    e = {};
    e.length = record[0];
    e.text_bytes = record[1];
    if (!ValidEntry(e) || record_bytes(e) > h.used - idx.offset ||
        !device.Read(base + idx.offset + 8, e.strokes, e.length * 4))
      return false;
    e.text_address = base + idx.offset + 8 + e.length * 4;
    if (reverse)
      e.text_hash = idx.hash;
    return true;
  }
  bool TextHash(const Entry &e, uint32_t &hash) {
    uint8_t bytes[64];
    hash = 2166136261u;
    for (uint32_t off = 0; off < e.text_bytes - 1;) {
      uint32_t n = e.text_bytes - 1 - off;
      if (n > sizeof(bytes))
        n = sizeof(bytes);
      if (!device.Read(e.text_address + off, bytes, n))
        return false;
      for (uint32_t j = 0; j < n; ++j) {
        hash ^= bytes[j];
        hash *= 16777619u;
      }
      off += n;
    }
    return true;
  }
  // The forward pass has already validated every record and its exact offset.
  // Check membership using just the index, without decoding the same record
  // again on the reverse pass. Hash collisions still require matching offset.
  bool HasForwardIndex(uint32_t base, const Header &h, const Entry &e) {
    const uint32_t hash = outline_hash(e);
    const uint32_t offset = e.text_address - base - 8 - e.length * 4;
    uint32_t lo = 0, hi = h.count;
    Index idx{};
    while (lo < hi) {
      const uint32_t mid = lo + (hi - lo) / 2;
      if (!ReadIndex(base, h, mid, false, idx))
        return false;
      if (idx.hash < hash)
        lo = mid + 1;
      else
        hi = mid;
    }
    for (; lo < h.count; ++lo) {
      if (!ReadIndex(base, h, lo, false, idx) || idx.hash != hash)
        return false;
      if (idx.offset == offset)
        return true;
    }
    return false;
  }
  bool FindAt(uint32_t base, const Header &h, const Entry &key, Entry &e) {
    uint32_t lo = 0, hi = h.count, hash = outline_hash(key);
    Index idx{};
    while (lo < hi) {
      uint32_t mid = lo + (hi - lo) / 2;
      if (!ReadIndex(base, h, mid, false, idx))
        return false;
      if (idx.hash < hash)
        lo = mid + 1;
      else
        hi = mid;
    }
    for (; lo < h.count; ++lo) {
      if (!ReadIndex(base, h, lo, false, idx) || idx.hash != hash ||
          !GetAt(base, h, lo, false, e))
        return false;
      int cmp = key_compare(e, key);
      if (!cmp)
        return true;
      if (cmp > 0)
        return false;
    }
    return false;
  }
  bool WriteVerified(uint32_t address, const void *data, size_t size) {
    if (!device.Program(address, data, size))
      return false;
    alignas(4) uint8_t check[256];
    const auto *expected = static_cast<const uint8_t *>(data);
    for (size_t off = 0; off < size;) {
      const size_t n = size - off > sizeof(check) ? sizeof(check) : size - off;
      if (!device.Read(address + off, check, n) ||
          memcmp(check, expected + off, n))
        return false;
      off += n;
    }
    return true;
  }
  bool BodyCrc(uint32_t base, const Header &h, uint32_t &crc) {
    alignas(4) uint8_t bytes[256];
    crc = 0;
    for (uint32_t off = SECTOR; off < h.used;) {
      uint32_t n = h.used - off > sizeof(bytes) ? sizeof(bytes) : h.used - off;
      if (!device.Read(base + off, bytes, n))
        return false;
      crc = crc32(crc, bytes, n);
      off += n;
    }
    return true;
  }
  bool Validate(uint32_t base, const Header &h, bool check_crc = true) {
    uint32_t crc;
    if (check_crc && (!BodyCrc(base, h, crc) || crc != h.body_crc))
      return false;
    uint32_t expected = SECTOR + h.count * 16, maximum = 0;
    Entry e{}, prev{};
    Index idx{};
    for (uint32_t i = 0; i < h.count; ++i) {
      if (!ReadIndex(base, h, i, false, idx) || idx.offset != expected ||
          !GetAt(base, h, i, false, e) || idx.hash != outline_hash(e) ||
          (i && key_compare(prev, e) >= 0))
        return false;
      uint8_t end;
      if (!device.Read(e.text_address + e.text_bytes - 1, &end, 1) || end)
        return false;
      expected += record_bytes(e);
      if (e.length > maximum)
        maximum = e.length;
      prev = e;
    }
    if (expected != h.used || maximum != h.maximum)
      return false;
    for (uint32_t i = 0; i < h.count; ++i) {
      uint32_t text_hash;
      if (!GetAt(base, h, i, true, e) || (i && compare(prev, e, true) >= 0) ||
          !HasForwardIndex(base, h, e) || !TextHash(e, text_hash) ||
          e.text_hash != text_hash)
        return false;
      prev = e;
    }
    return true;
  }
};
class EditSource : public Source {
public:
  EditSource(Store &store, const Entry &key, const Entry *replacement,
             bool clear = false)
      : store(store), key(key), replacement(replacement), clear(clear) {}
  bool Prepare() {
    removed[0] = removed[1] = -1;
    removed_bytes = inserted_bytes = 0;
    if (clear)
      return true;
    for (int reverse = 0; reverse < 2; ++reverse) {
      Entry e{};
      uint32_t filtered = 0;
      for (uint32_t i = 0; i < store.Count(); ++i) {
        if (!store.Get(i, reverse, e))
          return false;
        if (key_compare(e, key) == 0) {
          removed[reverse] = i;
          if (!reverse)
            removed_bytes = record_bytes(e);
          continue;
        }
        if (replacement && compare(e, *replacement, reverse) < 0) {
          ++filtered;
          if (!reverse)
            inserted_bytes += record_bytes(e);
        }
      }
      inserted[reverse] = filtered;
    }
    return (removed[0] >= 0) == (removed[1] >= 0);
  }
  bool Exists() const { return removed[0] >= 0; }
  uint32_t Count() const override {
    return clear ? 0
                 : store.Count() - (Exists() ? 1 : 0) + (replacement ? 1 : 0);
  }
  bool Get(uint32_t i, bool reverse, Entry &e) override {
    if (i >= Count())
      return false;
    if (replacement && i == inserted[reverse]) {
      e = *replacement;
      return true;
    }
    uint32_t old = i - (replacement && i > inserted[reverse] ? 1 : 0);
    if (removed[reverse] >= 0 && old >= static_cast<uint32_t>(removed[reverse]))
      ++old;
    return store.Get(old, reverse, e);
  }

  bool RecordOffset(const Entry &e, uint32_t &offset) const override {
    if (replacement && key_compare(e, *replacement) == 0) {
      offset = inserted_bytes;
      return true;
    }
    if (!store.RecordOffset(e, offset))
      return false;
    // Packed records retain their forward order across a one-entry edit.
    if (Exists() && key_compare(e, key) > 0)
      offset -= removed_bytes;
    if (replacement && key_compare(e, *replacement) > 0)
      offset += record_bytes(*replacement);
    return true;
  }

private:
  Store &store;
  Entry key;
  const Entry *replacement;
  bool clear;
  int32_t removed[2]{-1, -1};
  uint32_t inserted[2]{};
  uint32_t removed_bytes = 0, inserted_bytes = 0;
};
} // namespace squid_user
