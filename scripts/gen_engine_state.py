"""Generate ``xuloom/core/engine_state.py``: the shared state every QueryEngine mixin is typed against.

QueryEngine is assembled from mixins that use each other's attributes and methods. Checked on its own, a
mixin cannot see those members, so mypy could not check the mixins at all. ``EngineState`` declares them in
one place: instance attributes as annotations, methods as stubs under ``TYPE_CHECKING``. At runtime it is an
empty class, so inheriting it changes nothing; under mypy every mixin sees the whole engine, and each real
method is checked as an override of its declared signature.

    python scripts/gen_engine_state.py          # rewrite state.py
    python scripts/gen_engine_state.py --check  # exit 1 when engine_state.py is out of date (used by the tests)

Attribute types come from an explicit annotation, or from ``self.x = SomeClass(...)`` when every
assignment agrees (``None`` makes it Optional); anything else is ``Any``. Annotate in the defining class to
sharpen a type, then regenerate.
"""
from __future__ import annotations

import ast
import builtins
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "xuloom/core/engine_state.py"
NL = "\n"


def sources():
    """(module, path, class name) in QueryEngine's method resolution order."""
    engine = ROOT / "xuloom/core/engine.py"
    tree = ast.parse(engine.read_text(encoding="utf-8"))
    query = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueryEngine")
    bases = [base.id for base in query.bases if isinstance(base, ast.Name)]
    located = {}
    for path in sorted((ROOT / "xuloom/core/engine_parts").glob("*.py")) + [ROOT / "xuloom/core/model_stream.py"]:
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and node.name in bases:
                located[node.name] = (module, path)
    missing = [base for base in bases if base not in located]
    if missing:
        raise SystemExit(f"QueryEngine bases not found: {missing}")
    return [("xuloom.core.engine", engine, "QueryEngine")] + [(*located[base], base) for base in bases]


def module_names(module, tree):
    """Name -> import statement that makes it available, for names a module imports or defines."""
    names = {}
    package = module.rsplit(".", 1)[0]
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = f"import {alias.name} as {alias.asname}"
                else:
                    top = alias.name.split(".")[0]
                    names[top] = f"import {top}" if alias.name == top else f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            source = node.module or ""
            if node.level:
                source = ".".join(module.split(".")[: -node.level] + ([source] if source else []))
            for alias in node.names:
                local = alias.asname or alias.name
                names[local] = f"from {source} import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names[node.name] = f"from {module} import {node.name}"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = f"from {module} import {target.id}"
    del package
    return names


def referenced(expression):
    """Root names used by an annotation expression (strings are parsed as annotations)."""
    found = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                found |= referenced(ast.parse(node.value, mode="eval"))
            except SyntaxError:
                pass
    return found


def self_target(node):
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"


KEEP_DECORATORS = {"property", "staticmethod", "classmethod", "abstractmethod"}


def stub(function):
    """The function's signature with defaults elided and a ``...`` body."""
    function = ast.parse(ast.unparse(function)).body[0]  # private copy
    arguments = function.args
    arguments.defaults = [ast.Constant(Ellipsis) for _ in arguments.defaults]
    arguments.kw_defaults = [None if value is None else ast.Constant(Ellipsis) for value in arguments.kw_defaults]
    decorators = []
    for decorator in function.decorator_list:
        name = decorator.id if isinstance(decorator, ast.Name) else decorator.attr if isinstance(decorator, ast.Attribute) else ""
        if name in KEEP_DECORATORS or name in ("setter", "deleter"):
            decorators.append(decorator)
    function.decorator_list = decorators
    function.body = [ast.Expr(ast.Constant(Ellipsis))]
    function.type_comment = None
    return function


