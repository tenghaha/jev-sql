"""Connectivity graph over tables, built from foreign keys.

Nodes are tables, edges are foreign keys (child -> parent) carrying the column
pairs.  The graph is also reported undirected (adjacency, connected components,
isolated tables) because that is what a table-selection step needs: "which
tables can be reached from this one, and through which keys".
"""

from __future__ import annotations

__all__ = ["build_graph"]


def build_graph(tables: list[str], fk_by_table: dict[str, list[dict]]) -> dict:
    nodes = list(tables)
    edges: list[dict] = []
    adj: dict[str, set] = {t: set() for t in nodes}

    for child in nodes:
        for fk in fk_by_table.get(child, []):
            parent = fk["to_table"]
            if parent not in adj:            # FK to a table/ view that we did not profile
                continue
            edges.append({"from": child, "to": parent,
                          "pairs": [list(p) for p in fk["pairs"]],
                          "on_delete": fk.get("on_delete"),
                          "on_update": fk.get("on_update")})
            adj[child].add(parent)
            adj[parent].add(child)

    adjacency = {t: sorted(adj[t]) for t in nodes}
    return {
        "nodes": nodes,
        "edge_source": "foreign_key",   # PRAGMA foreign_key_list only
        "edges": edges,
        "adjacency": adjacency,
        "degree": {t: len(adj[t]) for t in nodes},
        "components": _components(nodes, adj),
        "isolated": [t for t in nodes if not adj[t]],
    }


def _components(nodes: list[str], adj: dict[str, set]) -> list[list[str]]:
    """Connected components, largest first (union-find)."""
    parent = {t: t for t in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in nodes:
        for b in adj[a]:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

    groups: dict[str, list[str]] = {}
    for t in nodes:
        groups.setdefault(find(t), []).append(t)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))
