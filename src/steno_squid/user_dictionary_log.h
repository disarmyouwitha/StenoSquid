#pragma once
// Personal dictionary store: one pool of 64 KiB segments holding an
// append-only record log, two write-once hash tables and a small meta log.
// Format and algorithms: docs/personal-dictionary-spec.md.
//
// Header-only, pointer-free on flash, no Zephyr includes beyond CRC, so the
// host tests compile it unchanged. All calls must be serialized by the caller
// (the engine lock on the device).
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#ifdef __ZEPHYR__
#include <zephyr/sys/crc.h>
#endif
namespace squid_user {
constexpr uint32_t REGION_START = 0x600000, SEG_BYTES = 0x10000,
                   SEG_COUNT = 159, SEG_HEADER = 16,
                   SLOTS_PER_SEG = (SEG_BYTES - SEG_HEADER) / 4,
                   MAX_TABLE_SEGS = 32, RESERVE = 3, MAX_STROKES = 16,
                   MAX_TEXT = 2047, MAGIC = 0x3355445a, STATE_BYTES = 80,
                   EMPTY = 0xffffffffu, IMPORT_CHUNK = 512;
constexpr uint32_t F_ALLOC = 1u << 31, F_COMMIT = 1u << 30, F_DEAD = 1u << 29;
enum Kind : uint8_t { FREE = 0, RECORD = 1, FORWARD = 2, REVERSE = 3, META = 4 };

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
inline uint32_t hash_more(uint32_t h, const void *data, size_t n) {
  const auto *p = static_cast<const uint8_t *>(data);
  while (n--) {
    h ^= *p++;
    h *= 16777619u;
  }
  return h;
}
inline uint32_t hash_bytes(const void *data, size_t n) {
  return hash_more(2166136261u, data, n);
}
inline uint32_t record_bytes(uint32_t strokes, uint32_t text_bytes) {
  return 8 + 4 * strokes + ((text_bytes + 3) & ~3u);
}
inline uint32_t size_check(uint32_t strokes, uint32_t text_bytes) {
  const uint32_t pair[2] = {strokes, text_bytes};
  return hash_bytes(pair, sizeof(pair)) & 0xfffu;
}

struct Device {
  virtual bool Read(uint32_t address, void *data, size_t size) = 0;
  // Within one 256-byte page. May only clear bits.
  virtual bool Program(uint32_t address, const void *data, size_t size) = 0;
  virtual bool Erase(uint32_t address) = 0; // one 4 KiB sector
  virtual bool EraseBlock(uint32_t address) { // one 64 KiB block
    for (uint32_t off = 0; off < SEG_BYTES; off += 4096)
      if (!Erase(address + off))
        return false;
    return true;
  }
  virtual ~Device() = default;
};

struct SegHeader {
  uint32_t magic;
  uint8_t kind, index;
  uint16_t epoch;
  uint32_t sequence, crc;
};
struct State {
  uint32_t flags, void_before;
  uint16_t fwd_epoch, fwd_segments, rev_epoch, rev_segments;
  uint32_t max_strokes;
  uint32_t import_first, import_count; // nonzero count: unfinished import
  uint32_t reserved[12];
  uint32_t crc;
};
static_assert(sizeof(SegHeader) == SEG_HEADER, "segment header layout");
static_assert(sizeof(State) == STATE_BYTES, "state record layout");

struct Entry {
  uint32_t strokes[MAX_STROKES]{};
  uint32_t length = 0, text_bytes = 0;
  const char *text = nullptr; // Caller's text for Add; unused on results.
  uint32_t text_address = 0;  // Flash address of the stored text.
  uint32_t address = 0;       // Flash address of the stored record.
};

class Store {
public:
  enum class Result { OK, UNCHANGED, NOT_FOUND, BUSY, FULL, FAILED };
  struct ReverseCursor {
    uint32_t hash = 0, index = 0, probes = 0, seen_count = 0, seen[16]{};
    bool started = false;
  };
  struct Iterator {
    uint8_t order[SEG_COUNT]{};
    uint32_t count = 0, position = 0, offset = SEG_HEADER;
    bool started = false;
  };
  struct ImportPlan {
    uint32_t first_sequence = 0, total = 0;
    uint16_t fwd_epoch = 0, rev_epoch = 0;
    uint8_t segments[SEG_COUNT]{}; // records, then forward, then reverse
  };
  struct VerifyReport {
    uint32_t live = 0, bad_crc = 0, unreachable = 0;
  };

  explicit Store(Device &device, uint32_t start = REGION_START,
                 uint32_t segments = SEG_COUNT)
      : device(device), start(start),
        nseg(segments > SEG_COUNT ? SEG_COUNT : segments) {}

  // ---- status ----
  bool Ready() const { return opened; }
  bool StatsReady() const { return stats_ready; }
  bool Rebuilding() const { return rb.active; }
  bool Cleaning() const { return cl.seg >= 0; }
  bool Importing() const { return imp.active; }
  bool Recovering() const {
    return opened && (!tables[0].segs || !tables[1].segs);
  }
  uint32_t MaxStrokes() const { return state.max_strokes; }
  uint32_t IntegrityErrors() const { return integrity_errors; }
  uint32_t NextSequence() const { return next_seq; }
  uint32_t Slots(int t) const { return tables[t].segs * SLOTS_PER_SEG; }
  uint32_t SlotsUsed(int t) const {
    uint32_t used = 0;
    for (uint32_t i = 0; i < tables[t].segs; ++i)
      used += segs[tables[t].map[i]].live;
    return used;
  }
  uint32_t FreeSegments() const {
    uint32_t n = 0;
    for (uint32_t s = 0; s < nseg; ++s)
      n += segs[s].kind == FREE && !segs[s].reserved;
    return n;
  }
  uint32_t RecordSegments() const {
    uint32_t n = 0;
    for (uint32_t s = 0; s < nseg; ++s)
      n += IsRecord(s);
    return n;
  }
  uint32_t Entries() const {
    uint32_t n = 0;
    for (uint32_t s = 0; s < nseg; ++s)
      if (IsRecord(s))
        n += segs[s].live;
    return n;
  }
  uint32_t DeadBytes() const {
    uint32_t n = 0;
    for (uint32_t s = 0; s < nseg; ++s)
      if (IsRecord(s))
        n += segs[s].dead;
    return n;
  }

