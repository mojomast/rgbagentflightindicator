"""Guard against the subprocess trap that hung the watcher on Linux/macOS.

    subprocess.run(["opencode", "api", "get", path], shell=True)

On POSIX, shell=True with an argument *list* runs only the first element: the
list is passed as extra args to a shell command string, so this launched a bare
`opencode` with $0="api", $1="get", ... and it hung until the 25 second timeout.
On Windows cmd.exe joins the list back together, so the bug was invisible on the
machine it was written on. Every Linux/macOS host that installed it hung.

This checks the Python sources for that shape and for a related trap: a list
argument to subprocess with shell enabled.

    python tools/check_portability.py       # exit 1 if anything looks wrong
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", ".venv", "node_modules"}


def python_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def check(path):
    problems = []
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"does not parse: {exc}")]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in ("run", "Popen", "call", "check_call", "check_output"):
            continue

        shell = None
        first = None
        for kw in node.keywords:
            if kw.arg == "shell":
                shell = kw.value
            if kw.arg == "args":
                first = kw.value
        if first is None and node.args:
            first = node.args[0]

        if shell is None:
            continue
        literal_true = isinstance(shell, ast.Constant) and shell.value is True
        dynamic = not isinstance(shell, ast.Constant)
        if not (literal_true or dynamic):
            continue

        is_list = isinstance(first, (ast.List, ast.Tuple))
        if is_list:
            problems.append((
                node.lineno,
                "subprocess with a list AND shell=True - on POSIX only the first "
                "element runs. Drop shell=True and resolve the executable with "
                "shutil.which() instead.",
            ))
    return problems


def main() -> int:
    bad = 0
    checked = 0
    for path in sorted(python_files()):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("tools" + os.sep) and "faketest" not in rel:
            # this file quotes the pattern in its own docstring
            pass
        checked += 1
        for line, message in check(path):
            bad += 1
            print(f"  {rel}:{line}: {message}")

    print(f"checked {checked} python files")
    if bad:
        print(f"{bad} problem(s) - see above")
        return 1
    print("no subprocess shell traps found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
