"""Canonical slot enumeration over FFedit JSON payloads.

A *slot* is a single mutable scalar integer inside an FFedit feature payload,
addressed by a JSON path. Both `lock` and `unlock` must enumerate slots in
exactly the same order, so the ordering rules here are normative:

* ``list``  -> visited in index order, 0..n-1
* ``dict``  -> visited in ``sorted(keys)`` order (lexicographic on the raw
  JSON key string; FFedit uses stringified integers for macroblock indices)
* scalars   -> ``int`` only. ``bool`` is excluded (it is an ``int`` subclass in
  Python but never appears as a codec value), and ``None`` is skipped, because
  a null cell means "this block is not coded" and inventing a value there
  would change the bitstream structure.

Anything that is not a list/dict/int is left untouched.
"""

from __future__ import annotations

from typing import Any, Iterator, List, Tuple, Union

PathKey = Union[int, str]
Path = Tuple[PathKey, ...]
Slot = Tuple[Any, PathKey, Path]


def walk(node: Any, path: Path = ()) -> Iterator[Slot]:
    """Yield ``(container, key, path)`` for every scalar int under *node*.

    The container/key pair is returned rather than the value so callers can
    write back in place without re-walking the tree.
    """
    if isinstance(node, list):
        items: Any = enumerate(node)
    elif isinstance(node, dict):
        items = ((k, node[k]) for k in sorted(node.keys()))
    else:
        return

    for key, child in items:
        if isinstance(child, (list, dict)):
            yield from walk(child, path + (key,))
        elif isinstance(child, int) and not isinstance(child, bool):
            yield (node, key, path + (key,))


def resolve(root: Any, path: Path) -> Tuple[Any, PathKey]:
    """Return the ``(container, key)`` addressed by *path* within *root*."""
    node = root
    for key in path[:-1]:
        node = node[key]
    return node, path[-1]


def path_to_json(path: Path) -> List[PathKey]:
    return list(path)


def path_from_json(raw: List[PathKey]) -> Path:
    return tuple(raw)
