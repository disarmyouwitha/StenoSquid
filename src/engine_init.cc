#include "zmk_javelin_steno/zmk_platform_shim.h"

#include "console.h"
#include "button_script_manager.h"
#include "container/list.h"
#include "dictionary/dictionary_list.h"
#include "engine.h"
#include "orthography.h"
#include "english_orthography.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
#include "host_layout.h"
#include "platform/macos_us_layout.h"
#include "platform/windows_us_layout.h"
#endif
#include "processor/all_up.h"
#include "processor/jeff_modifiers.h"
#include "processor/processor.h"
#include "processor/repeat.h"
#include "steno_key_state.h"
#include "static_allocate.h"
#include "stroke.h"
#if defined(CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS) || defined(CONFIG_ZMK_STENO_SQUID_PRACTICE) || defined(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
#include "steno_squid/split_status.h"
#endif
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
#include "steno_squid/practice_engine.h"
#endif
#include <new>

#include "javelin_engine_lock.h"
#include "engine_work.h"
#include "chord_delivery.h"
#include "javelin_hid_transport.h"
#include "lapwing_dictionary.h"
#include "lapwing_flash.h"
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include "user_dictionary_store.h"
#endif

#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/logging/log.h>
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include <zephyr/settings/settings.h>
#include <errno.h>
#endif

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

static JavelinStaticAllocate<StenoCompiledOrthography> compiledOrthography;
static JavelinStaticAllocate<StenoDictionaryList> dictionaryList;
static JavelinStaticAllocate<StenoJeffModifiers> jeffModifiers;
static JavelinStaticAllocate<StenoRepeat> stenoRepeat;
static JavelinStaticAllocate<StenoAllUp> allUp;
static StenoProcessor *processor = nullptr;

static atomic_t initialized;

// Dictionary selections are stored separately from base images and personal
// snapshots. A fresh keyboard starts with Lapwing. Personal=1, Lapwing=2,
// Plover=4, then the Lapwing extras from bit 3 up.
#define EXTRAS_SELECTION_SHIFT 3
static constexpr uint8_t PERSONAL_SELECTION = 1;
static constexpr uint8_t LAPWING_SELECTION = 2;
static constexpr uint8_t PLOVER_SELECTION = 4;
static_assert(EXTRAS_SELECTION_SHIFT + EXTRAS_DICTIONARY_COUNT <= 7);
#define EXTRAS_SELECTION_BIT(index) (uint8_t(1) << (EXTRAS_SELECTION_SHIFT + (index)))
// Commands, numbers and proper nouns start on. UK additions start off.
#define EXTRAS_SELECTION_DEFAULT                                                                   \
  (EXTRAS_SELECTION_BIT(EXTRAS_COMMANDS) | EXTRAS_SELECTION_BIT(EXTRAS_NUMBERS) |                  \
   EXTRAS_SELECTION_BIT(EXTRAS_PROPER_NOUNS))
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
static constexpr uint8_t DICTIONARY_SELECTION_VERSION = 2;
static uint8_t dictionary_selection = 3 | EXTRAS_SELECTION_DEFAULT;
static bool dictionary_enabled(uint8_t bit, bool) { return dictionary_selection & bit; }
#else
// Without the browser tool nothing can change the selection.
static bool dictionary_enabled(uint8_t, bool fallback) { return fallback; }
#endif

#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
bool steno_squid_engine_cancel_strokes(StenoKeyState &held) {
  if (!processor) {
    return false;
  }
  held = processor->GetCurrentKeyState();
  stenoRepeat.value.Process(StenoKeyState(), StenoAction::CANCEL_ALL);
  new (processor) StenoProcessor(stenoRepeat.value);
  return true;
}
#endif

#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
// These indexes are stored in ZMK settings.
static constexpr uint8_t HOST_PROFILE_MAC = 0;
static constexpr uint8_t HOST_PROFILE_WINDOWS = 1;
static constexpr const char *host_profiles[] = {"Mac U.S.", "Windows U.S. (plain text)"};
static uint8_t host_profile = HOST_PROFILE_WINDOWS;

static void apply_host_profile() {
  HostLayouts::SetActiveLayout(host_profile == HOST_PROFILE_MAC ? zmk_javelin_macos_us_layout()
                                                               : zmk_javelin_windows_us_layout());
}

static void get_host_parameter(void *, const char *line) {
  const char *name = strchr(line, ' ');
  if (name && strcmp(name + 1, "available_host_layouts") == 0) {
    Console::Printf("[\"%s\",\"%s\"]\n\n", host_profiles[0], host_profiles[1]);
  } else if (name && strcmp(name + 1, "host_layout") == 0) {
    Console::Printf("%s\n\n", host_profiles[host_profile]);
  } else {
    Console::Printf("ERR Unknown parameter\n\n");
  }
}

