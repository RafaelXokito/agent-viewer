"""WS2: static check that agent_viewer/ contains no call able to write under the roots (SPEC 11)."""
import ast
import os
import unittest

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "agent_viewer")
ALLOWED_MODES = {"r", "rb"}
# Banned on any receiver: no str/list/dict method has these names.
BANNED_ATTRS = {"unlink", "rename", "rmdir", "write_text", "write_bytes", "truncate", "rmtree",
                "touch", "symlink_to", "hardlink_to", "chmod", "lchmod"}
# Banned on the os module (str.replace and list.remove are harmless and common).
BANNED_OS = {"remove", "rename", "renames", "replace", "unlink", "rmdir", "removedirs",
             "truncate", "ftruncate", "chmod", "chown", "utime", "symlink", "link", "mkfifo",
             "write", "makedirs", "mkdir"}
BANNED_MODULES = {"shutil", "tempfile"}


def python_files():
    for dirpath, dirnames, filenames in os.walk(PACKAGE):
        dirnames[:] = [d for d in dirnames if d not in ("static", "__pycache__")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _mode_arg(call, position):
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value
    if len(call.args) > position:
        return call.args[position]
    return None


def violations(source, filename="<src>"):
    """Forbidden constructs in one module's source, as readable strings."""
    found = []
    tree = ast.parse(source, filename)
    for node in ast.walk(tree):
        where = "%s:%d" % (os.path.basename(filename), getattr(node, "lineno", 0))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for name in names:
                if name.split(".")[0] in BANNED_MODULES:
                    found.append("%s imports %s" % (where, name))
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
            found.append("%s calls .%s()" % (where, func.attr))
            continue
        if (isinstance(func, ast.Attribute) and func.attr in BANNED_OS
                and isinstance(func.value, ast.Name) and func.value.id == "os"):
            found.append("%s calls os.%s()" % (where, func.attr))
            continue
        is_builtin_open = isinstance(func, ast.Name) and func.id == "open"
        is_module_open = (isinstance(func, ast.Attribute) and func.attr == "open"
                          and isinstance(func.value, ast.Name) and func.value.id in ("io", "builtins", "codecs"))
        is_os_open = (isinstance(func, ast.Attribute) and func.attr in ("open", "fdopen")
                      and isinstance(func.value, ast.Name) and func.value.id == "os")
        if is_os_open:
            found.append("%s calls os.%s()" % (where, func.attr))
            continue
        if is_builtin_open or is_module_open:
            mode = _mode_arg(node, 1)
        elif (isinstance(func, ast.Attribute) and func.attr == "open"
              and not (isinstance(func.value, ast.Name) and func.value.id == "webbrowser")):
            mode = _mode_arg(node, 0)  # pathlib.Path.open(mode)
        else:
            continue
        if mode is None:
            continue
        if not (isinstance(mode, ast.Constant) and mode.value in ALLOWED_MODES):
            found.append("%s opens with mode %s" % (where, ast.dump(mode)))
    return found


class ReadOnlyGuardTest(unittest.TestCase):
    def test_package_has_no_write_calls(self):
        files = list(python_files())
        self.assertTrue(files, "no python files found under agent_viewer/")
        problems = []
        for path in files:
            with open(path, encoding="utf-8") as fh:
                problems += violations(fh.read(), path)
        self.assertEqual(problems, [])

    def test_guard_detects_forbidden_constructs(self):
        bad = "\n".join([
            "import shutil",
            "from tempfile import mkstemp",
            "open(p, 'w')",
            "open(p, mode='ab')",
            "open(p, m)",
            "io.open(p, 'r+b')",
            "os.open(p, os.O_WRONLY)",
            "os.remove(p)",
            "os.rename(a, b)",
            "os.replace(a, b)",
            "p.unlink()",
            "p.write_text('x')",
            "Path(p).open('w')",
            "fh.truncate(0)",
        ])
        self.assertEqual(len(violations(bad)), 14)

    def test_guard_allows_read_modes(self):
        good = "s.replace('a', 'b')\nitems.remove(x)\nopen(p)\nopen(p, 'rb')\nopen(p, 'r', encoding='utf-8')\nPath(p).open()\nopen(p, mode='rb')\nwebbrowser.open(url)"
        self.assertEqual(violations(good), [])


if __name__ == "__main__":
    unittest.main()