  // ---- open ----
  bool Open() {
    opened = false;
    stats_ready = false;
    rb = {};
    cl = {};
    imp = {};
    head = -1;
    meta = -1;
    tables[0] = tables[1] = {};
    rebuild_pending[0] = rebuild_pending[1] = false;
    table_stuck[0] = table_stuck[1] = false;
    next_seq = 1;
    bool any_record = false;
    for (uint32_t s = 0; s < nseg; ++s) {
      segs[s] = {};
      SegHeader h{};
      if (!device.Read(Base(s), &h, sizeof(h)))
        return false;
      if (h.magic != MAGIC || h.crc != crc32(0, &h, 12) || h.kind < RECORD ||
          h.kind > META)
        continue;
      segs[s].kind = h.kind;
      segs[s].index = h.index;
      segs[s].epoch = h.epoch;
      segs[s].sequence = h.sequence;
      if (h.sequence >= next_seq)
        next_seq = h.sequence + 1;
      any_record |= h.kind == RECORD;
    }
    // Current state: last good record in the newest meta segment that has one.
    bool have_state = false;
    for (;;) {
      int best = -1;
      for (uint32_t s = 0; s < nseg; ++s)
        if (segs[s].kind == META &&
            (best < 0 || Newer(segs[s].sequence, segs[best].sequence)))
          best = static_cast<int>(s);
      if (best < 0)
        break;
      if (ReadMeta(static_cast<uint32_t>(best))) {
        meta = best;
        have_state = true;
        break;
      }
      if (!Retire(static_cast<uint32_t>(best)))
        return false;
    }
    if (!have_state) {
      if (!(any_record ? Recover() : Format()))
        return false;
    } else if (state.import_count) {
      // An import was begun and never committed: drop what it uploaded.
      for (uint32_t s = 0; s < nseg; ++s)
        if (segs[s].kind != FREE &&
            segs[s].sequence - state.import_first < state.import_count &&
            !Retire(s))
          return false;
      if (state.import_first + state.import_count > next_seq)
        next_seq = state.import_first + state.import_count;
    }
    for (uint32_t s = 0; s < nseg; ++s) {
      const SegInfo &g = segs[s];
      const bool stale =
          (g.kind == META && static_cast<int>(s) != meta) ||
          (g.kind == RECORD && Newer(state.void_before, g.sequence)) ||
          (g.kind == FORWARD && (g.epoch != state.fwd_epoch ||
                                 g.index >= state.fwd_segments)) ||
          (g.kind == REVERSE &&
           (g.epoch != state.rev_epoch || g.index >= state.rev_segments));
      if (stale && !Retire(s))
        return false;
    }
    CollectTable(0);
    CollectTable(1);
    // Head and its tail.
    uint8_t order[SEG_COUNT];
    const uint32_t count = Order(order);
    uint32_t last_seg = 0, last_off = 0;
    bool have_last = false;
    if (count) {
      head = order[count - 1];
      if (!WalkTail(static_cast<uint32_t>(head), head_off, last_off, have_last))
        return false;
      last_seg = static_cast<uint32_t>(head);
      if (!have_last && count > 1) {
        uint32_t ignored;
        last_seg = order[count - 2];
        if (!WalkTail(last_seg, ignored, last_off, have_last))
          return false;
      }
    }
    opened = true;
    if (have_last && !Replay(last_seg, last_off))
      return false;
    return true;
  }

  // ---- lookups ----
  bool Find(const uint32_t *strokes, uint32_t length, Entry &out) {
    return opened && FindIn(tables[0], strokes, length, out, 0, false);
  }
  // Each call returns the next live record whose text matches.
  bool ReverseNext(ReverseCursor &c, const char *text, uint32_t text_bytes,
                   Entry &out) {
    const Table &t = tables[1];
    const uint32_t slots = t.segs * SLOTS_PER_SEG;
    if (!opened || !slots)
      return false;
    if (!c.started) {
      c.hash = hash_bytes(text, text_bytes);
      c.index = c.hash % slots;
      c.started = true;
    }
    for (; c.probes < slots; ++c.probes, c.index = (c.index + 1) % slots) {
      uint32_t v;
      if (!ReadSlot(t, c.index, v) || v == EMPTY) {
        c.probes = slots;
        return false;
      }
      if ((v >> 22) != (c.hash >> 22))
        continue;
      bool seen = false;
      for (uint32_t i = 0; i < c.seen_count; ++i)
        seen |= c.seen[i] == v;
      uint32_t seg, off;
      Rec r;
      if (seen || !Target(v, false, seg, off, r) || r.text != text_bytes ||
          !Load(seg, off, r, out, true) ||
          !SameText(out.text_address, text, text_bytes))
        continue;
      if (c.seen_count < 16)
        c.seen[c.seen_count++] = v;
      ++c.probes;
      c.index = (c.index + 1) % slots;
      return true;
    }
    return false;
  }
  bool ReadText(const Entry &e, uint32_t offset, void *data, size_t n) {
    return offset <= e.text_bytes && n <= e.text_bytes - offset &&
           device.Read(e.text_address + offset, data, n);
  }
  // Live records in insertion order.
  bool Next(Iterator &it, Entry &out) {
    if (!opened)
      return false;
    if (!it.started) {
      it.count = Order(it.order);
      it.started = true;
    }
    while (it.position < it.count) {
      const uint32_t seg = it.order[it.position];
      Rec r;
      if (!IsRecord(seg) || !ReadRec(seg, it.offset, r) || r.end || !r.valid) {
        ++it.position;
        it.offset = SEG_HEADER;
        continue;
      }
      const uint32_t off = it.offset;
      it.offset += r.size;
      if (r.live && Load(seg, off, r, out, true))
        return true;
    }
    return false;
  }

