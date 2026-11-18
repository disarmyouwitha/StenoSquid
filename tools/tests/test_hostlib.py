#!/usr/bin/env python3
"""Protect the one remaining upstream-source seam against literal braces."""
from hostlib import extract_c_function

source = '''
void before() {}
static int upstream(int x) {
  // } comment
  const char *a = "{quoted}\\\"}";
  const char *b = R"tag({raw})tag";
  const char c = '}'; /* { */
  if (x) { return 1; }
  return 0;
}
void after() {}
'''
found = extract_c_function(source, 'static int upstream(')
assert found.startswith('static int upstream(')
assert found.endswith('return 0;\n}')
assert 'after()' not in found
for invalid in ['missing(', 'void missing();']:
    try:
        extract_c_function(invalid, 'missing(')
    except RuntimeError:
        pass
    else:
        raise AssertionError('Missing/bodyless function must fail clearly')
print('Pinned upstream seam handles comments, quoted/raw strings and missing functions')
