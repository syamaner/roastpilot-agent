"""Static governance: which executable tests read committed ``docs/**/*.md``.

This module is the single authoritative implementation consumed by both the
fast-path selection (``pyproject.toml``'s ``docs``/``docs_ci`` marker
inventory) and its own governance tests. It replaces the slice-1
module-level marker sweep (``pytest.mark.docs`` applied to an entire test
module) with an exact, per-function inventory, so a docs-only CI lane never
silently drags in an unrelated sibling test that merely shares a module with
a real docs reader, and never silently drops a genuine reader either.

The analysis is deliberately conservative in two different directions at
once, matching its two failure modes:

* **Under-detection (a real reader goes unmarked)** is caught downstream by
  the exact node-id equality test in ``tests/test_pytest_governance.py`` —
  if a marker is missing, the fast-path selection and the full-suite
  membership assertion there diverge and the governance test fails loudly.
* **Genuine ambiguity (the analysis cannot prove either way)** raises
  :class:`UnresolvedDocsReaderEdge` with the function name and the reason,
  rather than silently guessing "not a reader" (which would recreate the
  fail-open direction this module exists to close) or falling back to
  broad-marking the whole module (the slice-1 behaviour this module
  replaces).

The detector walks only the module's own top-level ``def``/``async def``
statements (no classes, matching this repository's plain-function test
convention) and a bounded same-module call/fixture graph: a helper function
or ``@pytest.fixture`` that itself reads docs content marks every test that
calls it or depends on it, transitively, with cycles resolved to "not a new
reader" rather than raised (test helper graphs in this repository are not
expected to recurse, and a missed transitive edge here is still caught by
the downstream equality test above).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

_READ_TEXT_ATTRS = frozenset({"read_text", "read_bytes"})
_OPEN_ATTR = "open"
_GLOB_ATTRS = frozenset({"glob", "rglob"})
_MD_GLOB_ARGS = frozenset({"*.md"})
_ITERABLE_WRAPPERS = frozenset({"sorted", "list", "tuple", "set", "reversed", "frozenset"})


class UnresolvedDocsReaderEdge(RuntimeError):
    """Raised when the analysis cannot prove whether a call reads docs content.

    This is deliberately narrow: it fires only when an expression is already
    known to be rooted in the committed ``docs/`` tree (a literal ``"docs"``
    path component was found somewhere in its construction) but the read
    target's exact-Markdown-file status cannot be established statically —
    for example a docs-rooted value threaded through a call this module does
    not model. Ordinary non-docs file I/O (the overwhelming majority of test
    code) never touches this path, because it never carries a ``"docs"``
    path-component constant in the first place.
    """


def _string_constants(expression: ast.expr) -> list[str]:
    """Return every string literal reachable from ``expression`` by any path."""

    return [
        node.value
        for node in ast.walk(expression)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _contains_docs_root_literal(expression: ast.expr) -> bool:
    """Return whether ``expression`` contains a literal ``docs`` path component."""

    return any(
        value == "docs" or value.startswith("docs/") for value in _string_constants(expression)
    )


def _contains_md_suffix_literal(expression: ast.expr) -> bool:
    """Return whether ``expression`` contains a literal string ending in ``.md``."""

    return any(value.endswith(".md") for value in _string_constants(expression))


def _referenced_names(expression: ast.expr) -> set[str]:
    """Return every bare name referenced anywhere inside ``expression``."""

    return {node.id for node in ast.walk(expression) if isinstance(node, ast.Name)}


@dataclass
class _AliasEnvironment:
    """Tracks which local names are known to be docs-rooted or exact-Markdown."""

    root_aliases: set[str] = field(default_factory=set[str])
    md_aliases: set[str] = field(default_factory=set[str])
    # Names bound to an iterable (list/tuple/generator/...) that yields at
    # least one exact-Markdown docs path when iterated (e.g. a mixed list of
    # ``Path`` objects, only some of which are under ``docs/``).
    md_iterable_aliases: set[str] = field(default_factory=set[str])

    def copy(self) -> _AliasEnvironment:
        """Return an independent copy for a nested (function-local) scope."""

        return _AliasEnvironment(
            set(self.root_aliases), set(self.md_aliases), set(self.md_iterable_aliases)
        )

    def is_root(self, expression: ast.expr) -> bool:
        """Return whether ``expression`` denotes something under ``docs/``."""

        if _contains_docs_root_literal(expression):
            return True
        return bool(_referenced_names(expression) & (self.root_aliases | self.md_aliases))

    def is_md(self, expression: ast.expr) -> bool:
        """Return whether ``expression`` denotes exactly one ``docs/**/*.md`` path.

        Container- and generator-shaped expressions (``list``/``tuple``/
        ``set``/comprehensions) and glob-family calls never satisfy this on
        their own — they can *yield* Markdown paths but are not themselves
        one, and treating them as one would wrongly pull a bare ``"*.md"``
        glob-pattern argument in as if it were the suffix of a real file
        path. Use :meth:`can_yield_md` for those shapes instead.
        """

        container_types = (
            ast.List,
            ast.Tuple,
            ast.Set,
            ast.ListComp,
            ast.SetComp,
            ast.GeneratorExp,
            ast.DictComp,
        )
        if isinstance(expression, container_types):
            return False
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr in _GLOB_ATTRS
        ):
            return False
        if isinstance(expression, ast.Name) and expression.id in self.md_aliases:
            return True
        if self.is_root(expression) and _contains_md_suffix_literal(expression):
            return True
        if (
            isinstance(expression, ast.Call)
            and not expression.keywords
            and len(expression.args) == 1
            and not isinstance(expression.args[0], ast.Starred)
        ):
            # A single-argument wrapper call (``Path(name)``, ``str(name)``,
            # a same-module normaliser, ...) around an already-exact md
            # value still denotes that same path. Tried only after the
            # direct literal check above, so a call that already carries its
            # own docs/*.md literal (e.g. ``"docs/{}.md".format(name)``) is
            # resolved by that more specific check, never re-derived from an
            # unrelated argument. This can only widen detection (a true
            # positive), never admit a non-md value.
            return self.is_md(expression.args[0])
        return False

    def can_yield_md(self, expression: ast.expr) -> bool:
        """Return whether iterating ``expression`` can produce an exact md path."""

        if isinstance(expression, ast.Name) and expression.id in self.md_iterable_aliases:
            return True
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr in _GLOB_ATTRS
            and self.is_root(expression.func.value)
            and any(
                isinstance(argument, ast.Constant) and argument.value in _MD_GLOB_ARGS
                for argument in expression.args
            )
        ):
            return True
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            for element in expression.elts:
                target = element.value if isinstance(element, ast.Starred) else element
                if self.can_yield_md(target) or self.is_md(target):
                    return True
            return False
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id in _ITERABLE_WRAPPERS
            and expression.args
        ):
            return self.can_yield_md(expression.args[0])
        if isinstance(expression, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            return self.is_md(expression.elt) or any(
                self.can_yield_md(generator.iter) for generator in expression.generators
            )
        return False


def _bind_assignment_targets(
    targets: list[ast.expr], value: ast.expr, environment: _AliasEnvironment
) -> None:
    """Fold one assignment's classification into ``environment``, name targets only."""

    names = [target.id for target in targets if isinstance(target, ast.Name)]
    if not names:
        return
    if environment.is_md(value):
        for name in names:
            environment.md_aliases.add(name)
            environment.root_aliases.add(name)
    elif environment.can_yield_md(value):
        for name in names:
            environment.md_iterable_aliases.add(name)
    elif environment.is_root(value):
        for name in names:
            environment.root_aliases.add(name)


