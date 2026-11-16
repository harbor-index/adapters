"""Minimal stand-in for the unmaintained `pyext` package (does not install on Python>=3.10).

AIRS-Bench's APPS `testing_util.py` only uses `RuntimeModule.from_string(name, doc, code)`,
which compiles `code` into a fresh module. This shim does exactly that.
"""
import types


class RuntimeModule:
    @staticmethod
    def from_string(name, doc, code):
        module = types.ModuleType(name, doc)
        exec(compile(code, f"<{name}>", "exec"), module.__dict__)
        return module
