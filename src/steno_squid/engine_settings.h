#pragma once

// Private settings reader, also compiled by the native regression.
static int javelin_settings_set(const char *name, size_t length,
                                settings_read_cb read, void *argument) {
#ifdef CONFIG_ZMK_JAVELIN_STENO_HOST_LAYOUTS
  if (strcmp(name, "host_profile") == 0) {
    uint8_t profile;
    if (length != sizeof(profile) || read(argument, &profile, sizeof(profile)) != sizeof(profile) ||
        profile >= ARRAY_SIZE(host_profiles)) {
      return -EINVAL;
    }
    host_profile = profile;
    return 0;
  }
#endif
  if (strcmp(name, "dictionaries") != 0) {
    return -ENOENT;
  }
  uint8_t record[2];
  if (length != sizeof(record) ||
      read(argument, record, sizeof(record)) != static_cast<ssize_t>(sizeof(record)) ||
      record[0] != DICTIONARY_SELECTION_VERSION || (record[1] & 0x80)) {
    return -EINVAL;
  }
  dictionary_selection = record[1];
  return 0;
}

