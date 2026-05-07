"""
graph.py — Build a dependency graph and compute blast radius.

Nodes are relative_path strings. An edge A → B means "A imports B".
After raw edges are added, a resolution pass expands __init__.py re-exports
so that importers of a package are directly coupled to the package's source
modules rather than being 2 hops away through the __init__.py intermediary.
"""

import json
import logging
from collections import deque

import networkx as nx

from .models import BlastRadiusEntry, FileProfile, Finding

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


def export_graph_html(
    graph: nx.DiGraph,
    findings: list[Finding],
    output_path: str = "codewatch_graph.html",
    subgraph_nodes: list[str] | None = None,
) -> None:
    """Export the dependency graph as an interactive HTML file using pyvis.

    Nodes are coloured by worst finding severity and sized by incoming-edge
    count (more depended-on = larger). When subgraph_nodes is provided only
    those nodes and the edges between them are rendered.
    """
    from pyvis.network import Network  # type: ignore

    g = graph.subgraph(subgraph_nodes).copy() if subgraph_nodes is not None else graph.copy()

    # Build file → worst severity map.
    sev_rank = {"high": 2, "medium": 1, "low": 0}
    file_max_sev: dict[str, str] = {}
    for f in findings:
        if sev_rank.get(f.severity, -1) > sev_rank.get(file_max_sev.get(f.file, ""), -1):
            file_max_sev[f.file] = f.severity

    def _color(node: str) -> str:
        sev = file_max_sev.get(node)
        if sev == "high":
            return "#e74c3c"
        if sev == "medium":
            return "#e67e22"
        if sev is not None:
            return "#2ecc71"  # low findings only — clean enough
        if node in {f.file for f in findings}:
            return "#2ecc71"
        return "#95a5a6"  # not referenced in any finding

    # Build file → findings list for tooltip.
    file_findings: dict[str, list[str]] = {}
    for f in findings:
        file_findings.setdefault(f.file, []).append(f"{f.rule}: {f.target}")

    # Drop orphan nodes (no edges in either direction — they add visual noise).
    orphans = [n for n in g.nodes() if graph.in_degree(n) == 0 and graph.out_degree(n) == 0]
    if orphans:
        g = g.copy()
        g.remove_nodes_from(orphans)
        logger.info("graph: skipped %d orphan nodes", len(orphans))

    total_nodes = len(g.nodes())
    title_text = f"codewatch · dependency graph · {total_nodes} files"

    if total_nodes > 100:
        sev_points = {"high": 3, "medium": 2, "low": 1}
        node_score: dict[str, float] = {}
        for node in g.nodes():
            score = float(graph.in_degree(node))
            for f in findings:
                if f.file == node:
                    score += sev_points.get(f.severity, 0)
            node_score[node] = score
        top20 = sorted(node_score, key=lambda n: node_score[n], reverse=True)[:20]
        g = g.subgraph(set(top20)).copy()
        logger.info(
            "graph: large repo (%d nodes) — rendering risk subgraph of top 20",
            total_nodes,
        )
        title_text = f"codewatch · risk subgraph · top 20 of {total_nodes} files"

    node_count = len(g.nodes())
    for node in g.nodes():
        incoming = graph.in_degree(node)
        outgoing = graph.out_degree(node)
        tooltip_lines = [node, f"imported by: {incoming} file{'s' if incoming != 1 else ''}"]
        if node in file_findings:
            tooltip_lines.append("")
            tooltip_lines.extend(file_findings[node])
        g.nodes[node]["color"] = _color(node)
        g.nodes[node]["size"] = min(50, 10 + max(incoming, outgoing) * 5)
        g.nodes[node]["title"] = "\n".join(tooltip_lines)
        g.nodes[node]["label"] = node.split("/")[-1]  # basename only; full path in tooltip

    net = Network(
        directed=True,
        height="800px",
        width="100%",
        bgcolor="#1e2327",
        font_color="#ffffff",
        select_menu=False,
        filter_menu=False,
    )

    options = {
        "interaction": {
            "dragNodes": True,
            "dragView": True,
            "zoomView": True,
            "zoomSpeed": 0.5,
            "navigationButtons": True,
            "keyboard": False,
        },
        "physics": {
            "enabled": True,
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {
                "gravitationalConstant": -120,
                "centralGravity": 0.005,
                "springLength": 250,
                "springConstant": 0.03,
                "damping": 0.9,
                "avoidOverlap": 1,
            },
            "stabilization": {
                "enabled": True,
                "fit": True,
                "iterations": 300,
            },
            "minVelocity": 0.75,
        },
        "layout": {
            "improvedLayout": True,
        },
    }
    net.set_options(json.dumps(options))
    net.from_nx(g)
    net.write_html(output_path)

    inject = (
        # Restyle navigation buttons: remove vis.js neon SVG icons, replace with plain black symbols.
        "<style>"
        ".vis-navigation .vis-button{"
        "background-color:#ffffff!important;"
        "background-image:none!important;"
        "border-radius:5px!important;"
        "border:none!important;"
        "box-shadow:0 1px 4px rgba(0,0,0,0.35)!important;"
        "display:flex!important;"
        "align-items:center!important;"
        "justify-content:center!important;"
        "color:#000000!important;"
        "font-size:15px!important;"
        "font-weight:bold!important;"
        "}"
        ".vis-navigation .vis-button:hover{background-color:#e8e8e8!important;}"
        ".vis-button.vis-up::before{content:'↑';}"
        ".vis-button.vis-down::before{content:'↓';}"
        ".vis-button.vis-left::before{content:'←';}"
        ".vis-button.vis-right::before{content:'→';}"
        ".vis-button.vis-zoomIn::before{content:'+';}"
        ".vis-button.vis-zoomOut::before{content:'−';}"
        ".vis-button.vis-zoomExtends::before{content:'⊞';}"
        "</style>"
        # Title — top-center, appears exactly once.
        '<div style="position:fixed;top:12px;left:50%;transform:translateX(-50%);'
        "color:#ffffff;font-size:14px;font-weight:600;font-family:monospace;"
        "background:rgba(0,0,0,0.55);padding:5px 18px;border-radius:6px;"
        f"white-space:nowrap;z-index:999;\">{title_text}</div>"
        # Legend — top-right, clear of navigation buttons which sit at bottom.
        '<div style="position:fixed;top:16px;right:16px;padding:10px 14px;'
        "background:rgba(0,0,0,0.6);border-radius:6px;color:#ffffff;font-size:12px;"
        "font-family:sans-serif;line-height:2;z-index:999;\">"
        "<div>🔴&nbsp; HIGH</div>"
        "<div>🟠&nbsp; MEDIUM</div>"
        "<div>🟢&nbsp; Clean</div>"
        '<div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        "background:#95a5a6;vertical-align:middle;margin-right:3px;\"></span>&nbsp; No findings</div>"
        "</div>"
    )
    with open(output_path, encoding="utf-8") as fh:
        html = fh.read()
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html.replace("</body>", inject + "</body>"))

    logger.info("graph written to %s", output_path)