  // ---- writes ----
  Result Add(const Entry &e) {
    if (!opened || !e.length || e.length > MAX_STROKES ||
        e.text_bytes > MAX_TEXT || !e.text)
      return Result::FAILED;
    if (rb.active || imp.active || Recovering())
      return Result::BUSY;
    Entry old{};
    const bool replacing = FindIn(tables[0], e.strokes, e.length, old, 0, false);
    if (replacing && old.text_bytes == e.text_bytes &&
        SameText(old.text_address, e.text, e.text_bytes))
      return Result::UNCHANGED;
    const Result r = Insert(e.strokes, e.length, e.text, 0, e.text_bytes,
                            replacing ? &old : nullptr, false);
    return r;
  }
  Result Remove(const uint32_t *strokes, uint32_t length) {
    if (!opened)
      return Result::FAILED;
    if (rb.active || imp.active || Recovering())
      return Result::BUSY;
    Entry old{};
    if (!FindIn(tables[0], strokes, length, old, 0, false))
      return Result::NOT_FOUND;
    return MarkDead(old.address) ? Result::OK : Result::FAILED;
  }
  Result Reset() {
    if (!opened)
      return Result::FAILED;
    if (rb.active || imp.active)
      return Result::BUSY;
    cl = {};
    Table fresh[2];
    State next = state;
    next.void_before = next_seq;
    next.max_strokes = 0;
    next.fwd_epoch = static_cast<uint16_t>(state.fwd_epoch + 1);
    next.rev_epoch = static_cast<uint16_t>(state.rev_epoch + 1);
    next.fwd_segments = next.rev_segments = 1;
    for (int t = 0; t < 2; ++t) {
      const int s = Allocate(t ? REVERSE : FORWARD, 0,
                             t ? next.rev_epoch : next.fwd_epoch, true);
      if (s < 0)
        return Result::FULL;
      fresh[t].segs = 1;
      fresh[t].map[0] = static_cast<uint8_t>(s);
    }
    next.void_before = next_seq;
    if (!AppendState(next))
      return Result::FAILED;
    for (uint32_t s = 0; s < nseg; ++s) {
      const bool old_table =
          (segs[s].kind == FORWARD && s != fresh[0].map[0]) ||
          (segs[s].kind == REVERSE && s != fresh[1].map[0]);
      if ((segs[s].kind == RECORD || old_table) && !Retire(s))
        return Result::FAILED;
    }
    tables[0] = fresh[0];
    tables[1] = fresh[1];
    head = -1;
    rebuild_pending[0] = rebuild_pending[1] = false;
    table_stuck[0] = table_stuck[1] = false;
    stats_ready = true;
    return Result::OK;
  }
  void RequestRebuild() {
    rebuild_pending[0] = rebuild_pending[1] = true;
    table_stuck[0] = table_stuck[1] = false;
  }

  // ---- background work ----
  // Does one slice of pending work. Returns true while more is waiting, so the
  // caller can release its lock and call again.
  bool Step() {
    if (!opened || imp.active)
      return false;
    if (rb.active)
      return RebuildSlice(), true;
    if (!stats_ready)
      return StatsSlice(), true;
    for (int t = 0; t < 2; ++t) {
      if (!rebuild_pending[t] && !tables[t].segs && !table_stuck[t])
        rebuild_pending[t] = true;
      if (!rebuild_pending[t])
        continue;
      if (StartRebuild(t))
        return true;
      if (Victim() >= 0 && CleanSlice())
        return true;
      rebuild_pending[t] = false;
      table_stuck[t] = true;
    }
    if (cl.seg >= 0 || (FreeSegments() < 8 && VictimWorthIt() >= 0))
      return CleanSlice();
    return false;
  }

  // ---- bulk import ----
  // Reserves free segments and reports where the host's image must go.
  Result ImportBegin(uint32_t records, uint32_t forward, uint32_t reverse,
                     bool replace, ImportPlan &plan) {
    if (!opened)
      return Result::FAILED;
    if (rb.active || imp.active)
      return Result::BUSY;
    const uint32_t total = records + forward + reverse;
    if (!records || !forward || !reverse || forward > MAX_TABLE_SEGS ||
        reverse > MAX_TABLE_SEGS || total > nseg)
      return Result::FAILED;
    cl = {};
    if (total + RESERVE > FreeSegments()) {
      if (!replace)
        return Result::FULL;
      const Result r = Reset();
      if (r != Result::OK)
        return r;
      if (total + RESERVE > FreeSegments())
        return Result::FULL;
    }
    State next = state;
    next.import_first = next_seq;
    next.import_count = total;
    if (!AppendState(next))
      return Result::FAILED;
    imp = {};
    imp.active = true;
    imp.records = records;
    imp.forward = forward;
    imp.reverse = reverse;
    imp.plan.first_sequence = next_seq;
    imp.plan.total = total;
    imp.plan.fwd_epoch = static_cast<uint16_t>(state.fwd_epoch + 1);
    imp.plan.rev_epoch = static_cast<uint16_t>(state.rev_epoch + 1);
    next_seq += total;
    uint32_t n = 0;
    for (uint32_t s = 0; s < nseg && n < total; ++s)
      if (segs[s].kind == FREE && !segs[s].reserved) {
        segs[s].reserved = true;
        imp.plan.segments[n++] = static_cast<uint8_t>(s);
      }
    plan = imp.plan;
    return Result::OK;
  }
  // Segments in plan order, each from offset 0, contiguous chunks.
  bool ImportChunk(uint32_t n, uint32_t offset, const void *data,
                   uint32_t size) {
    if (!imp.active || n >= imp.plan.total || !size || size > IMPORT_CHUNK ||
        (size & 3) || offset > SEG_BYTES || size > SEG_BYTES - offset)
      return false;
    if (offset == 0) {
      if (n != imp.next)
        return false;
      if (!device.EraseBlock(Base(imp.plan.segments[n])))
        return false;
      imp.next = n + 1;
      imp.offset = 0;
    } else if (n + 1 != imp.next || offset != imp.offset) {
      return false;
    }
    if (!ProgramVerified(Base(imp.plan.segments[n]) + offset, data, size))
      return false;
    imp.offset = offset + size;
    return true;
  }
  bool ImportFinish() {
    if (!imp.active || imp.next != imp.plan.total)
      return ImportAbort(), false;
    Table fresh[2]{};
    uint32_t max_strokes = 0;
    // Headers first, so lookups can run against the uploaded tables.
    for (uint32_t n = 0; n < imp.plan.total; ++n) {
      const uint32_t s = imp.plan.segments[n];
      const bool is_record = n < imp.records;
      const bool is_forward = !is_record && n < imp.records + imp.forward;
      const uint32_t index =
          is_record ? 0
                    : n - imp.records - (is_forward ? 0 : imp.forward);
      const uint8_t kind = is_record ? RECORD : is_forward ? FORWARD : REVERSE;
      const uint16_t epoch = is_record    ? 0
                             : is_forward ? imp.plan.fwd_epoch
                                          : imp.plan.rev_epoch;
      SegHeader h{};
      if (!device.Read(Base(s), &h, sizeof(h)) || h.magic != MAGIC ||
          h.crc != crc32(0, &h, 12) || h.kind != kind || h.index != index ||
          h.epoch != epoch || h.sequence != imp.plan.first_sequence + n)
        return ImportAbort(), false;
      segs[s].kind = kind;
      segs[s].index = static_cast<uint8_t>(index);
      segs[s].epoch = epoch;
      segs[s].sequence = h.sequence;
      segs[s].pending = true;
      segs[s].live = 0;
      segs[s].dead = 0;
      if (!is_record) {
        Table &t = fresh[is_forward ? 0 : 1];
        t.map[index] = static_cast<uint8_t>(s);
        t.segs = static_cast<uint16_t>(is_forward ? imp.forward : imp.reverse);
      }
    }
    uint32_t total_live = 0;
    for (uint32_t n = 0; n < imp.records; ++n) {
      const uint32_t s = imp.plan.segments[n];
      for (uint32_t off = SEG_HEADER;;) {
        Rec r;
        if (!ReadRec(s, off, r))
          return ImportAbort(), false;
        if (r.end)
          break;
        Entry e{}, found{};
        if (!r.valid || !r.live || !Load(s, off, r, e, true) ||
            !FindIn(fresh[0], e.strokes, e.length, found, 0, true) ||
            found.address != e.address ||
            !HasSlot(fresh[1], HashFlash(e.text_address, e.text_bytes),
                     SlotValue(HashFlash(e.text_address, e.text_bytes), s,
                               off)))
          return ImportAbort(), false;
        if (e.length > max_strokes)
          max_strokes = e.length;
        ++segs[s].live;
        ++total_live;
        off += r.size;
      }
    }
    for (int t = 0; t < 2; ++t)
      for (uint32_t i = 0; i < fresh[t].segs; ++i)
        if (!CountSlots(fresh[t].map[i]))
          return ImportAbort(), false;
    if (!total_live || SlotsUsedIn(fresh[0]) != total_live ||
        SlotsUsedIn(fresh[1]) != total_live)
      return ImportAbort(), false;
    State next = state;
    next.void_before = imp.plan.first_sequence;
    next.fwd_epoch = imp.plan.fwd_epoch;
    next.rev_epoch = imp.plan.rev_epoch;
    next.fwd_segments = static_cast<uint16_t>(imp.forward);
    next.rev_segments = static_cast<uint16_t>(imp.reverse);
    next.max_strokes = max_strokes;
    next.import_first = next.import_count = 0;
    if (!AppendState(next))
      return ImportAbort(), false;
    for (uint32_t s = 0; s < nseg; ++s) {
      if (segs[s].pending) {
        segs[s].pending = segs[s].reserved = false;
        segs[s].counted = true;
      } else if (segs[s].kind == RECORD || segs[s].kind == FORWARD ||
                 segs[s].kind == REVERSE) {
        Retire(s);
      }
    }
    tables[0] = fresh[0];
    tables[1] = fresh[1];
    head = -1; // The next add opens a fresh segment.
    imp = {};
    rebuild_pending[0] = rebuild_pending[1] = false;
    table_stuck[0] = table_stuck[1] = false;
    stats_ready = true;
    return true;
  }
  void ImportAbort() {
    if (!imp.active)
      return;
    for (uint32_t n = 0; n < imp.plan.total; ++n) {
      const uint32_t s = imp.plan.segments[n];
      if (n < imp.next)
        Retire(s);
      segs[s] = {};
    }
    imp = {};
    State next = state;
    next.import_first = next.import_count = 0;
    AppendState(next);
  }

