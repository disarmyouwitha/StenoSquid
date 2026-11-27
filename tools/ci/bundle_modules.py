"""Bundle the Manager's JavaScript modules into one script for the offline HTML.

The page is a single file opened from disk, so relative `import`s cannot work. Each module becomes a function scope (their top-level
names never collide) and `import {a, b as c} from "./x.js"` becomes a destructuring of that module's exports. This handles exactly
the syntax the Manager's modules use: single `import {...} from "./name.js";` statements (which may span lines) and `export`
on function, async function, generator, class, const and let declarations. Anything else is an error, not a silent skip.
"""
from pathlib import Path
import re

IMPORT = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"\./([A-Za-z0-9_]+)\.js";[ \t]*\n', re.M)
EXPORT = re.compile(r'^export\s+(async\s+function\*?|function\*?|class|const|let)\s+([A-Za-z_$][\w$]*)', re.M)
UNSUPPORTED = re.compile(r'^\s*(export\s+(default|\*|\{)|import\s+(\*|[A-Za-z_$"\']))|import\.meta|^await\s', re.M)


def _transform(name, source):
    if UNSUPPORTED.search(source):
        raise ValueError(f"{name}.js uses module syntax the bundler does not handle")
    imports = []

    def take_import(match):
        parts = []
        for item in match.group(1).split(","):
            item = item.strip()
            if not item:
                continue
            original, _, alias = item.partition(" as ")
            parts.append(f"{original.strip()}: {alias.strip()}" if alias else original.strip())
        imports.append(match.group(2))
        return f'const {{{", ".join(parts)}}} = __modules["{match.group(2)}"];\n'

    body = IMPORT.sub(take_import, source)
    if re.search(r'^import\b', body, re.M):
        raise ValueError(f"{name}.js has an import the bundler does not understand")
    names = [match.group(2) for match in EXPORT.finditer(body)]
    body = EXPORT.sub(lambda match: match.group(0).replace("export ", "", 1), body)
    if re.search(r'^export\b', body, re.M):
        raise ValueError(f"{name}.js has an export the bundler does not understand")
    return body, names, imports


def bundle(entry):
    """JavaScript defining `__modules[name]` (the exports of each module) for `entry` and everything it imports."""
    entry = Path(entry)
    folder = entry.parent
    done, active, chunks = set(), [], []

    def visit(name):
        if name in done:
            return
        if name in active:
            raise ValueError("Import cycle: " + " -> ".join(active + [name]))
        active.append(name)
        body, names, imports = _transform(name, (folder / f"{name}.js").read_text(encoding="utf-8"))
        for dependency in imports:
            visit(dependency)
        active.pop()
        done.add(name)
        chunks.append(f'__modules["{name}"] = (() => {{\n{body}\nreturn {{{", ".join(names)}}};\n}})();\n')

    visit(entry.stem)
    return "const __modules = {};\n" + "".join(chunks)
