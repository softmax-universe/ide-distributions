"""Test-only audit hook mounted into each interpreter during packaged workspace startup."""

import os
import sys


def audit(event, args):
    if event != "compile":
        return
    filename = args[1]
    if not isinstance(filename, str) or not filename.startswith("/"):
        return
    try:
        caller = sys._getframe(1)
    except ValueError:
        caller = None  # CPython running a source script directly.
    if caller is not None and caller.f_code.co_name in {"parse", "_build_lazy_map"}:
        # Hermes uses ast.parse for discovery. AnyIO compiles with PyCF_ONLY_AST to
        # find lazy exports. Neither produces executable module bytecode.
        if caller.f_globals.get("__name__") in {"ast", "anyio._lazyimport"}:
            return
    with open("/tmp/startup-compiled-sources", "a") as report:
        print(filename, file=report)


sys.addaudithook(audit)
with open("/tmp/startup-audited-interpreters", "a") as report:
    print(os.path.realpath(sys.executable), file=report)