  // Read-only consistency check of every record.
  bool Verify(VerifyReport &report) {
    report = {};
    if (!opened)
      return false;
    for (uint32_t s = 0; s < nseg; ++s) {
      if (!IsRecord(s))
        continue;
      for (uint32_t off = SEG_HEADER;;) {
        Rec r;
        if (!ReadRec(s, off, r))
          return false;
        if (r.end || !r.valid)
          break;
        if (r.live) {
          Entry e{}, found{};
          if (!Load(s, off, r, e, true)) {
            ++report.bad_crc;
          } else {
            ++report.live;
            const uint32_t th = HashFlash(e.text_address, e.text_bytes);
            if (!FindIn(tables[0], e.strokes, e.length, found, 0, false) ||
                found.address != e.address ||
                !HasSlot(tables[1], th, SlotValue(th, s, off)))
              ++report.unreachable;
          }
        }
        off += r.size;
      }
    }
    return true;
  }

private:
  struct SegInfo {
    uint8_t kind = FREE, index = 0;
    uint16_t epoch = 0;
    uint32_t sequence = 0;
    uint32_t dead = 0;   // record segments: bytes in dead records
    uint16_t live = 0;   // record segments: live records; tables: used slots
    bool counted = false, pending = false, reserved = false;
  };
  struct Table {
    uint8_t map[MAX_TABLE_SEGS]{};
    uint16_t segs = 0;
  };
  struct Rec {
    uint32_t word = 0, strokes = 0, text = 0, size = 0;
    bool end = false, valid = false, committed = false, live = false;
  };
  struct Rebuild {
    bool active = false;
    int table = 0;
    uint16_t epoch = 0;
    Table fresh;
    uint8_t order[SEG_COUNT]{};
    uint32_t count = 0, position = 0, offset = SEG_HEADER;
  };
  struct Clean {
    int seg = -1;
    uint32_t offset = SEG_HEADER;
  };
  struct Import {
    bool active = false;
    uint32_t records = 0, forward = 0, reverse = 0, next = 0, offset = 0;
    ImportPlan plan;
  };

  static bool Newer(uint32_t a, uint32_t b) {
    return static_cast<int32_t>(a - b) > 0;
  }
  uint32_t Base(uint32_t seg) const { return start + seg * SEG_BYTES; }
  bool IsRecord(uint32_t s) const {
    return segs[s].kind == RECORD && !segs[s].pending;
  }
  static uint32_t SlotValue(uint32_t hash, uint32_t seg, uint32_t off) {
    return (hash >> 22 << 22) | (seg << 14) | (off >> 2);
  }

  // ---- low-level flash helpers ----
  bool ProgramVerified(uint32_t address, const void *data, size_t size) {
    const auto *bytes = static_cast<const uint8_t *>(data);
    while (size) {
      size_t n = 256 - (address & 255u);
      if (n > size)
        n = size;
      if (!device.Program(address, bytes, n))
        return false;
      uint8_t check[64];
      for (size_t off = 0; off < n;) {
        const size_t m = n - off > sizeof(check) ? sizeof(check) : n - off;
        if (!device.Read(address + off, check, m) ||
            memcmp(check, bytes + off, m))
          return false;
        off += m;
      }
      address += n;
      bytes += n;
      size -= n;
    }
    return true;
  }
  bool SameText(uint32_t address, const char *text, uint32_t n) {
    char chunk[64];
    for (uint32_t off = 0; off < n;) {
      const uint32_t m = n - off > sizeof(chunk) ? sizeof(chunk) : n - off;
      if (!device.Read(address + off, chunk, m) || memcmp(chunk, text + off, m))
        return false;
      off += m;
    }
    return true;
  }
  uint32_t HashFlash(uint32_t address, uint32_t n) {
    uint32_t h = 2166136261u;
    uint8_t chunk[64];
    for (uint32_t off = 0; off < n;) {
      const uint32_t m = n - off > sizeof(chunk) ? sizeof(chunk) : n - off;
      if (!device.Read(address + off, chunk, m))
        return 0;
      h = hash_more(h, chunk, m);
      off += m;
    }
    return h;
  }