static void set_host_profile(void *, const char *line) {
  const char *name = strchr(line, ' ');
  for (uint8_t profile = 0; name && profile < ARRAY_SIZE(host_profiles); ++profile) {
    if (strcmp(name + 1, host_profiles[profile]) != 0) {
      continue;
    }
    if (profile != host_profile) {
      if (settings_save_one("javelin/host_profile", &profile, sizeof(profile)) != 0) {
        Console::Printf("ERR Unable to save host layout\n\n");
        return;
      }
      host_profile = profile;
      apply_host_profile();
    }
    Console::SendOk();
    return;
  }
  Console::Printf("ERR Unknown host layout\n\n");
}
#endif

#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#include "steno_squid/engine_settings.h"

SETTINGS_STATIC_HANDLER_DEFINE(javelin, "javelin", nullptr, javelin_settings_set,
                               nullptr, nullptr);

static void select_dictionary(void *, const char *line) {
  const char *name = strchr(line, ' ');
  if (!name) {
    Console::Printf("ERR No dictionary specified\n\n");
    return;
  }
  ++name;
  uint8_t bit = 0;
  if (strcmp(name, "user_dictionary") == 0) {
    bit = PERSONAL_SELECTION;
  } else if (strcmp(name, "Lapwing Base") == 0) {
    bit = LAPWING_SELECTION;
  } else if (strcmp(name, "Plover Base") == 0 && plover_dictionary()) {
    bit = PLOVER_SELECTION;
  }
  for (size_t i = 0; !bit && i < EXTRAS_DICTIONARY_COUNT; i++) {
    if (extras_dictionary(i) && strcmp(name, extras_dictionary_name(i)) == 0) {
      bit = EXTRAS_SELECTION_BIT(i);
    }
  }
  if (!bit) {
    Console::Printf("ERR Unknown dictionary\n\n");
    return;
  }
  uint8_t selected = dictionary_selection;
  if (strncmp(line, "enable_dictionary ", 18) == 0) {
    selected |= bit;
  } else if (strncmp(line, "disable_dictionary ", 19) == 0) {
    selected &= ~bit;
  } else {
    selected ^= bit;
  }
  // Persist before acknowledging or changing the active engine. On failure the
  // previous selection remains active and the browser receives an error.
  const uint8_t record[] = {DICTIONARY_SELECTION_VERSION, selected};
  if (selected != dictionary_selection &&
      settings_save_one("javelin/dictionaries", record, sizeof(record)) != 0) {
    Console::Printf("ERR Unable to save dictionary selection\n\n");
    return;
  }
  dictionary_selection = selected;
  BufferWriter events;
  ConsoleWriter::Push(&events);
  if (selected & bit) {
    StenoEngine::container.value.EnableDictionary(name);
  } else {
    StenoEngine::container.value.DisableDictionary(name);
  }
  ConsoleWriter::Pop();
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
  steno_squid_practice_dictionary_changed();
#endif
  Console::SendOk();
  events.WriteBufferTo(ConsoleWriter::GetActiveWriter());
}
#endif

static bool load_dictionary_collection() {
  List<StenoDictionaryListEntry> entries;
#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
  auto *personal = steno_squid_user_dictionary_open();
  if (personal) {
    entries.Add(StenoDictionaryListEntry(personal, dictionary_selection & PERSONAL_SELECTION));
  }
#endif
  // The Lapwing extras rank above the bases, in the order Plover gives them.
  for (size_t i = 0; i < EXTRAS_DICTIONARY_COUNT; i++) {
    if (auto *extra = extras_dictionary(i)) {
      entries.Add(StenoDictionaryListEntry(
          extra, dictionary_enabled(EXTRAS_SELECTION_BIT(i), i != EXTRAS_UK_ADDITIONS)));
    }
  }
  entries.Add(StenoDictionaryListEntry(lapwing_dictionary(), dictionary_enabled(LAPWING_SELECTION, true)));
  if (auto *plover = plover_dictionary()) {
    // Personal overrides remain first. Fresh settings default to Lapwing.
    entries.Add(StenoDictionaryListEntry(plover, dictionary_enabled(PLOVER_SELECTION, false)));
  }

  new (dictionaryList)
      StenoDictionaryList(static_cast<List<StenoDictionaryListEntry> &&>(entries));

  new (compiledOrthography) StenoCompiledOrthography(english_orthography);
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
  apply_host_profile();
#endif

  new (StenoEngine::container) StenoEngine(
      dictionaryList.value,
      nullptr,
      compiledOrthography.value,
      StenoStroke(StrokeMask::STAR),  // undo stroke = *
      nullptr);

#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
#define UPSTREAM_COMMAND(name)
#define ENGINE_COMMAND(name, help, handler) Console::instance.RegisterCommand(name, help, handler, nullptr);
#define USER_COMMAND(name, help, handler, usb_only)
#include "steno_squid/console_commands.def"
#undef UPSTREAM_COMMAND
#undef ENGINE_COMMAND
#undef USER_COMMAND
  steno_squid_user_dictionary_register(Console::instance, dictionaryList.value);
#endif
  StenoEngine::container.value.AddConsoleCommands(Console::instance);

  LOG_INF("Steno engine initialized");
  return true;
}

