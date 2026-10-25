"""Architectural invariants — the shape of the package is a testable contract.

These tests read the source tree with :mod:`ast` and assert the dependency rules that
keep RISTER composable. They fail the build if a module reaches sideways, if a cycle
appears, or if the dependency-free core stops being dependency-free.
"""
from __future__ import annotations

import ast
import re
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
    ("mcp", "agent"),     # remote tools come back as ordinary Tool objects
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
             "evals", "mcp", "serve", "demo", "__main__", "__init__", "tests"}
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
        if _package_of(source) == target:
            continue  # a package importing its own submodules is not a cycle
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
    """Every non-core edge must be one of the three deliberate cross-links."""
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
        "queue", "sqlite3", "__future__",
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


# -- the published architecture page is a contract, not a snapshot ------------
#: ROOT is the project directory (rister/); the page lives at the repo root.
DOC = ROOT.parent / "docs" / "architecture.html"


def _significant(path: Path) -> int:
    """Lines that carry meaning: blank lines and comments are not code."""
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def _source_stats() -> dict[str, object]:
    """Measure the package the same way docs/architecture.html claims to."""
    files = sorted(PACKAGE.rglob("*.py"))
    source = [f for f in files if "tests" not in f.relative_to(PACKAGE).parts]

    classes = 0
    exceptions = 0
    per_module: dict[str, dict[str, int]] = {}
    for path in source:
        rel = path.relative_to(PACKAGE)
        module = rel.parts[0] if len(rel.parts) > 1 else path.stem
        bucket = per_module.setdefault(module, {"files": 0, "loc": 0})
        bucket["files"] += 1
        bucket["loc"] += _significant(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.col_offset == 0 and not node.name.startswith("_"):
                classes += 1
            for base in node.bases:
                label = getattr(base, "id", None) or getattr(base, "attr", None) or ""
                if label.endswith("Error") and label != node.name:
                    exceptions += 1
                    break

    packages = sorted(
        p.name
        for p in PACKAGE.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and p.name not in {"tests", "__pycache__"}
    )
    root_modules = [p.stem for p in PACKAGE.glob("*.py")]
    test_functions = sum(
        1
        for path in sorted((PACKAGE / "tests").glob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    exports = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    names = [
        elt.value
        for node in ast.walk(exports)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "__all__" for t in node.targets)
        for elt in getattr(node.value, "elts", [])
        if isinstance(elt, ast.Constant)
    ]

    return {
        "files": len(files),
        "loc": sum(_significant(f) for f in files),
        "classes": classes,
        "exceptions": exceptions,
        "modules": len(packages) + len(root_modules),
        "tests": test_functions,
        "exports": len(names),
        "per_module": per_module,
        "packages": packages,
    }


def test_documented_counts_match_the_source():
    """Every number on the architecture page is recomputed from the AST here."""
    if not DOC.exists():  # pragma: no cover - the page ships with the repo
        raise AssertionError(f"{DOC} is missing")
    page = DOC.read_text(encoding="utf-8")
    stats = _source_stats()

    def stat(label: str) -> int:
        match = re.search(
            r'<div class="n">([\d,]+)</div><div class="k">' + re.escape(label) + r"</div>", page
        )
        assert match, f"the page no longer reports {label!r}"
        return int(match.group(1).replace(",", ""))

    def chip(label: str) -> int:
        match = re.search(
            r'<div class="chip"><b>([\d,]+)</b> ' + re.escape(label) + r"</div>", page
        )
        assert match, f"the page no longer reports a {label!r} chip"
        return int(match.group(1).replace(",", ""))

    assert stat("files") == stats["files"], "file count on the page is stale"
    assert stat("significant LOC") == stats["loc"], "LOC on the page is stale"
    assert stat("public classes") == stats["classes"], "class count on the page is stale"
    assert stat("exception types") == stats["exceptions"], "exception count is stale"
    assert stat("modules") == stats["modules"], "module count on the page is stale"
    assert chip("tests") == stats["tests"], "test count on the page is stale"
    assert chip("exports") == stats["exports"], "export count on the page is stale"
    assert chip("runtime deps") == 0

    footer = re.search(r"(\d+) files · ([\d,]+) significant LOC</div>", page)
    assert footer, "the footer no longer states its counts"
    assert int(footer.group(1)) == stats["files"]
    assert int(footer.group(2).replace(",", "")) == stats["loc"]


def test_documented_modules_match_the_source():
    """Each module card states the real file and line counts for that package."""
    page = DOC.read_text(encoding="utf-8")
    listed = re.findall(r'\{id:"(\w+)", loc:(\d+), files:"(\d+) files?"', page)
    assert listed, "no module cards found"
    per_module = _source_stats()["per_module"]
    for module, loc, files in listed:
        assert module in per_module, f"the page documents {module!r}, which does not exist"
        assert int(loc) == per_module[module]["loc"], f"{module}: LOC on the page is stale"
        assert int(files) == per_module[module]["files"], f"{module}: file count is stale"
    claimed = {module for module, _, _ in listed}
    undocumented = set(per_module) - claimed - {"__init__", "__main__"}
    assert not undocumented, f"modules missing from the page: {sorted(undocumented)}"


def test_every_test_file_is_registered():
    """A test file that the runner cannot see is worse than no test file."""
    from rister.tests.__main__ import MODULES

    on_disk = {
        f"rister.tests.{path.stem}"
        for path in (PACKAGE / "tests").glob("test_*.py")
    }
    assert on_disk <= set(MODULES), f"never discovered: {sorted(on_disk - set(MODULES))}"


def test_version_has_one_source_of_truth():
    """The version is stated in pyproject, the page and the banners — all must agree."""
    import rister
    from rister.core.meta import __version__ as canonical
    from rister.mcp.client import DEFAULT_CLIENT_INFO
    from rister.serve import App

    assert rister.__version__ == canonical
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{canonical}"' in pyproject, "pyproject.toml disagrees with core.meta"
    page = DOC.read_text(encoding="utf-8")
    assert f"RISTER {canonical} · generated" in page, "architecture page states another version"
    assert DEFAULT_CLIENT_INFO["version"] == canonical
    assert App().handle("GET", "/health")[1]["version"] == canonical


def test_prose_files_quote_the_real_test_count():
    """The READMEs, the Makefile and the design doc all state the suite size."""
    stats = _source_stats()
    files = [
        ROOT.parent / "README.md",
        ROOT / "README.md",
        ROOT.parent / "Makefile",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        stated = {
            int(value.replace(",", ""))
            for value in re.findall(r"(?<![\d,])(\d[\d,]*) tests\b", text)
        }
        assert stated <= {stats["tests"]}, f"{path.name} quotes a stale test count: {stated}"
    design = ROOT.parent / "docs" / "07-rister.md"
    match = re.search(r"\*\*Tests:\*\* (\d+)", design.read_text(encoding="utf-8"))
    assert match, "docs/07-rister.md no longer states a test count"
    assert int(match.group(1)) == stats["tests"], "docs/07-rister.md quotes a stale test count"