  // ---- segments ----
  int Allocate(uint8_t kind, uint8_t index, uint16_t epoch, bool use_reserve) {
    if (!use_reserve && FreeSegments() <= RESERVE)
      return -1;
    for (uint32_t n = 0; n < nseg; ++n) {
      const uint32_t s = (alloc_cursor + n) % nseg;
      if (segs[s].kind != FREE || segs[s].reserved)
        continue;
      alloc_cursor = (s + 1) % nseg;
      SegHeader h{MAGIC, kind, index, epoch, next_seq, 0};
      h.crc = crc32(0, &h, 12);
      if (!device.EraseBlock(Base(s)) ||
          !ProgramVerified(Base(s), &h, sizeof(h)))
        return -1;
      segs[s] = {};
      segs[s].kind = kind;
      segs[s].index = index;
      segs[s].epoch = epoch;
      segs[s].sequence = next_seq++;
      segs[s].counted = true;
      return static_cast<int>(s);
    }
    return -1;
  }
  // Kill the header first so a half-finished erase can never resurrect
  // dead records. The block itself is erased when it is next allocated.
  bool Retire(uint32_t s) {
    const uint32_t zero = 0;
    const bool ok = device.Program(Base(s), &zero, sizeof(zero));
    if (static_cast<int>(s) == head)
      head = -1;
    if (static_cast<int>(s) == cl.seg)
      cl = {};
    const bool reserved = segs[s].reserved;
    segs[s] = {};
    segs[s].reserved = reserved;
    return ok;
  }
  uint32_t Order(uint8_t *order) const {
    uint32_t count = 0;
    for (uint32_t s = 0; s < nseg; ++s) {
      if (!IsRecord(s))
        continue;
      uint32_t i = count++;
      for (; i && Newer(segs[order[i - 1]].sequence, segs[s].sequence); --i)
        order[i] = order[i - 1];
      order[i] = static_cast<uint8_t>(s);
    }
    return count;
  }

  // ---- records ----
  bool ReadRec(uint32_t seg, uint32_t off, Rec &r) {
    r = {};
    if (off + 8 > SEG_BYTES) {
      r.end = true;
      return true;
    }
    if (!device.Read(Base(seg) + off, &r.word, 4))
      return false;
    if (r.word == EMPTY) {
      r.end = true;
      return true;
    }
    r.strokes = ((r.word >> 24) & 31u) + 1;
    r.text = (r.word >> 12) & 0xfffu;
    r.size = record_bytes(r.strokes, r.text);
    r.valid = !(r.word & F_ALLOC) && r.strokes <= MAX_STROKES &&
              r.text <= MAX_TEXT &&
              (r.word & 0xfffu) == size_check(r.strokes, r.text) &&
              r.size <= SEG_BYTES - off;
    r.committed = r.valid && !(r.word & F_COMMIT);
    r.live = r.committed && (r.word & F_DEAD);
    return true;
  }
  bool Load(uint32_t seg, uint32_t off, const Rec &r, Entry &e, bool check) {
    const uint32_t base = Base(seg) + off;
    uint32_t stored = 0;
    e.length = r.strokes;
    e.text_bytes = r.text;
    e.text = nullptr;
    e.address = base;
    e.text_address = base + 8 + 4 * r.strokes;
    if (!device.Read(base + 4, &stored, 4) ||
        !device.Read(base + 8, e.strokes, 4 * r.strokes))
      return false;
    if (!check)
      return true;
    uint32_t crc = crc32(segs[seg].sequence, e.strokes, 4 * r.strokes);
    uint8_t chunk[64];
    for (uint32_t done = 0; done < r.text;) {
      const uint32_t m = r.text - done > sizeof(chunk) ? sizeof(chunk)
                                                        : r.text - done;
      if (!device.Read(e.text_address + done, chunk, m))
        return false;
      crc = crc32(crc, chunk, m);
      done += m;
    }
    if (crc != stored) {
      ++integrity_errors;
      return false;
    }
    return true;
  }
  // Decodes a slot and reads the record header it points at.
  bool Target(uint32_t v, bool pending, uint32_t &seg, uint32_t &off, Rec &r) {
    seg = (v >> 14) & 0xffu;
    off = (v & 0x3fffu) << 2;
    if (seg >= nseg || segs[seg].kind != RECORD ||
        segs[seg].pending != pending || off < SEG_HEADER)
      return false;
    return ReadRec(seg, off, r) && !r.end && r.live;
  }
  bool MarkDead(uint32_t address) {
    const uint32_t seg = (address - start) / SEG_BYTES;
    uint32_t word = 0;
    if (!device.Read(address, &word, 4))
      return false;
    if (!(word & F_DEAD))
      return true;
    word &= ~F_DEAD;
    if (!ProgramVerified(address, &word, 4))
      return false;
    segs[seg].dead += record_bytes(((word >> 24) & 31u) + 1,
                                   (word >> 12) & 0xfffu);
    if (segs[seg].live)
      --segs[seg].live;
    return true;
  }
  // Finds the segment tail and the last committed record in it.
  bool WalkTail(uint32_t seg, uint32_t &tail, uint32_t &last, bool &have) {
    have = false;
    for (uint32_t off = SEG_HEADER;;) {
      Rec r;
      if (!ReadRec(seg, off, r))
        return false;
      if (r.end) {
        tail = off;
        return true;
      }
      if (!r.valid) {
        tail = SEG_BYTES; // Torn header: the rest is unusable.
        return true;
      }
      if (r.committed) {
        last = off;
        have = true;
      }
      off += r.size;
    }
  }

