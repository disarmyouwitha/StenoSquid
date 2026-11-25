# Layer handling on the left half: dictionary hold and Practice. It does not
# depend on the right half having a display.
if(CONFIG_ZMK_JAVELIN_STENO AND CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
  target_sources(app PRIVATE src/steno_squid/layer_control.c src/steno_squid/engine_work.c)
endif()

if(CONFIG_ZMK_STENO_SQUID_SPLIT_STATUS)
  if(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
    target_sources(app PRIVATE src/steno_squid/split_status_central.c)
  else()
    target_sources(app PRIVATE
      src/steno_squid/split_status_display.c
      src/steno_squid/display/portrait.c
      src/steno_squid/display/pet_widget.c
      src/steno_squid/display/screen.c
      src/steno_squid/display/practice_widget.c
    )
  endif()
endif()

if(CONFIG_ZMK_STENO_SQUID_PRACTICE)
  target_sources(app PRIVATE src/behaviors/behavior_javelin_practice.c
      src/steno_squid/practice_progress.c)
endif()

if(CONFIG_ZMK_JAVELIN_STENO)

  target_sources(app PRIVATE src/behaviors/behavior_tap_first.c)

  zephyr_include_directories(include)
  target_sources(app PRIVATE src/behaviors/behavior_javelin_steno.c)

  # The engine runs only on a supported split central.
  if(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)

    # The engine is the javelin-steno fork named in config/west.yml. It carries
    # the firmware's changes as commits, so it is built as it is.
    set(JAVELIN_SRC_DIR "${CMAKE_CURRENT_LIST_DIR}/javelin")
    if(NOT EXISTS "${JAVELIN_SRC_DIR}/engine.h")
      # When this repository is the build module, west checks the engine out
      # beside Zephyr in the isolated workspace rather than inside this repo.
      set(JAVELIN_SRC_DIR "${ZEPHYR_BASE}/../javelin-steno")
    endif()
    if(NOT EXISTS "${JAVELIN_SRC_DIR}/engine.h")
      message(FATAL_ERROR "Javelin engine not found; see config/west.yml")
    endif()
    zephyr_include_directories(${JAVELIN_SRC_DIR})

    if(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_FLASH)
      set(SQUID_SRC "${CMAKE_CURRENT_LIST_DIR}/src/steno_squid")
      target_sources(app PRIVATE
        "${SQUID_SRC}/dictionary_flash.c"
        "${SQUID_SRC}/dictionary_flash_power.c"
        "${SQUID_SRC}/dictionary_flash_pm.c"
        "${SQUID_SRC}/dictionary_flash_qspi.c"
        "${SQUID_SRC}/dictionary_flash_scrub.c"
        "${SQUID_SRC}/w25q_quad.c")
      target_sources_ifdef(CONFIG_ZMK_JAVELIN_SERIAL_CONSOLE app PRIVATE
        "${SQUID_SRC}/serial_console.c")
      if(CONFIG_ZMK_JAVELIN_DIAG)
        # Build id shown by the S command: the CI commit, else git describe.
        set(JAVELIN_BUILD_ID "unknown")
        if(DEFINED ENV{GITHUB_SHA})
          string(SUBSTRING "$ENV{GITHUB_SHA}" 0 7 JAVELIN_BUILD_ID)
        else()
          execute_process(
            COMMAND git -C "${CMAKE_CURRENT_LIST_DIR}" describe --always --dirty
            OUTPUT_VARIABLE JAVELIN_GIT_ID OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET RESULT_VARIABLE JAVELIN_GIT_RC)
          if(JAVELIN_GIT_RC EQUAL 0 AND JAVELIN_GIT_ID)
            set(JAVELIN_BUILD_ID "${JAVELIN_GIT_ID}")
          endif()
        endif()
        file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/flash_diagnostics_build_id.h"
          "#define JAVELIN_BUILD_ID \"${JAVELIN_BUILD_ID}\"\n")
        target_include_directories(app PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
        target_sources(app PRIVATE "${SQUID_SRC}/flash_diagnostics.c" "${SQUID_SRC}/dictionary_flash_diag.c")
      endif()
      target_sources_ifdef(CONFIG_ZMK_JAVELIN_BATTERY_LOG app PRIVATE "${SQUID_SRC}/battery_log.c")
      target_include_directories(app PRIVATE "${SQUID_SRC}")
    endif()

    # Core engine sources
    set(JAVELIN_CORE_SRC
      ${JAVELIN_SRC_DIR}/asset_manager.cc
      ${JAVELIN_SRC_DIR}/base64.cc
      ${JAVELIN_SRC_DIR}/bit_field.cc
      ${JAVELIN_SRC_DIR}/bit.cc
      ${JAVELIN_SRC_DIR}/button_script_manager.cc
      ${JAVELIN_SRC_DIR}/button_script.cc
      ${JAVELIN_SRC_DIR}/console_input_buffer.cc
      ${JAVELIN_SRC_DIR}/console.cc
      ${JAVELIN_SRC_DIR}/container/cyclic_queue.cc
      ${JAVELIN_SRC_DIR}/container/list.cc
      ${JAVELIN_SRC_DIR}/crc32.cc
      ${JAVELIN_SRC_DIR}/date_time.cc
      ${JAVELIN_SRC_DIR}/engine_add_translation_mode.cc
      ${JAVELIN_SRC_DIR}/engine_binding.cc
      ${JAVELIN_SRC_DIR}/engine_console_mode.cc
      ${JAVELIN_SRC_DIR}/engine_normal_mode.cc
      ${JAVELIN_SRC_DIR}/engine.cc
      ${JAVELIN_SRC_DIR}/flash.cc
      ${JAVELIN_SRC_DIR}/hash.cc
      ${JAVELIN_SRC_DIR}/host_layout.cc
      ${JAVELIN_SRC_DIR}/key_code.cc
      ${JAVELIN_SRC_DIR}/key_press_parser.cc
      ${JAVELIN_SRC_DIR}/mem.cc
      ${JAVELIN_SRC_DIR}/orthography.cc
      ${JAVELIN_SRC_DIR}/pattern_component.cc
      ${JAVELIN_SRC_DIR}/pattern.cc
      ${JAVELIN_SRC_DIR}/pool_allocate.cc
      ${JAVELIN_SRC_DIR}/random.cc
      ${JAVELIN_SRC_DIR}/script_byte_code.cc
      ${JAVELIN_SRC_DIR}/script_storage.cc
      ${JAVELIN_SRC_DIR}/script.cc
      ${JAVELIN_SRC_DIR}/segment_builder.cc
      ${JAVELIN_SRC_DIR}/segment.cc
      ${JAVELIN_SRC_DIR}/start_console_command_detector.cc
      ${JAVELIN_SRC_DIR}/state.cc
      ${JAVELIN_SRC_DIR}/steno_key_code_buffer_functions.cc
      ${JAVELIN_SRC_DIR}/steno_key_code_buffer.cc
      ${JAVELIN_SRC_DIR}/steno_key_code_emitter.cc
      ${JAVELIN_SRC_DIR}/steno_key_code.cc
      ${JAVELIN_SRC_DIR}/steno_key_state.cc
      ${JAVELIN_SRC_DIR}/str.cc
      ${JAVELIN_SRC_DIR}/stroke_history.cc
      ${JAVELIN_SRC_DIR}/stroke_list_parser.cc
      ${JAVELIN_SRC_DIR}/stroke.cc
      ${JAVELIN_SRC_DIR}/system.cc
      ${JAVELIN_SRC_DIR}/timer_manager.cc
      ${JAVELIN_SRC_DIR}/unicode_script.cc
      ${JAVELIN_SRC_DIR}/unicode.cc
      ${JAVELIN_SRC_DIR}/utf8_pointer.cc
      ${JAVELIN_SRC_DIR}/word_list.cc
      ${JAVELIN_SRC_DIR}/wpm_tracker.cc
      ${JAVELIN_SRC_DIR}/writer.cc
    )

    # Dictionary sources
    set(JAVELIN_DICT_SRC
      ${JAVELIN_SRC_DIR}/dictionary/cache_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/compact_map_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/debug_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/dictionary_list.cc
      ${JAVELIN_SRC_DIR}/dictionary/dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/emily_symbols_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/full_map_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/invalid_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/orthospelling_data.cc
      ${JAVELIN_SRC_DIR}/dictionary/orthospelling_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/reverse_auto_suffix_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/reverse_map_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/reverse_prefix_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/reverse_suffix_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/unicode_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/user_dictionary.cc
      ${JAVELIN_SRC_DIR}/dictionary/wrapped_dictionary.cc
    )

    # Processor sources
    set(JAVELIN_PROC_SRC
      ${JAVELIN_SRC_DIR}/processor/all_up.cc
      ${JAVELIN_SRC_DIR}/processor/first_up.cc
      ${JAVELIN_SRC_DIR}/processor/gemini.cc
      ${JAVELIN_SRC_DIR}/processor/jeff_modifiers.cc
      ${JAVELIN_SRC_DIR}/processor/paper_tape.cc
      ${JAVELIN_SRC_DIR}/processor/passport.cc
      ${JAVELIN_SRC_DIR}/processor/plover_hid.cc
      ${JAVELIN_SRC_DIR}/processor/procat.cc
      ${JAVELIN_SRC_DIR}/processor/processor_list.cc
      ${JAVELIN_SRC_DIR}/processor/processor.cc
      ${JAVELIN_SRC_DIR}/processor/repeat.cc
      ${JAVELIN_SRC_DIR}/processor/tx_bolt.cc
    )

    # HAL sources (weak defaults — our shims override the critical ones)
    set(JAVELIN_HAL_SRC
      ${JAVELIN_SRC_DIR}/hal/ble.cc
      ${JAVELIN_SRC_DIR}/hal/bootloader.cc
      ${JAVELIN_SRC_DIR}/hal/connection.cc
      ${JAVELIN_SRC_DIR}/hal/display.cc
      ${JAVELIN_SRC_DIR}/hal/gpio.cc
      ${JAVELIN_SRC_DIR}/hal/infrared.cc
      ${JAVELIN_SRC_DIR}/hal/midi.cc
      ${JAVELIN_SRC_DIR}/hal/mouse.cc
      ${JAVELIN_SRC_DIR}/hal/power.cc
      ${JAVELIN_SRC_DIR}/hal/rgb.cc
      ${JAVELIN_SRC_DIR}/hal/rtc.cc
      ${JAVELIN_SRC_DIR}/hal/serial_port.cc
      ${JAVELIN_SRC_DIR}/hal/sound.cc
      ${JAVELIN_SRC_DIR}/hal/usb_status.cc
    )

    # Button scripts reference the bitmap fonts. src/platform/font_stub.cc
    # stands in for the glyph tables, which this build never draws.
    set(JAVELIN_FONT_SRC
      ${JAVELIN_SRC_DIR}/font/monochrome/font.cc
    )

    # Split sources (ZMK handles split transport, but Javelin split types
    # are referenced by other code)
    set(JAVELIN_SPLIT_SRC
      ${JAVELIN_SRC_DIR}/split/split_console.cc
      ${JAVELIN_SRC_DIR}/split/split_key_state.cc
      ${JAVELIN_SRC_DIR}/split/split_midi.cc
      ${JAVELIN_SRC_DIR}/split/split_power_override.cc
      ${JAVELIN_SRC_DIR}/split/split_serial_buffer.cc
      ${JAVELIN_SRC_DIR}/split/split_usb_status.cc
      ${JAVELIN_SRC_DIR}/split/split_version.cc
      ${JAVELIN_SRC_DIR}/split/split.cc
    )

    # Upstream sources use the fork's permissive flags; first-party adapters
    # compile strictly in a separate C++23 library.
    zephyr_library_named(javelin_steno)
    zephyr_library_sources(${JAVELIN_CORE_SRC} ${JAVELIN_DICT_SRC} ${JAVELIN_PROC_SRC}
      ${JAVELIN_HAL_SRC} ${JAVELIN_FONT_SRC} ${JAVELIN_SPLIT_SRC})
    zephyr_library_compile_options(-fpermissive)

    zephyr_library_named(steno_squid_engine)
    zephyr_library_sources(
      src/platform/zmk_key_shim.cc
      src/platform/zmk_clock_shim.cc
      src/platform/zmk_flash_shim.cc
      src/platform/zmk_console_shim.cc
      src/platform/zmk_stubs.cc
      src/platform/font_stub.cc
      src/engine_init.cc)
    if(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
      zephyr_library_sources(src/steno_squid/stroke_feed.cc)
    endif()
    if(CONFIG_ZMK_STENO_SQUID_PRACTICE)
      zephyr_library_sources(src/steno_squid/practice_engine.cc)
    endif()
    if(CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS)
      zephyr_library_sources(src/steno_squid/memory_report.cc)
    endif()
    if(CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK)
      zephyr_library_sources(src/steno_squid/dictionary_benchmark.cc)
      if(CONFIG_ZMK_JAVELIN_DIAG)
        zephyr_library_sources(src/steno_squid/storage_probe.cc)
        zephyr_library_sources(src/steno_squid/checkpoint_probe.cc)
      endif()
      zephyr_library_include_directories(${CMAKE_CURRENT_BINARY_DIR})
    endif()
    if(CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS)
      zephyr_library_sources(src/platform/macos_us_layout.cc src/platform/windows_us_layout.cc)
    endif()
    foreach(bridge_target IN ITEMS javelin_steno steno_squid_engine)
    # ZMK app headers aren't inherited by libraries — add manually
    target_include_directories(${bridge_target} PRIVATE ${APPLICATION_SOURCE_DIR}/include)
    set(ZEPHYR_CURRENT_LIBRARY ${bridge_target})
    zephyr_library_compile_options(
      -std=gnu++23
      -fno-exceptions
      -fno-rtti
      -fno-threadsafe-statics
    )
    target_compile_definitions(${bridge_target} PRIVATE
      JAVELIN_USE_EMBEDDED_STENO=1
      JAVELIN_PLATFORM_ZEPHYR=1
      JAVELIN_BOARD_CONFIG=<stddef.h>
      M_PI=3.14159265358979323846
    )
    endforeach()
    set(ZEPHYR_CURRENT_LIBRARY steno_squid_engine)
    target_include_directories(javelin_steno PRIVATE "${SQUID_SRC}")
    if(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_FLASH)
      zephyr_library_sources("${SQUID_SRC}/native_store_runtime.cc" "${SQUID_SRC}/native_input.cc")
      target_sources(app PRIVATE "${SQUID_SRC}/native_input_layers.c")
      zephyr_library_include_directories(
        "${JAVELIN_SRC_DIR}/dictionary" "${SQUID_SRC}")
      if(CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY)
        target_sources(app PRIVATE "${SQUID_SRC}/javelin_usb.c")
        zephyr_library_sources("${SQUID_SRC}/javelin_web_hid.cc")
        if(CONFIG_ZMK_JAVELIN_STENO_BLE_HID)
          target_sources(app PRIVATE "${SQUID_SRC}/javelin_ble_hid.c"
            "${SQUID_SRC}/javelin_ble_tx.c")
        endif()
      endif()
    endif()

  endif() # central role check

endif() # CONFIG_ZMK_JAVELIN_STENO
