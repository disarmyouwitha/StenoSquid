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