  // ---- tables ----
  bool ReadSlot(const Table &t, uint32_t i, uint32_t &v) {
    return device.Read(Base(t.map[i / SLOTS_PER_SEG]) + SEG_HEADER +
                           4 * (i % SLOTS_PER_SEG),
                       &v, 4);
  }
  bool WriteSlot(const Table &t, uint32_t i, uint32_t v) {
    const uint32_t s = t.map[i / SLOTS_PER_SEG];
    if (!ProgramVerified(Base(s) + SEG_HEADER + 4 * (i % SLOTS_PER_SEG), &v, 4))
      return false;
    ++segs[s].live;
    return true;
  }
  uint32_t SlotsUsedIn(const Table &t) const {
    uint32_t used = 0;
    for (uint32_t i = 0; i < t.segs; ++i)
      used += segs[t.map[i]].live;
    return used;
  }
  bool FindEmpty(const Table &t, uint32_t hash, uint32_t &index,
                 uint32_t &probes) {
    const uint32_t slots = t.segs * SLOTS_PER_SEG;
    if (!slots)
      return false;
    index = hash % slots;
    for (probes = 0; probes < slots; ++probes, index = (index + 1) % slots) {
      uint32_t v;
      if (!ReadSlot(t, index, v))
        return false;
      if (v == EMPTY)
        return true;
    }
    return false;
  }
  bool HasSlot(const Table &t, uint32_t hash, uint32_t value) {
    const uint32_t slots = t.segs * SLOTS_PER_SEG;
    if (!slots)
      return false;
    uint32_t i = hash % slots;
    for (uint32_t n = 0; n < slots; ++n, i = (i + 1) % slots) {
      uint32_t v;
      if (!ReadSlot(t, i, v) || v == EMPTY)
        return false;
      if (v == value)
        return true;
    }
    return false;
  }
  bool FindIn(const Table &t, const uint32_t *strokes, uint32_t length,
              Entry &out, uint32_t exclude, bool pending) {
    const uint32_t slots = t.segs * SLOTS_PER_SEG;
    if (!slots || !length || length > MAX_STROKES)
      return false;
    const uint32_t hash = hash_bytes(strokes, 4 * length);
    uint32_t i = hash % slots;
    for (uint32_t n = 0; n < slots; ++n, i = (i + 1) % slots) {
      uint32_t v, seg, off;
      Rec r;
      if (!ReadSlot(t, i, v) || v == EMPTY)
        return false;
      if ((v >> 22) != (hash >> 22) || !Target(v, pending, seg, off, r) ||
          r.strokes != length || Base(seg) + off == exclude)
        continue;
      uint32_t stored[MAX_STROKES];
      if (!device.Read(Base(seg) + off + 8, stored, 4 * length) ||
          memcmp(stored, strokes, 4 * length))
        continue;
      if (Load(seg, off, r, out, true))
        return true;
    }
    return false;
  }
  void CollectTable(int t) {
    const uint8_t kind = t ? REVERSE : FORWARD;
    const uint16_t epoch = t ? state.rev_epoch : state.fwd_epoch;
    const uint32_t want = t ? state.rev_segments : state.fwd_segments;
    bool have[MAX_TABLE_SEGS]{};
    tables[t] = {};
    for (uint32_t s = 0; s < nseg; ++s)
      if (segs[s].kind == kind && segs[s].epoch == epoch &&
          segs[s].index < want && segs[s].index < MAX_TABLE_SEGS) {
        tables[t].map[segs[s].index] = static_cast<uint8_t>(s);
        have[segs[s].index] = true;
      }
    bool complete = want && want <= MAX_TABLE_SEGS;
    for (uint32_t i = 0; i < want && complete; ++i)
      complete = have[i];
    tables[t].segs = complete ? static_cast<uint16_t>(want) : 0;
  }
  bool CountSlots(uint32_t s) {
    uint32_t used = 0, chunk[64];
    for (uint32_t off = SEG_HEADER; off < SEG_BYTES;) {
      const uint32_t m = SEG_BYTES - off > sizeof(chunk) ? sizeof(chunk)
                                                          : SEG_BYTES - off;
      if (!device.Read(Base(s) + off, chunk, m))
        return false;
      for (uint32_t i = 0; i < m / 4; ++i)
        used += chunk[i] != EMPTY;
      off += m;
    }
    segs[s].live = static_cast<uint16_t>(used);
    segs[s].counted = true;
    return true;
  }

  // ---- meta log ----
  static uint32_t StateCrc(State s) {
    s.flags = 0;
    return crc32(0, &s, STATE_BYTES - 4);
  }
  bool ReadMeta(uint32_t seg) {
    bool found = false;
    uint32_t off = SEG_HEADER;
    for (; off + STATE_BYTES <= SEG_BYTES; off += STATE_BYTES) {
      State s{};
      if (!device.Read(Base(seg) + off, &s, sizeof(s)))
        return false;
      if (s.flags == EMPTY)
        break;
      if (s.flags == ~(F_ALLOC | F_COMMIT) && s.crc == StateCrc(s) &&
          s.fwd_segments <= MAX_TABLE_SEGS &&
          s.rev_segments <= MAX_TABLE_SEGS && s.max_strokes <= MAX_STROKES) {
        state = s;
        found = true;
      }
    }
    meta_off = off;
    return found;
  }
  bool AppendState(State next) {
    next.flags = ~F_ALLOC;
    memset(next.reserved, 0, sizeof(next.reserved));
    next.crc = StateCrc(next);
    const uint32_t committed = ~(F_ALLOC | F_COMMIT);
    if (meta < 0 || meta_off + STATE_BYTES > SEG_BYTES) {
      const int fresh = Allocate(META, 0, 0, true);
      if (fresh < 0)
        return false;
      const uint32_t at = Base(static_cast<uint32_t>(fresh)) + SEG_HEADER;
      if (!ProgramVerified(at, &next, sizeof(next)) ||
          !ProgramVerified(at, &committed, 4))
        return false;
      const int old = meta;
      meta = fresh;
      meta_off = SEG_HEADER + STATE_BYTES;
      if (old >= 0)
        Retire(static_cast<uint32_t>(old));
    } else {
      const uint32_t at = Base(static_cast<uint32_t>(meta)) + meta_off;
      meta_off += STATE_BYTES;
      if (!ProgramVerified(at, &next, sizeof(next)) ||
          !ProgramVerified(at, &committed, 4))
        return false;
    }
    next.flags = committed;
    state = next;
    return true;
  }
  bool Format() {
    for (uint32_t s = 0; s < nseg; ++s)
      if (segs[s].kind != FREE && !Retire(s))
        return false;
    State next{};
    next.void_before = next_seq;
    next.fwd_epoch = next.rev_epoch = 1;
    next.fwd_segments = next.rev_segments = 1;
    if (Allocate(FORWARD, 0, 1, true) < 0 || Allocate(REVERSE, 0, 1, true) < 0)
      return false;
    meta = -1;
    return AppendState(next);
  }
  // Records exist but no usable state: keep every record, rebuild the rest.
  bool Recover() {
    State next{};
    next.max_strokes = MAX_STROKES;
    for (uint32_t s = 0; s < nseg; ++s) {
      if (segs[s].kind == FORWARD && segs[s].epoch >= next.fwd_epoch)
        next.fwd_epoch = segs[s].epoch;
      if (segs[s].kind == REVERSE && segs[s].epoch >= next.rev_epoch)
        next.rev_epoch = segs[s].epoch;
      if (segs[s].kind != FREE && segs[s].kind != RECORD && !Retire(s))
        return false;
    }
    ++next.fwd_epoch;
    ++next.rev_epoch;
    meta = -1;
    return AppendState(next);
  }

