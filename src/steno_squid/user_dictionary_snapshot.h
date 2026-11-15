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
  virtual ~Source() = default;
};
class Store : public Source {
public:
  explicit Store(Device &device) : device(device) {}
  uint32_t Count() const override { return header.count; }
  uint32_t Used() const { return active < 0 ? 0 : header.used; }
  uint32_t Maximum() const { return header.maximum; }
  uint32_t Generation() const { return header.generation; }
  unsigned ValidCopies() const { return valid_copies; }
  bool Open() {
    AbortImport();
    active = -1;
    header = {};
    valid_copies = 0;
    for (int slot = 0; slot < 2; ++slot) {
      Header candidate{};
      if (!device.Read(Base(slot), &candidate, sizeof(candidate)) ||
          !ValidHeader(candidate) || !Validate(Base(slot), candidate))
        continue;
      ++valid_copies;
      if (active < 0 ||
          static_cast<int32_t>(candidate.generation - header.generation) > 0) {
        active = slot;
        header = candidate;
      }
    }
    return active >= 0;
  }
  bool Get(uint32_t i, bool reverse, Entry &e) override {
    return active >= 0 && GetAt(Base(active), header, i, reverse, e);
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
    return active >= 0 && FindAt(Base(active), header, key, e, false);
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
    alignas(4) uint8_t buffer[256];
    for (uint32_t i = 0; i < next.count; ++i) {
      if (!source.Get(i, false, e))
        return false;
      Index idx{outline_hash(e), offset};
      uint32_t record[]{e.length, e.text_bytes};
      if (!WriteVerified(base + SECTOR + i * 8, &idx, sizeof(idx)) ||
          !WriteVerified(base + offset, record, sizeof(record)) ||
          !WriteVerified(base + offset + 8, e.strokes, e.length * 4))
        return false;
      for (uint32_t off = 0; off < e.text_bytes;) {
        uint32_t n = e.text_bytes - off > sizeof(buffer) ? sizeof(buffer)
                                                         : e.text_bytes - off;
        memset(buffer, 0xff, sizeof(buffer));
        if (!ReadText(e, off, buffer, n) ||
            !WriteVerified(base + offset + 8 + e.length * 4 + off, buffer,
                           (n + 3) & ~3u))
          return false;
        off += n;
      }
      offset += record_bytes(e);
    }
    for (uint32_t i = 0; i < next.count; ++i) {
      Entry mapped{};
      if (!source.Get(i, true, e) || (i && compare(previous, e, true) >= 0) ||
          !FindAt(base, next, e, mapped))
        return false;
      Index idx{e.text_hash,
                mapped.text_address - base - 8 - mapped.length * 4};
      if (!WriteVerified(base + SECTOR + next.count * 8 + i * 8, &idx,
                         sizeof(idx)))
        return false;
      previous = e;
    }
    if (!BodyCrc(base, next, next.body_crc) || !Validate(base, next))
      return false;
    return Publish(next);
  }

private:
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
  bool GetAt(uint32_t base, const Header &h, uint32_t i, bool reverse, Entry &e,
             bool need_text_hash = true) {
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
    else if (need_text_hash) {
      uint8_t bytes[64];
      uint32_t hash = 2166136261u;
      for (uint32_t off = 0; off < e.text_bytes - 1;) {
        uint32_t n = e.text_bytes - 1 - off > sizeof(bytes)
                         ? sizeof(bytes)
                         : e.text_bytes - 1 - off;
        if (!device.Read(e.text_address + off, bytes, n))
          return false;
        for (uint32_t j = 0; j < n; ++j) {
          hash ^= bytes[j];
          hash *= 16777619u;
        }
        off += n;
      }
      e.text_hash = hash;
    }
    return true;
  }
  bool FindAt(uint32_t base, const Header &h, const Entry &key, Entry &e,
              bool need_text_hash = true) {
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
          !GetAt(base, h, lo, false, e, need_text_hash))
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
  bool Validate(uint32_t base, const Header &h) {
    uint32_t crc;
    if (!BodyCrc(base, h, crc) || crc != h.body_crc)
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
      Entry match{};
      if (!GetAt(base, h, i, true, e) || (i && compare(prev, e, true) >= 0) ||
          !FindAt(base, h, e, match) || e.text_address != match.text_address ||
          e.text_hash != match.text_hash)
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
          continue;
        }
        if (replacement && compare(e, *replacement, reverse) < 0)
          ++filtered;
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

private:
  Store &store;
  Entry key;
  const Entry *replacement;
  bool clear;
  int32_t removed[2]{-1, -1};
  uint32_t inserted[2]{};
};
} // namespace squid_user
