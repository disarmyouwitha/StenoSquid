#pragma once

// Private scoring implementation shared with the native test harness.
/* The first chord follows the displayed learning outline. Both failed strokes
 * restart at the first chord. */
static bool practice_first_stroke_correct(const StenoStroke &stroke) {
  const char *outline = steno_squid_multi[practice_prompt()].outline;
  const char *slash = strchr(outline, '/');
  if (!slash) {
    return false;
  }
  char first[STENO_SQUID_PRACTICE_OUTLINE_SIZE];
  snprintf(first, sizeof(first), "%.*s", int(slash - outline), outline);
  StenoStroke expected;
  expected.Set(first);
  return stroke == expected;
}

static bool practice_correct(const StenoStroke &stroke) {
  if (practice.drill == STENO_SQUID_PRACTICE_BUILD) {
    StenoStroke expected;
    expected.Set(steno_squid_build[practice_prompt()].strokes[practice.build_stage]);
    return stroke == expected;
  }
  if (!steno_squid_practice_word_drill(practice.drill) && practice.drill != STENO_SQUID_PRACTICE_MULTI) {
    StenoStroke expected;
    expected.Set(steno_squid_practice_outline(practice.drill, practice_prompt()));
    return stroke == expected;
  }
  const bool multi = practice.drill == STENO_SQUID_PRACTICE_MULTI;
  const StenoDictionaryLookup lookup(multi ? practice.multi_strokes : &stroke, multi ? 2 : 1);
  const char *expected = multi ? steno_squid_multi[practice_prompt()].word : steno_squid_words[practice_prompt()].word;
  auto result = StenoEngine::container.value.GetDictionary().Lookup(lookup);
  bool correct = result.IsValid() && strcasecmp(result.GetText(), expected) == 0;
  result.Destroy();
  if (!correct) {
    if (const auto *base = practice_fallback_dictionary()) {
      result = base->Lookup(lookup);
      correct = result.IsValid() && strcasecmp(result.GetText(), expected) == 0;
      result.Destroy();
    }
  }
  return correct;
}