extern "C" {

void zmk_javelin_steno_init(void) {
  const JavelinEngineLock lock;
  if (!lock || atomic_get(&initialized)) {
    return;
  }
  if (!lapwing_dictionary()) {
    LOG_WRN("External Lapwing dictionary is unavailable");
    return;
  }

  // Construct the manager queried by the emitter even though ZMK handles keys.
  alignas(4) static const uint8_t empty_button_script[64] = {0x4a, 0x53, 0x53, 0x34};
  ButtonScriptManager::Initialize(empty_button_script);

  if (!load_dictionary_collection()) {
    LOG_WRN("Steno engine not started");
    return;
  }

  // Processor pipeline: Repeat → AllUp → JeffModifiers → Engine
  new (jeffModifiers) StenoJeffModifiers(StenoEngine::container.value);
  new (allUp) StenoAllUp(jeffModifiers.value);
  new (stenoRepeat) StenoRepeat(allUp.value);

  static StenoProcessor processorInstance(stenoRepeat.value);
  processor = &processorInstance;
  atomic_set(&initialized, 1);
  lapwing_flash_ready_changed();

  LOG_INF("Steno processor pipeline ready");
}

void zmk_javelin_steno_process_key(int steno_key_index, bool is_press) {
  // On contention discard the whole held chord, including its eventual releases.
  // A partial chord must never become a different word after the lock is freed.
  static StenoSquidChordDelivery delivery;
  delivery.Event(steno_key_index, is_press);
  const JavelinEngineLock lock;
  if (!lock || !processor) {
    delivery.Drop();
    zmk_javelin_steno_prepare();
#ifdef CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS
    if (atomic_get(&initialized) && lapwing_flash_status() == LAPWING_FLASH_READY)
      steno_squid_upload_status(STENO_SQUID_MODE_BUSY, 500);
#endif
    return;
  }
  if (delivery.interrupted) {
    stenoRepeat.value.Process(StenoKeyState(), StenoAction::CANCEL_ALL);
    new (processor) StenoProcessor(stenoRepeat.value);
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
    steno_squid_practice_discard_chord();
#endif
    if (!delivery.Recover()) return;
  }
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
  if (steno_squid_practice_process_key(steno_key_index, is_press)) {
    return;
  }
#endif
  LOG_DBG("[JAV] key %d %s", steno_key_index, is_press ? "down" : "up");
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_TAPE
  // Catch the engine's stroke events for the right display's stroke tape.
  steno_squid_stroke_feed_begin();
#endif
  processor->Process(static_cast<StenoKey>(steno_key_index), is_press);
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_TAPE
  steno_squid_stroke_feed_end();
#endif
}

} // extern "C"

#ifdef CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY
// This hook is installed and read only under JavelinEngineLock. It stops a
// large personal export as soon as its transport reports a terminal failure.
static bool (*response_cancelled)(void *);
static void *response_context;
bool steno_squid_javelin_response_active() { return response_context != nullptr; }
bool steno_squid_javelin_response_cancelled() {
  return response_cancelled && response_cancelled(response_context);
}

bool steno_squid_javelin_console_line(const char *line, size_t size, IWriter &writer,
                                      bool (*cancelled)(void *), void *context) {
  const JavelinEngineLock lock;
  if (!lock) {
    // The flash is asleep, unprovisioned or still verifying.
    return false;
  }
  if (!atomic_get(&initialized)) {
    zmk_javelin_steno_prepare();
    return false;
  }
  response_cancelled = cancelled;
  response_context = context;
  ConsoleWriter::Push(&writer);
  Console::instance.HandleInput(line, size, ConnectionId::SERIAL_CONSOLE);
  ConsoleWriter::Pop();
  response_cancelled = nullptr;
  response_context = nullptr;
  return true;
}
#endif

static void prepare_engine(struct k_work *) { zmk_javelin_steno_init(); }
K_WORK_DEFINE(prepare_engine_work, prepare_engine);
extern "C" void zmk_javelin_steno_prepare(void) {
  if (!atomic_get(&initialized))
    k_work_submit_to_queue(&steno_squid_engine_queue, &prepare_engine_work);
}
extern "C" bool zmk_javelin_steno_ready(void) { return atomic_get(&initialized) != 0; }
