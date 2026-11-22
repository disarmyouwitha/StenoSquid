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
#ifdef CONFIG_ZMK_STENO_SQUID_STROKE_HINT
  if (strcmp(name, "stroke_hints") == 0) {
    uint8_t enabled;
    if (length != sizeof(enabled) || read(argument, &enabled, sizeof(enabled)) != sizeof(enabled) ||
        enabled > 1) {
      return -EINVAL;
    }
    stroke_hints = enabled;
    return 0;
  }
#endif
  return -ENOENT;
}
