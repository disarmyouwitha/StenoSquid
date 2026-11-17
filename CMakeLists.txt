# Sets <prefix>_BYTES and <prefix>_CRC in the caller's scope from a dictionary
# image, and reconfigures when the image changes.
function(javelin_image_size_crc prefix image)
  file(SIZE "${image}" bytes)
  execute_process(
    COMMAND ${PYTHON_EXECUTABLE} -c
      "import pathlib,sys,zlib; print(hex(zlib.crc32(pathlib.Path(sys.argv[1]).read_bytes())))"
      "${image}"
    OUTPUT_VARIABLE crc OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY)
  set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${image}")
  set(${prefix}_BYTES "${bytes}" PARENT_SCOPE)
  set(${prefix}_CRC "${crc}" PARENT_SCOPE)
endfunction()
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

    if(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
      if(NOT EXISTS "${ZMK_JAVELIN_LAPWING_IMAGE}")
        message(FATAL_ERROR "Set ZMK_JAVELIN_LAPWING_IMAGE to the mapped Lapwing JSC4 image")
      endif()
      set(LAPWING_SRC "${CMAKE_CURRENT_LIST_DIR}/src/steno_squid")
      javelin_image_size_crc(LAPWING "${ZMK_JAVELIN_LAPWING_IMAGE}")
      target_sources(app PRIVATE
        "${LAPWING_SRC}/lapwing_flash.c"
        "${LAPWING_SRC}/lapwing_flash_power.c"
        "${LAPWING_SRC}/lapwing_flash_pm.c"
        "${LAPWING_SRC}/lapwing_flash_qspi.c"
        "${LAPWING_SRC}/lapwing_flash_trust.c"
        "${LAPWING_SRC}/lapwing_flash_scrub.c"
        "${LAPWING_SRC}/w25q_quad.c")
      target_sources_ifdef(CONFIG_ZMK_JAVELIN_SERIAL_CONSOLE app PRIVATE
        "${LAPWING_SRC}/serial_console.c")
      target_sources_ifdef(CONFIG_ZMK_JAVELIN_STENO_HID_PROVISIONING app PRIVATE
        "${LAPWING_SRC}/lapwing_flash_provision.c")
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
        file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/diag_build_id.h"
          "#define JAVELIN_BUILD_ID \"${JAVELIN_BUILD_ID}\"\n")
        target_include_directories(app PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
        target_sources(app PRIVATE "${LAPWING_SRC}/diag.c" "${LAPWING_SRC}/lapwing_flash_diag.c")
      endif()
      target_sources_ifdef(CONFIG_ZMK_JAVELIN_BATTERY_LOG app PRIVATE "${LAPWING_SRC}/battery_log.c")
      target_include_directories(app PRIVATE "${LAPWING_SRC}")
      target_compile_definitions(app PRIVATE LAPWING_BYTES=${LAPWING_BYTES} LAPWING_CRC=${LAPWING_CRC})
      # Optional second base, provisioned separately without rewriting Lapwing.
      set(PLOVER_BYTES 0)
      set(PLOVER_CRC 0)
      if(ZMK_JAVELIN_PLOVER_IMAGE)
        javelin_image_size_crc(PLOVER "${ZMK_JAVELIN_PLOVER_IMAGE}")
      endif()
      target_compile_definitions(app PRIVATE PLOVER_BYTES=${PLOVER_BYTES} PLOVER_CRC=${PLOVER_CRC})
      # Optional image of the smaller Lapwing dictionaries. Once a build names
      # it, the firmware needs it in flash before onboard Javelin starts.
      set(EXTRAS_BYTES 0)
      set(EXTRAS_CRC 0)
      if(ZMK_JAVELIN_EXTRAS_IMAGE)
        javelin_image_size_crc(EXTRAS "${ZMK_JAVELIN_EXTRAS_IMAGE}")
      endif()
      target_compile_definitions(app PRIVATE EXTRAS_BYTES=${EXTRAS_BYTES} EXTRAS_CRC=${EXTRAS_CRC})
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

    # Platform shims (our ZMK implementations)
    set(ZMK_SHIM_SRC
      src/platform/zmk_key_shim.cc
      src/platform/zmk_clock_shim.cc
      src/platform/zmk_flash_shim.cc
      src/platform/zmk_console_shim.cc
      src/platform/zmk_stubs.cc
    )

    # Engine initialization
    set(ZMK_INIT_SRC
      src/engine_init.cc
    )
    if(CONFIG_ZMK_STENO_SQUID_STROKE_TAPE)
      list(APPEND ZMK_INIT_SRC src/steno_squid/stroke_feed.cc)
    endif()
    if(CONFIG_ZMK_STENO_SQUID_PRACTICE)
      list(APPEND ZMK_INIT_SRC src/steno_squid/practice_engine.cc)
    endif()

    # Pure Javelin sources (no ZMK deps) → separate library for C++23
    set(JAVELIN_LIB_SRC
      ${JAVELIN_CORE_SRC}
      ${JAVELIN_DICT_SRC}
      ${JAVELIN_PROC_SRC}
      ${JAVELIN_HAL_SRC}
      ${JAVELIN_FONT_SRC}
      ${JAVELIN_SPLIT_SRC}
    )

    # Shims + init (need ZMK headers) → compiled into app target
    set(ZMK_BRIDGE_SRC
      ${ZMK_SHIM_SRC}
      ${ZMK_INIT_SRC}
    )

    # Build everything as a Zephyr library for C++23.
    zephyr_library_named(javelin_steno)
    zephyr_library_sources(${JAVELIN_LIB_SRC})
    zephyr_library_compile_options(-fpermissive)

    zephyr_library_named(steno_squid_engine)
    zephyr_library_sources(${ZMK_BRIDGE_SRC} src/platform/font_stub.cc)
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
    target_include_directories(javelin_steno PRIVATE "${LAPWING_SRC}")
    if(CONFIG_ZMK_JAVELIN_STENO_EXTERNAL_LAPWING)
      zephyr_library_sources("${LAPWING_SRC}/lapwing_dictionary.cc")
      zephyr_library_include_directories(
        "${JAVELIN_SRC_DIR}/dictionary" "${LAPWING_SRC}")
      zephyr_library_compile_definitions(LAPWING_BYTES=${LAPWING_BYTES} PLOVER_BYTES=${PLOVER_BYTES}
        EXTRAS_BYTES=${EXTRAS_BYTES})
      if(CONFIG_ZMK_JAVELIN_STENO_BROWSER_DICTIONARY)
        target_sources(app PRIVATE "${LAPWING_SRC}/javelin_usb.c")
        zephyr_library_sources("${LAPWING_SRC}/user_dictionary_store.cc" "${LAPWING_SRC}/javelin_web_hid.cc")
        if(CONFIG_ZMK_JAVELIN_STENO_BLE_HID)
          target_sources(app PRIVATE "${LAPWING_SRC}/javelin_ble_hid.c"
            "${LAPWING_SRC}/javelin_ble_tx.c")
        endif()
      endif()
    endif()

  endif() # central role check

endif() # CONFIG_ZMK_JAVELIN_STENO