  // ---- add / move ----
  bool EnsureHead(uint32_t size, bool cleaning) {
    if (head >= 0 && head_off + size <= SEG_BYTES)
      return true;
    if (!cleaning) {
      while (FreeSegments() <= RESERVE) {
        while (!stats_ready)
          StatsSlice();
        if (Victim() < 0 || !CleanSlice())
          break;
      }
      // Cleaning may have opened a head with room.
      if (head >= 0 && head_off + size <= SEG_BYTES)
        return true;
    }
    const int s = Allocate(RECORD, 0, 0, cleaning);
    if (s < 0)
      return false;
    head = s;
    head_off = SEG_HEADER;
    return true;
  }
  // Appends a record, retires the one it replaces, and links it. `text` is
  // RAM text, or null to copy `text_bytes` from flash at `text_address`.
  Result Insert(const uint32_t *strokes, uint32_t length, const char *text,
                uint32_t text_address, uint32_t text_bytes, const Entry *old,
                bool cleaning) {
    const uint32_t size = record_bytes(length, text_bytes);
    const uint32_t hashes[2] = {
        hash_bytes(strokes, 4 * length),
        text ? hash_bytes(text, text_bytes)
             : HashFlash(text_address, text_bytes)};
    // Reserve a slot in each table before anything is committed.
    uint32_t index[2], probes[2];
    for (int t = 0; t < 2; ++t)
      if (!FindEmpty(tables[t], hashes[t], index[t], probes[t])) {
        if (table_stuck[t])
          return Result::FULL;
        rebuild_pending[t] = true;
        return Result::BUSY;
      }
    if (length > state.max_strokes) {
      State next = state;
      next.max_strokes = length;
      if (!AppendState(next))
        return Result::FAILED;
    }
    if (!EnsureHead(size, cleaning))
      return Result::FULL;
    // Cleaning inside EnsureHead may have used the reserved slots and moved
    // the record being replaced: look both up again.
    Entry moved{};
    if (!cleaning) {
      for (int t = 0; t < 2; ++t)
        if (!FindEmpty(tables[t], hashes[t], index[t], probes[t]))
          return Result::BUSY;
      old = FindIn(tables[0], strokes, length, moved, 0, false) ? &moved
                                                                 : nullptr;
    }
    const uint32_t seg = static_cast<uint32_t>(head), off = head_off;
    const uint32_t at = Base(seg) + off;
    // Only now: cleaning inside EnsureHead shares this buffer.
    auto *words = reinterpret_cast<uint32_t *>(scratch);
    memset(scratch, 0, size);
    memcpy(scratch + 8, strokes, 4 * length);
    if (text)
      memcpy(scratch + 8 + 4 * length, text, text_bytes);
    else if (text_bytes &&
             !device.Read(text_address, scratch + 8 + 4 * length, text_bytes))
      return Result::FAILED;
    words[0] = (~F_ALLOC & ~(31u << 24) & ~0xffffffu) | ((length - 1) << 24) |
               (text_bytes << 12) | size_check(length, text_bytes);
    words[1] = crc32(segs[seg].sequence, scratch + 8,
                     4 * length + text_bytes);
    head_off += size;
    if (!ProgramVerified(at, scratch, 4) ||
        !ProgramVerified(at + 4, scratch + 4, size - 4))
      return Result::FAILED;
    words[0] &= ~F_COMMIT;
    if (!ProgramVerified(at, scratch, 4)) // commit point
      return Result::FAILED;
    ++segs[seg].live;
    if (old && !MarkDead(old->address))
      return Result::FAILED;
    for (int t = 0; t < 2; ++t) {
      if (!WriteSlot(tables[t], index[t], SlotValue(hashes[t], seg, off)))
        return Result::FAILED;
      const uint32_t slots = tables[t].segs * SLOTS_PER_SEG;
      if (probes[t] > 256 || SlotsUsedIn(tables[t]) > slots / 8 * 7)
        rebuild_pending[t] = !table_stuck[t];
    }
    return Result::OK;
  }
  // Finishes whatever the last committed record's writer was doing.
  bool Replay(uint32_t seg, uint32_t off) {
    Rec r;
    Entry e{};
    if (!ReadRec(seg, off, r) || r.end || !r.live || !Load(seg, off, r, e, true))
      return true;
    Entry other{};
    while (FindIn(tables[0], e.strokes, e.length, other, e.address, false))
      if (!MarkDead(other.address))
        return false;
    const uint32_t hashes[2] = {hash_bytes(e.strokes, 4 * e.length),
                                HashFlash(e.text_address, e.text_bytes)};
    for (int t = 0; t < 2; ++t) {
      const uint32_t value = SlotValue(hashes[t], seg, off);
      uint32_t index, probes;
      if (!tables[t].segs || HasSlot(tables[t], hashes[t], value))
        continue;
      if (!FindEmpty(tables[t], hashes[t], index, probes)) {
        rebuild_pending[t] = true;
        continue;
      }
      if (!WriteSlot(tables[t], index, value))
        return false;
    }
    return true;
  }

  // ---- statistics ----
  void StatsSlice() {
    for (uint32_t s = 0; s < nseg; ++s) {
      if (segs[s].counted || segs[s].kind == FREE || segs[s].kind == META ||
          segs[s].pending)
        continue;
      if (segs[s].kind != RECORD) {
        if (!CountSlots(s))
          segs[s].counted = true;
        return;
      }
      uint32_t live = 0, dead = 0;
      for (uint32_t off = SEG_HEADER;;) {
        Rec r;
        if (!ReadRec(s, off, r) || r.end || !r.valid)
          break;
        if (r.live)
          ++live;
        else
          dead += r.size;
        off += r.size;
      }
      segs[s].live = static_cast<uint16_t>(live);
      segs[s].dead = dead;
      segs[s].counted = true;
      return;
    }
    stats_ready = true;
  }