def _bind_loop_target(target: ast.expr, iterable: ast.expr, environment: _AliasEnvironment) -> None:
    """Fold one ``for``/comprehension binding into ``environment``."""

    if not isinstance(target, ast.Name):
        return
    if environment.can_yield_md(iterable):
        environment.md_aliases.add(target.id)
        environment.root_aliases.add(target.id)
    elif environment.is_root(iterable):
        environment.root_aliases.add(target.id)


def _scoped_descendants(node: ast.AST, boundary: ast.AST) -> list[ast.AST]:
    """Return ``node`` and every descendant, never crossing a nested-scope boundary.

    Unlike :func:`ast.walk`, this stops recursing at any
    ``FunctionDef``/``AsyncFunctionDef``/``ClassDef``/``Lambda`` that is not
    ``boundary`` itself. This is what makes module-level alias resolution
    safe on a large module: a same-named local variable inside one of many
    unrelated top-level test functions can never leak into the module-wide
    alias environment that every other function's analysis starts from.
    """

    collected: list[ast.AST] = [node]
    for child in ast.iter_child_nodes(node):
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
            and child is not boundary
        ):
            continue
        collected.extend(_scoped_descendants(child, boundary))
    return collected


def _resolve_aliases(scope: ast.AST, environment: _AliasEnvironment, *, prune_nested: bool) -> None:
    """Populate ``environment`` from every binding statement inside ``scope``.

    Flat and order-tolerant by design (three passes cover the realistic
    dependency depth of test-module helper code); a binding that depends on
    another binding defined later in the same scope is picked up on retry.

    Args:
        scope: The AST subtree to analyse.
        environment: The alias environment to populate in place.
        prune_nested: When ``True`` (module-level resolution), never descend
            into a nested function/class/lambda body — each top-level
            function is its own independent scope. When ``False``
            (per-function resolution), nested helpers defined *inside* the
            function under analysis are treated as part of that function's
            own execution and are included.
    """

    for _ in range(3):
        before = (
            frozenset(environment.root_aliases),
            frozenset(environment.md_aliases),
            frozenset(environment.md_iterable_aliases),
        )
        nodes = _scoped_descendants(scope, scope) if prune_nested else ast.walk(scope)
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not scope:
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                _bind_assignment_targets(list(targets), node.value, environment)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                _bind_loop_target(node.target, node.iter, environment)
        after = (
            frozenset(environment.root_aliases),
            frozenset(environment.md_aliases),
            frozenset(environment.md_iterable_aliases),
        )
        if after == before:
            break