def generate():
    attributes = {}   # name -> list of (annotation text | None, inferred class | "None" | None, module names)
    methods = {}      # name -> list of stub nodes (property + setter share a name)
    needed = {}       # import statements by name
    order = []
    for module, path, class_name in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = module_names(module, tree)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)

        def use(expression):
            for name in referenced(expression):
                if name in names:
                    needed.setdefault(name, names[name])

        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith("__") and item.name.endswith("__"):
                    pass
                elif item.name not in methods or any(
                        isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter") for d in item.decorator_list):
                    if item.name not in methods:
                        order.append(("method", item.name))
                        methods[item.name] = []
                    if not methods[item.name] or methods[item.name][0][0] == class_name:
                        node = stub(item)
                        methods[item.name].append((class_name, node))
                        for annotation in [a.annotation for a in item.args.args + item.args.kwonlyargs + item.args.posonlyargs
                                           + [x for x in (item.args.vararg, item.args.kwarg) if x]] + [item.returns]:
                            if annotation is not None:
                                use(annotation)
                for node in ast.walk(item):
                    if isinstance(node, ast.AnnAssign) and self_target(node.target):
                        attributes.setdefault(node.target.attr, []).append(("annotation", ast.unparse(node.annotation)))
                        use(node.annotation)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            for element in (target.elts if isinstance(target, ast.Tuple) else [target]):
                                if self_target(element):
                                    value = node.value
                                    kind = None
                                    if isinstance(value, ast.Constant) and value.value is None:
                                        kind = ("none", None)
                                    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                                            and value.func.id[:1].isupper() and value.func.id in names \
                                            and not isinstance(target, ast.Tuple):
                                        kind = ("class", value.func.id)
                                    attributes.setdefault(element.attr, []).append(kind or ("other", None))
                                    if kind and kind[0] == "class":
                                        needed.setdefault(value.func.id, names[value.func.id])
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                attributes.setdefault(item.target.id, []).append(("annotation", ast.unparse(item.annotation)))
                use(item.annotation)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.setdefault(target.id, []).append(("other", None))

    lines = []
    for name in sorted(attributes):
        if name in methods:
            continue
        seen = attributes[name]
        annotated = [value for kind, value in seen if kind == "annotation"]
        classes = {value for kind, value in seen if kind == "class"}
        if annotated:
            annotation = annotated[0]
        elif classes and len(classes) == 1 and all(kind in ("class", "none") for kind, _ in seen):
            annotation = next(iter(classes))
            if any(kind == "none" for kind, _ in seen):
                annotation = f"Optional[{annotation}]"
        else:
            annotation = "Any"
        lines.append(f"    {name}: {annotation}")
    stubs = []
    for name in sorted(methods):
        for _, node in methods[name]:
            for line in ast.unparse(node).split(NL):
                stubs.append("        " + line)
    body = NL.join(lines + stubs)
    imports = sorted({statement for name, statement in needed.items()
                      if name not in ("Any", "Optional", "TYPE_CHECKING") and not hasattr(builtins, name)
                      and re.search(r"\b" + re.escape(name) + r"\b", body)})
    imports = [statement for statement in imports
               if not statement.startswith("from xuloom.core.engine import")
               and not any(statement.endswith(" import " + cls) for _, _, cls in sources())]
    header = [
        '"""Shared QueryEngine state for typing the engine mixins. Generated; do not edit.',
        "",
        "Regenerate with ``python scripts/gen_engine_state.py`` after adding engine attributes or mixin methods.",
        "At runtime ``EngineState`` is an empty class: attribute annotations create nothing and the method stubs",
        "exist only for type checkers. See docs/development.md (类型检查覆盖范围).",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import TYPE_CHECKING, Any, Optional",
        "",
        "if TYPE_CHECKING:",
        *["    " + statement for statement in imports],
        "",
        "",
        "class EngineState:",
        '    """Attributes and methods every QueryEngine mixin may rely on."""',
        "",
        *lines,
        "",
        "    if TYPE_CHECKING:",
        *stubs,
        "",
    ]
    return NL.join(header)


def main():
    text = generate()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current.replace("\r\n", NL) != text:
            print(f"{TARGET.relative_to(ROOT)} is out of date; run python scripts/gen_engine_state.py")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8", newline=NL)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
