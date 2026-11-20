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
#include "dictionary_benchmark.h"
#include "javelin_hid_transport.h"
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
#include "steno_squid/native_store_runtime.h"
#include "steno_squid/native_input.h"
#endif
#include "dictionary_flash.h"
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
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
static StenoSquidChordDelivery native_delivery;
#endif

#include <dt-bindings/zmk/javelin_steno_ids.h>

// Studio/keymap IDs and the engine must describe the same physical keys.
static_assert(STENO_ID_S1 == int(StenoKey::S1));
static_assert(STENO_ID_S2 == int(StenoKey::S2));
static_assert(STENO_ID_TL == int(StenoKey::TL));
static_assert(STENO_ID_KL == int(StenoKey::KL));
static_assert(STENO_ID_PL == int(StenoKey::PL));
static_assert(STENO_ID_WL == int(StenoKey::WL));
static_assert(STENO_ID_HL == int(StenoKey::HL));
static_assert(STENO_ID_RL == int(StenoKey::RL));
static_assert(STENO_ID_A == int(StenoKey::A));
static_assert(STENO_ID_O == int(StenoKey::O));
static_assert(STENO_ID_STAR1 == int(StenoKey::STAR1));
static_assert(STENO_ID_STAR2 == int(StenoKey::STAR2));
static_assert(STENO_ID_STAR3 == int(StenoKey::STAR3));
static_assert(STENO_ID_STAR4 == int(StenoKey::STAR4));
static_assert(STENO_ID_E == int(StenoKey::E));
static_assert(STENO_ID_U == int(StenoKey::U));
static_assert(STENO_ID_FR == int(StenoKey::FR));
static_assert(STENO_ID_RR == int(StenoKey::RR));
static_assert(STENO_ID_PR == int(StenoKey::PR));
static_assert(STENO_ID_BR == int(StenoKey::BR));
static_assert(STENO_ID_LR == int(StenoKey::LR));
static_assert(STENO_ID_GR == int(StenoKey::GR));
static_assert(STENO_ID_TR == int(StenoKey::TR));
static_assert(STENO_ID_SR == int(StenoKey::SR));
static_assert(STENO_ID_DR == int(StenoKey::DR));
static_assert(STENO_ID_ZR == int(StenoKey::ZR));
static_assert(STENO_ID_NUM1 == int(StenoKey::NUM1));
static_assert(STENO_ID_NUM2 == int(StenoKey::NUM2));
static_assert(STENO_ID_NUM3 == int(StenoKey::NUM3));
static_assert(STENO_ID_NUM4 == int(StenoKey::NUM4));
static_assert(STENO_ID_NUM5 == int(StenoKey::NUM5));
static_assert(STENO_ID_NUM6 == int(StenoKey::NUM6));
static_assert(STENO_ID_NUM7 == int(StenoKey::NUM7));
static_assert(STENO_ID_NUM8 == int(StenoKey::NUM8));
static_assert(STENO_ID_NUM9 == int(StenoKey::NUM9));
static_assert(STENO_ID_NUM10 == int(StenoKey::NUM10));
static_assert(STENO_ID_NUM11 == int(StenoKey::NUM11));
static_assert(STENO_ID_NUM12 == int(StenoKey::NUM12));

static atomic_t initialized;

#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
bool steno_squid_engine_cancel_strokes(StenoKeyState &held) {
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
  steno_squid_native_input_cancel();
#endif
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

uint8_t steno_squid_host_layout_index() { return host_profile; }

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
  bool enabled = false;
  const int operation = !strncmp(line, "enable_dictionary ", 18) ? 0
                      : !strncmp(line, "disable_dictionary ", 19) ? 1 : 2;
  if (!steno_squid_native_select(name, operation, &enabled)) {
    Console::Printf("ERR Unable to save native dictionary selection\n\n");
    return;
  }
  BufferWriter events;
  ConsoleWriter::Push(&events);
  if (enabled) {
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
  for (size_t i = 0; i < steno_squid_native_dictionary_count(); ++i) {
    entries.Add(StenoDictionaryListEntry(steno_squid_native_dictionary(i),
                                        steno_squid_native_dictionary_enabled(i)));
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
  if (!steno_squid_native_dictionary_ready()) {
    LOG_WRN("Native dictionary catalog is unavailable");
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
  dictionary_flash_ready_changed();

  LOG_INF("Steno processor pipeline ready");
}

#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
void zmk_javelin_steno_native_process_key(int steno_key_index, bool is_press,uint32_t when) {
#else
void zmk_javelin_steno_process_key(int steno_key_index, bool is_press) {
#endif
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
  StenoSquidKeyBenchmark benchmark(steno_key_index,is_press);
#endif
  // On contention discard the whole held chord, including its eventual releases.
  // A partial chord must never become a different word after the lock is freed.
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
  auto &delivery=native_delivery;
  const auto before=delivery;
#else
  static StenoSquidChordDelivery delivery;
#endif
  delivery.Event(steno_key_index, is_press);
  const JavelinEngineLock lock;
  if (!lock || !processor) {
#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
    if(processor && steno_squid_native_input_retry(steno_key_index,is_press,when)){delivery=before;return;}
#endif
    delivery.Drop();
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
    benchmark.Dropped();
#endif
    zmk_javelin_steno_prepare();
#ifdef CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS
    if (atomic_get(&initialized) && dictionary_flash_status() == DICTIONARY_FLASH_READY)
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
#ifdef CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK
  benchmark.Processed();
#endif
}

#ifdef CONFIG_ZMK_STENO_SQUID_NATIVE_STORE
void zmk_javelin_steno_process_key(int key,bool pressed) {
  const uint32_t when=k_uptime_get_32();
  if(steno_squid_native_input_offer(key,pressed,when))return;
  zmk_javelin_steno_native_process_key(key,pressed,when);
}
// Called only with the flash/engine mutex held, including the raw failure path.
void zmk_javelin_steno_native_cancel(void) {
  if(processor) {
    stenoRepeat.value.Process(StenoKeyState(),StenoAction::CANCEL_ALL);
    new (processor) StenoProcessor(stenoRepeat.value);
#ifdef CONFIG_ZMK_STENO_SQUID_PRACTICE
    steno_squid_practice_discard_chord();
#endif
  }
  native_delivery={};
}
#endif

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