  // ---- cleaning ----
  int Victim() const {
    int best = -1;
    for (uint32_t s = 0; s < nseg; ++s)
      if (IsRecord(s) && static_cast<int>(s) != head && segs[s].dead &&
          (best < 0 || segs[s].dead > segs[best].dead))
        best = static_cast<int>(s);
    return best;
  }
  int VictimWorthIt() const {
    const int v = Victim();
    return v >= 0 && (segs[v].dead >= 4096 || FreeSegments() <= RESERVE + 1)
               ? v
               : -1;
  }
  // Moves up to 32 live records out of the victim. False if nothing to do.
  bool CleanSlice() {
    if (cl.seg < 0) {
      cl.seg = Victim();
      cl.offset = SEG_HEADER;
      if (cl.seg < 0)
        return false;
    }
    const uint32_t seg = static_cast<uint32_t>(cl.seg);
    for (int moved = 0; moved < 32;) {
      Rec r;
      if (!ReadRec(seg, cl.offset, r))
        return false;
      if (r.end || !r.valid) {
        Retire(seg);
        return true;
      }
      const uint32_t off = cl.offset;
      if (r.live) {
        Entry e{};
        if (!Load(seg, off, r, e, true)) {
          if (!MarkDead(Base(seg) + off))
            return false;
        } else {
          const Result res = Insert(e.strokes, e.length, nullptr,
                                    e.text_address, e.text_bytes, &e, true);
          if (res != Result::OK)
            return false; // Table needs a rebuild first, or flash failed.
          ++moved;
        }
      }
      cl.offset = off + r.size;
    }
    return true;
  }

  // ---- table rebuild ----
  bool StartRebuild(int t) {
    const uint32_t live = Entries();
    const uint32_t room =
        FreeSegments() > RESERVE ? FreeSegments() - RESERVE : 0;
    uint32_t want = (live * 2 + SLOTS_PER_SEG - 1) / SLOTS_PER_SEG;
    const uint32_t tight = (live * 4 / 3 + SLOTS_PER_SEG) / SLOTS_PER_SEG;
    if (want < 1)
      want = 1;
    if (want > MAX_TABLE_SEGS)
      want = MAX_TABLE_SEGS;
    if (want > room)
      want = tight > room ? 0 : room;
    // A full table blocks cleaning, and cleaning is what frees segments, so a
    // minimum-size rebuild may borrow the cleaning reserve. One segment stays
    // back for a meta rollover; the old table's segments return on commit.
    const bool borrow = !want && tight < FreeSegments();
    if (borrow)
      want = tight;
    if (!want || tight > MAX_TABLE_SEGS)
      return false;
    rb = {};
    rb.table = t;
    rb.epoch =
        static_cast<uint16_t>((t ? state.rev_epoch : state.fwd_epoch) + 1);
    for (uint32_t i = 0; i < want; ++i) {
      const int s = Allocate(t ? REVERSE : FORWARD, static_cast<uint8_t>(i),
                             rb.epoch, borrow);
      if (s < 0) {
        for (uint32_t j = 0; j < i; ++j)
          Retire(rb.fresh.map[j]);
        rb = {};
        return false;
      }
      segs[s].pending = true; // Not the current table until committed.
      rb.fresh.map[i] = static_cast<uint8_t>(s);
    }
    rb.fresh.segs = static_cast<uint16_t>(want);
    rb.count = Order(rb.order);
    rb.active = true;
    cl = {};
    return true;
  }
  void AbortRebuild() {
    for (uint32_t i = 0; i < rb.fresh.segs; ++i)
      Retire(rb.fresh.map[i]);
    table_stuck[rb.table] = true;
    rebuild_pending[rb.table] = false;
    rb = {};
  }
  void RebuildSlice() {
    const int t = rb.table;
    for (int done = 0; done < 64 && rb.position < rb.count;) {
      const uint32_t seg = rb.order[rb.position];
      Rec r;
      if (!IsRecord(seg) || !ReadRec(seg, rb.offset, r) || r.end || !r.valid) {
        ++rb.position;
        rb.offset = SEG_HEADER;
        continue;
      }
      const uint32_t off = rb.offset;
      rb.offset += r.size;
      if (!r.live)
        continue;
      ++done;
      Entry e{}, other{};
      if (!Load(seg, off, r, e, true)) {
        MarkDead(Base(seg) + off);
        continue;
      }
      uint32_t hash, index, probes;
      if (t == 0) {
        hash = hash_bytes(e.strokes, 4 * e.length);
        // Records are visited oldest first: a duplicate outline already in
        // the new table is the older copy.
        while (FindInFresh(e, other))
          if (!MarkDead(other.address))
            return AbortRebuild();
      } else {
        hash = HashFlash(e.text_address, e.text_bytes);
      }
      if (!FindEmpty(rb.fresh, hash, index, probes) ||
          !WriteSlot(rb.fresh, index, SlotValue(hash, seg, off)))
        return AbortRebuild();
    }
    if (rb.position < rb.count)
      return;
    State next = state;
    if (t == 0) {
      next.fwd_epoch = rb.epoch;
      next.fwd_segments = rb.fresh.segs;
    } else {
      next.rev_epoch = rb.epoch;
      next.rev_segments = rb.fresh.segs;
    }
    if (!AppendState(next)) // commit point
      return AbortRebuild();
    for (uint32_t i = 0; i < tables[t].segs; ++i)
      Retire(tables[t].map[i]);
    for (uint32_t i = 0; i < rb.fresh.segs; ++i)
      segs[rb.fresh.map[i]].pending = false;
    tables[t] = rb.fresh;
    rebuild_pending[t] = false;
    table_stuck[t] = false;
    rb = {};
  }
  bool FindInFresh(const Entry &e, Entry &other) {
    return FindIn(rb.fresh, e.strokes, e.length, other, e.address, false);
  }

  Device &device;
  const uint32_t start, nseg;
  SegInfo segs[SEG_COUNT];
  Table tables[2];
  State state{};
  Rebuild rb;
  Clean cl;
  Import imp;
  int head = -1, meta = -1;
  uint32_t head_off = SEG_HEADER, meta_off = SEG_HEADER, next_seq = 1,
           alloc_cursor = 0, integrity_errors = 0;
  bool opened = false, stats_ready = false;
  bool rebuild_pending[2]{}, table_stuck[2]{};
  alignas(4) uint8_t scratch[8 + 4 * MAX_STROKES + 2048];
};
} // namespace squid_user
