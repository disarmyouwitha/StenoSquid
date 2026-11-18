"""Common native-test helpers; no Zephyr checkout required."""
import os
import shlex
import shutil


def compiler(language='c++'):
    """Return a compiler command, respecting CC/CXX (including wrapper arguments)."""
    variable = 'CC' if language == 'c' else 'CXX'
    configured = os.environ.get(variable)
    if configured:
        return shlex.split(configured)
    choices = ('cc', 'clang', 'gcc') if language == 'c' else ('clang++', 'c++', 'g++')
    for name in choices:
        if path := shutil.which(name):
            return [path]
    raise RuntimeError(f'No native {language} compiler found; set {variable}')


def extract_c_function(source, signature):
    """Read a pinned upstream function while ignoring braces in comments/literals.

    First-party implementations use included private headers instead. This seam
    exists only for exercising ZMK's unmodified layer-lock implementation.
    """
    import re
    position = source.find(signature)
    if position < 0:
        raise RuntimeError(f"Pinned source no longer defines {signature!r}")
    start = source.rfind('\n', 0, position) + 1
    # C comments, quoted strings/chars, and C++ raw strings are indivisible.
    tokens = re.compile(r'''//[^\n]*|/\*.*?\*/|R"([^ ()\\\t\r\n]{0,16})\(.*?\)\1"|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{};]''', re.S)
    depth = 0
    opened = False
    for match in tokens.finditer(source, position + len(signature)):
        token = match[0]
        if token == '{':
            depth += 1
            opened = True
        elif token == '}':
            depth -= 1
            if opened and depth == 0:
                return source[start:match.end()]
        elif token == ';' and not opened:
            break
    raise RuntimeError(f"Cannot locate complete pinned function {signature!r}")
