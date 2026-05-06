"""
graph.py — Build a dependency graph and compute blast radius.

Nodes are relative_path strings. An edge A → B means "A imports B".
After raw edges are added, a resolution pass expands __init__.py re-exports
so that importers of a package are directly coupled to the package's source
modules rather than being 2 hops away through the __init__.py intermediary.
"""

import logging
from collections import deque

import networkx as nx

from .models import BlastRadiusEntry, FileProfile

logger = logging.getLogger(__name__)


def build_graph(profiles: list[FileProfile]) -> nx.DiGraph:
    """Build a directed dependency graph from a list of FileProfiles.

    Nodes are relative_path strings. An edge A → B means A imports B.
    All project files appear as nodes — isolated files (zero edges) are
    included so that DEAD_CODE detection can query zero-in-degree nodes.

    After raw edges are added, __init__.py re-exports are resolved so that
    a file importing ``services`` (which maps to ``services/__init__.py``)
    gets a direct edge to ``services/auth.py`` when __init__.py re-exports
    from there. This keeps blast radius distances accurate.
    """
    graph = nx.DiGraph()

    # Add every file as a node first so isolated files are not invisible.
    for profile in profiles:
        graph.add_node(profile.relative_path)

    known = {p.relative_path for p in profiles}

    for profile in profiles:
        for imported in profile.imports:
            if imported in known:
                graph.add_edge(profile.relative_path, imported)

    _resolve_init_reexports(graph, profiles)

    return graph


def _resolve_init_reexports(
    graph: nx.DiGraph,
    profiles: list[FileProfile],
) -> None:
    """Add direct edges from __init__.py importers to its re-exported sources.

    Without this pass a file that does ``import services`` (resolved to
    ``services/__init__.py``) sits 2 hops from ``services/auth.py`` even
    though it is directly coupled to it via the re-export. This pass adds
    the 1-hop direct edge, making blast radius distances accurate.

    The __init__.py node and its own edges are kept — only additional edges
    are added, nothing is removed.
    """
    profile_map = {p.relative_path: p for p in profiles}

    init_nodes = [
        n
        for n in list(graph.nodes)
        if n == "__init__.py" or n.endswith("/__init__.py")
    ]

    for init_path in init_nodes:
        init_profile = profile_map.get(init_path)
        if not init_profile:
            continue

        importers = list(graph.predecessors(init_path))
        reexported = [t for t in init_profile.imports if graph.has_node(t)]

        for importer in importers:
            for target in reexported:
                if target != importer:
                    graph.add_edge(importer, target)


def blast_radius(
    graph: nx.DiGraph,
    source: str,
    depth: int,
) -> list[BlastRadiusEntry]:
    """Return every node that depends on ``source``, up to ``depth`` hops away.

    BFS runs on the reversed graph. In the original graph an edge A → B
    means "A imports B". Reversing gives B → A, so successors of B are all
    files that import it. BFS from ``source`` in the reversed graph therefore
    finds every file that would be affected if ``source`` breaks or changes —
    which is the correct definition of downstream blast radius.

    Results are sorted by (distance, node) for deterministic output.
    ``source`` itself is excluded from the returned entries.
    """
    if depth <= 0:
        logger.warning("blast_radius_depth %d is invalid; defaulting to 3", depth)
        depth = 3

    if source not in graph:
        return []

    # copy=False avoids materialising a new graph object; we only read from it.
    rev = graph.reverse(copy=False)

    entries: list[BlastRadiusEntry] = []
    visited: set[str] = {source}
    queue: deque[tuple[str, int]] = deque([(source, 0)])

    while queue:
        node, dist = queue.popleft()
        if dist >= depth:
            continue
        for neighbor in rev.successors(node):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            entries.append(
                BlastRadiusEntry(
                    node=neighbor,
                    distance=dist + 1,
                    # In-degree in the original graph = number of files that
                    # import this node = its own downstream fan-out potential.
                    incoming_edges=graph.in_degree(neighbor),
                )
            )
            queue.append((neighbor, dist + 1))

    return sorted(entries, key=lambda e: (e.distance, e.node))
