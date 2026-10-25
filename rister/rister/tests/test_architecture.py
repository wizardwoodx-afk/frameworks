"""Architectural invariants — the shape of the package is a testable contract.

These tests read the source tree with :mod:`ast` and assert the dependency rules that
keep RISTER composable. They fail the build if a module reaches sideways, if a cycle
appears, or if the dependency-free core stops being dependency-free.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
ROOT = PACKAGE.parent

#: Packages that may import whatever they like, because their job *is* composition.
COMPOSITION_ROOTS = {"__init__", "__main__", "demo", "tests"}

#: The only edges allowed besides "everything imports core".
#: A dependent module may import from a dependency listed here.
ALLOWED_EXTRA = {
    ("graph", "state"),   # the engine merges state through the reducer algebra
    ("evals", "agent"),   # a suite can evaluate an agent directly
}


def _module_name(path: Path) -> str:
    """Resolve a file to its dotted module name inside the package."""
    rel = path.relative_to(PACKAGE).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["rister", *parts])


def _package_parts(path: Path) -> list[str]:
    """The package a file lives in, as parts after the top-level ``rister``."""
    rel = path.relative_to(PACKAGE).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()  # the module itself is not part of its package
    return parts


def _edges() -> tuple[set[tuple[str, str]], dict[str, list[str]]]:
    """Return (package-level edges, {module: raw unresolved imports})."""
    edges: set[tuple[str, str]] = set()
    unresolved: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        source_pkg = ".".join(_package_parts(path))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                # Relative import: walk up `level - 1` packages from the current one.
                parts = _package_parts(path)
                up = node.level - 1
                base = parts[: len(parts) - up] if up else parts
                dotted = ".".join(["rister", *base, *(node.module.split(".") if node.module else [])])
            elif node.module and node.module.startswith("rister"):
                dotted = node.module
            else:
                unresolved.setdefault(module, []).append(
                    node.module or ", ".join(a.name for a in node.names)
                )
                continue
            target_parts = dotted.split(".")[1:]
            if not target_parts:
                continue
            # A package importing its own submodules is not an edge between subsystems.
            if _package_of(module) == target_parts[0]:
                continue
            edges.add((module, target_parts[0]))
    return edges, unresolved


def _package_of(module: str) -> str:
    """The subsystem a module belongs to; the package root is a composition root.

    ``rister.agent.tool`` -> ``agent``; ``rister.__main__`` -> ``__main__``;
    ``rister`` (the root ``__init__``) -> ``__init__``.
    """
    parts = module.split(".")
    return "__init__" if len(parts) == 1 else parts[1]


def test_every_module_is_reachable():
    """No orphan files: every module belongs to a known package."""
    known = {"core", "state", "graph", "agent", "govern", "observe", "security",
             "evals", "serve", "demo", "__main__", "__init__", "tests"}
    found = {p.relative_to(PACKAGE).parts[0].replace(".py", "")
             for p in PACKAGE.rglob("*.py")}
    assert found <= known, f"unexpected top-level modules: {sorted(found - known)}"


def test_core_imports_nothing_from_the_package():
    """`core` is the floor: it must never reach back into a subsystem."""
    edges, _ = _edges()
    offenders = sorted(t for (s, t) in edges if s.startswith("rister.core"))
    assert not offenders, f"core imports {offenders}"


def test_no_import_cycles():
    """A cycle would make the package impossible to import piecemeal."""
    edges, _ = _edges()
    graph: dict[str, set[str]] = {}
    for source, target in edges:
        graph.setdefault(_package_of(source), set()).add(target)

    visiting: set[str] = set()
    done: set[str] = set()
    path: list[str] = []

    def walk(node: str) -> None:
        if node in done:
            return
        assert node not in visiting, "import cycle: " + " -> ".join([*path, node])
        visiting.add(node)
        path.append(node)
        for nxt in sorted(graph.get(node, ())):
            walk(nxt)
        path.pop()
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        walk(node)


def test_dependency_matrix_is_exactly_as_documented():
    """Every non-core edge must be one of the two deliberate cross-links."""
    edges, _ = _edges()
    allowed = {
        ("core", "state"), ("core", "errors"), ("core", "types"),  # intra-package
    }
    extra = set()
    for source, target in edges:
        pkg = _package_of(source)
        if pkg in COMPOSITION_ROOTS:
            continue
        if target in ("core", "errors", "types"):
            continue
        if pkg == target:
            continue
        extra.add((pkg, target))
    assert extra <= ALLOWED_EXTRA, (
        "undocumented cross-module dependency: "
        f"{sorted(extra - ALLOWED_EXTRA)}"
    )


def test_subsystems_do_not_import_everything():
    """Outside the composition roots, no module may import more than two subsystems."""
    edges, _ = _edges()
    counts: dict[str, set[str]] = {}
    for source, target in edges:
        pkg = _package_of(source)
        if pkg in COMPOSITION_ROOTS or pkg == "core":
            continue
        if target in ("core", "errors", "types") or target == pkg:
            continue
        counts.setdefault(pkg, set()).add(target)
    for pkg, targets in counts.items():
        assert len(targets) <= 2, f"{pkg} imports {sorted(targets)}"


def _optional_imports(tree: ast.AST) -> set[str]:
    """Names imported inside a try/except ImportError — allowed to be absent."""
    found: set[str] = set()

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try):
            catches_import = any(
                (isinstance(h.type, ast.Name)
                 and h.type.id in ("ImportError", "ModuleNotFoundError", "Exception"))
                or (isinstance(h.type, ast.Tuple)
                    and any(isinstance(e, ast.Name)
                            and e.id in ("ImportError", "ModuleNotFoundError")
                            for e in h.type.elts))
                for h in node.handlers
                if h.type is not None
            )
            for child in ast.walk(node):
                if child is node:
                    continue
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    names = ([a.name for a in child.names]
                             if isinstance(child, ast.Import)
                             else [child.module or ""])
                    if catches_import:
                        found.update(n.split(".")[0] for n in names)
            for child in node.body:
                visit(child, guarded or catches_import)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return found


def test_no_third_party_imports():
    """Only the standard library may be imported at module level.

    An optional dependency (imported inside a ``try`` that catches ImportError) is
    allowed — RISTER runs with zero of them installed, and says so clearly when one
    is needed.
    """
    stdlib = {
        "abc", "argparse", "ast", "base64", "collections", "contextlib", "copy",
        "csv", "dataclasses", "datetime", "difflib", "enum", "fnmatch", "hashlib",
        "hmac", "http", "importlib", "inspect", "io", "itertools", "json", "logging",
        "math", "os", "pathlib", "random", "re", "secrets", "shutil", "socket",
        "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
        "time", "types", "typing", "unicodedata", "urllib", "uuid", "warnings",
        "asyncio", "traceback", "contextvars", "concurrent", "gzip", "numbers",
        "__future__",
    }
    external: dict[str, str] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        optional = _optional_imports(tree)
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                roots = [node.module.split(".")[0]]
            for root in roots:
                if root in stdlib or root == "rister":
                    continue
                if root in optional:
                    continue  # legitimately optional; not required at runtime
                external[root] = str(path.relative_to(ROOT))
    assert not external, f"unguarded third-party imports: {external}"


def test_public_surface_is_importable_and_unique():
    """The top-level API must be stable: every export resolves, and none is a duplicate."""
    import rister

    missing = [name for name in rister.__all__ if not hasattr(rister, name)]
    assert not missing, f"exports listed but not defined: {missing}"
    assert len(rister.__all__) == len(set(rister.__all__)), "duplicate entries in __all__"
    assert "__version__" in dir(rister)


def test_composition_roots_are_the_only_wide_importers():
    """Only the composition roots may import three or more subsystems.

    Everything else reaches at most one sibling — the two documented cross-links.
    """
    edges, _ = _edges()
    counts: dict[str, set[str]] = {}
    for source, target in edges:
        pkg = _package_of(source)
        if pkg in COMPOSITION_ROOTS:
            continue
        counts.setdefault(pkg, set()).add(target)
    wide = {pkg for pkg, targets in counts.items() if len(targets) >= 3}
    assert not wide, f"unexpected wide importers: {sorted(wide)}"
    for pkg, targets in counts.items():
        extra = {t for t in targets if t != "core"}
        assert extra <= {b for a, b in ALLOWED_EXTRA if a == pkg}, (
            f"{pkg} imports {sorted(extra)}, which is not a documented cross-link"
        )