def _find_reads(scope: ast.AST, environment: _AliasEnvironment, function_name: str) -> bool:
    """Return whether ``scope`` contains a direct exact-Markdown docs read.

    Raises:
        UnresolvedDocsReaderEdge: If a read call's receiver is provably
            docs-rooted but not provably an exact ``.md`` file.
    """

    found = False
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not scope:
            continue
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and (
            node.func.attr in _READ_TEXT_ATTRS or node.func.attr == _OPEN_ATTR
        ):
            receiver = node.func.value
            if environment.is_md(receiver):
                found = True
            elif environment.is_root(receiver):
                raise UnresolvedDocsReaderEdge(
                    f"{function_name}: docs-rooted receiver of .{node.func.attr}() could not "
                    "be proven to be an exact docs/**/*.md path"
                )
        elif isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            receiver = node.args[0]
            if environment.is_md(receiver):
                found = True
            elif environment.is_root(receiver):
                raise UnresolvedDocsReaderEdge(
                    f"{function_name}: docs-rooted argument to open() could not be proven to "
                    "be an exact docs/**/*.md path"
                )
    return found


def _same_module_calls(scope: ast.AST, known_names: frozenset[str]) -> set[str]:
    """Return same-module function names ``scope`` calls or otherwise reaches."""

    calls: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not scope:
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in known_names
        ):
            calls.add(node.func.id)
    return calls


def _is_fixture_decorator(decorator: ast.expr) -> bool:
    """Return whether a decorator expression is ``pytest.fixture`` (called or not)."""

    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Attribute) and target.attr == "fixture"


def _parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return every positional/keyword parameter name of ``function``."""

    arguments = function.args
    return {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }


def docs_reading_tests(source: str) -> set[str]:
    """Return the exact set of top-level test function names that read docs content.

    A "reader" is a ``test_*`` function that, directly or through a bounded
    same-module call/fixture graph, executes a real read
    (``read_text``/``read_bytes``/``Path.open``/builtin ``open``) of an
    expression provably rooted at an exact ``docs/**/*.md`` path. Comments,
    docstrings, and non-executable text never count, because only ``ast.Call``
    nodes are inspected.

    Args:
        source: The Python source text of one test module.

    Returns:
        The set of ``test_`` function names classified as docs readers.

    Raises:
        UnresolvedDocsReaderEdge: If any function contains a call this
            analysis cannot classify with confidence (see the class
            docstring); this is intentionally louder than a silent guess.
    """

    tree = ast.parse(source)
    module_environment = _AliasEnvironment()
    _resolve_aliases(tree, module_environment, prune_nested=True)

    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {
        statement.name: statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    known_names = frozenset(functions)
    fixture_names = {
        name
        for name, function in functions.items()
        if any(_is_fixture_decorator(decorator) for decorator in function.decorator_list)
    }

    direct_reads: dict[str, bool] = {}
    call_edges: dict[str, set[str]] = {}
    for name, function in functions.items():
        local_environment = module_environment.copy()
        _resolve_aliases(function, local_environment, prune_nested=False)
        direct_reads[name] = _find_reads(function, local_environment, name)
        edges = _same_module_calls(function, known_names)
        edges |= _parameter_names(function) & fixture_names
        call_edges[name] = edges - {name}

    memo: dict[str, bool] = {}

    def resolve(name: str, stack: frozenset[str]) -> bool:
        if name in memo:
            return memo[name]
        if name in stack:
            return False
        if direct_reads.get(name):
            memo[name] = True
            return True
        result = any(
            resolve(callee, stack | {name})
            for callee in call_edges.get(name, ())
            if callee in functions
        )
        memo[name] = result
        return result

    return {name for name in functions if name.startswith("test_") and resolve(name, frozenset())}
