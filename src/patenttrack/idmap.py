"""Single responsibility: map identifier occurrences of one record to a
canonical *application identity* (the idmap stage).

The stage owns identity only.  It does **not** decide whether a record is a
duplicate, an event or an independent application; that is ``dedup``'s job.
It answers one question: *which application do these numbers denote?*

Identity rules
--------------
* A canonical identifier is ``REGION:NAMESPACE:BODY``.  Region is part of the
  identity, so ``CN:APP:123`` and ``US:APP:123`` are **different**
  applications even though the body is equal.
* Within one record, one application number and same-region document numbers
  (publication / grant / registration) are linked.
* Two *different* application numbers are never linked to each other, and no
  chain of shared document numbers may join them.  If two records claim the
  same document number but different application numbers, the later claim is
  refused and reported as conflict evidence (the applications stay separate).
* A result row carrying several application numbers is a listing, not
  evidence of identity: it is kept as its own ambiguous observation and the
  applications are not merged.
* Priority numbers are family evidence, never identity evidence.
* Linking is order-independent: a component's representative is the smallest
  canonical node in it, so shuffled input yields identical keys.
* Records with no identifier at all fall back to their record id (preserving
  the original pipeline's keys) and then a same-source content fingerprint.

``begin()`` resets the per-run registry; the pipeline calls it once per run.
``finalize()`` freezes the run and reports cross-region / unresolved conflicts.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import normalizer

_LINKABLE = ("publication", "grant", "registration")
_PRIORITY = "priority"
_APPLICATION = "application"


class _UnionFind:
    """Union-find whose representative is always the smallest node string.

    ``application_sets`` tracks the application nodes in each component.  A
    union that would put two *different* application nodes in one component is
    refused and reported by the caller, which is what prevents family listings
    and shared publication numbers from merging distinct applications.
    """

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.apps: dict[str, set[str]] = {}

    def add(self, node: str) -> str:
        if node not in self.parent:
            self.parent[node] = node
            self.apps[node] = {node} if node.split(":")[1:2] == ["APP"] else set()
        return node

    def find(self, node: str) -> str:
        self.add(node)
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def apps_of(self, node: str) -> set[str]:
        return set(self.apps.get(self.find(node), set()))

    def union(self, left: str, right: str) -> tuple[bool, str | None]:
        """Union two nodes.  Returns ``(merged, conflict)``.

        ``conflict`` is ``"application_conflict"`` when the components carry
        different application nodes; in that case nothing is merged.
        """
        a, b = self.find(left), self.find(right)
        if a == b:
            return True, None
        apps_a, apps_b = self.apps.get(a, set()), self.apps.get(b, set())
        if apps_a and apps_b and apps_a != apps_b:
            return False, "application_conflict"
        root, child = (a, b) if a < b else (b, a)
        self.parent[child] = root
        self.apps[root] = apps_a | apps_b
        self.apps.pop(child, None)
        return True, None

    def components(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for node in sorted(self.parent):
            result.setdefault(self.find(node), []).append(node)
        return result


class _Registry:
    def __init__(self) -> None:
        self.uf = _UnionFind()
        # (namespace, body) -> {region: set(nodes)} for known regions only.
        self.body_regions: dict[tuple[str, str], dict[str, set[str]]] = {}

    def reset(self) -> None:
        self.uf = _UnionFind()
        self.body_regions = {}

    def register(self, item: dict[str, Any]) -> None:
        namespace, body, region = item["namespace"], item["body"], item["region"]
        if namespace == _PRIORITY or region == "??":
            return
        self.body_regions.setdefault((namespace, body), {}).setdefault(region, set()).add(
            item["canonical"]
        )


_registry = _Registry()
_final_conflicts: list[dict[str, Any]] = []
_finalized = False


def begin() -> None:
    global _finalized, _final_conflicts
    _registry.reset()
    _final_conflicts = []
    _finalized = False


def reset() -> None:
    begin()


def _nodes_by_namespace(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    ids = record.get("ids")
    if not isinstance(ids, dict):
        ids = normalizer.extract_identifiers(record)
    return ids


def _flatten(ids: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for namespace in sorted(ids):
        items.extend(ids[namespace])
    return items


def _link(
    left: str,
    right: str,
    conflicts: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> None:
    merged, conflict = _registry.uf.union(left, right)
    if merged:
        links.append({"a": left, "b": right, "reason": "co_occurrence"})
        return
    conflicts.append(
        {
            "kind": conflict or "link_conflict",
            "values": [left, right],
            "reason": "different application numbers claim the same document "
                      "number; applications kept separate",
        }
    )


def process(record: dict[str, Any]) -> dict[str, Any]:
    """Annotate one record with its application-identity evidence."""
    rec = dict(record)
    ids = _nodes_by_namespace(rec)
    rec["ids"] = ids
    items = _flatten(ids)

    for item in items:
        _registry.uf.add(item["canonical"])
        _registry.register(item)

    conflicts: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    by_ns: dict[str, list[str]] = {}
    for item in items:
        bucket = by_ns.setdefault(item["namespace"], [])
        if item["canonical"] not in bucket:
            bucket.append(item["canonical"])
    for nodes in by_ns.values():
        nodes.sort()

    applications = by_ns.get(_APPLICATION, [])
    linkable = [node for ns in _LINKABLE for node in by_ns.get(ns, [])]

    anchor: str | None = None
    basis: str | None = None

    if len(applications) > 1:
        conflicts.append(
            {
                "kind": "multiple_applications",
                "namespace": _APPLICATION,
                "values": list(applications),
                "reason": "one row carries several application numbers; "
                          "treated as a listing, not merged",
            }
        )
        # Ambiguous row: give it its own identity so no application absorbs it.
        anchor = f"FP:{normalizer.record_fingerprint(rec)}"
        basis = "ambiguous_multiple_applications"
        _registry.uf.add(anchor)
    elif len(applications) == 1:
        anchor = applications[0]
        basis = "application_number"
        anchor_region = anchor.split(":", 1)[0]
        for node in linkable:
            if node.split(":", 1)[0] != anchor_region:
                conflicts.append(
                    {
                        "kind": "cross_region_document_in_row",
                        "values": [anchor, node],
                        "reason": "document numbers from different regions in "
                                  "one row are not merged into one application",
                    }
                )
                continue
            _link(anchor, node, conflicts, links)
    elif linkable:
        # No application number: the smallest same-region document anchors the
        # component.  Cross-region documents in the same row stay separate.
        anchor = linkable[0]
        anchor_region = anchor.split(":", 1)[0]
        basis = {
            "publication": "publication_number",
            "grant": "grant_number",
            "registration": "registration_number",
        }.get(anchor.split(":")[1].lower(), "document_number")
        for node in linkable[1:]:
            if node.split(":", 1)[0] != anchor_region:
                conflicts.append(
                    {
                        "kind": "cross_region_document_in_row",
                        "values": [anchor, node],
                        "reason": "document numbers from different regions in "
                                  "one row are not merged into one application",
                    }
                )
                continue
            _link(anchor, node, conflicts, links)

    if anchor is None:
        record_id = rec.get("id")
        if record_id not in (None, ""):
            anchor = f"REC:{record_id}"
            basis = "record_id"
        else:
            anchor = f"FP:{normalizer.record_fingerprint(rec)}"
            basis = "content_fingerprint"
        _registry.uf.add(anchor)

    priority_ids = by_ns.get(_PRIORITY, [])

    rec["idmap"] = {
        "application_key": anchor,
        "basis": basis,
        "nodes": sorted({item["canonical"] for item in items}),
        "component_nodes": [anchor] + [link["b"] for link in links],
        "identifiers": {namespace: list(nodes) for namespace, nodes in by_ns.items()},
        "links": links,
        "priority_ids": priority_ids,
        "conflicts": conflicts,
        "unknown_region": any(
            item["region"] == "??" and item["namespace"] != _PRIORITY for item in items
        ),
    }
    rec["_stage_idmap"] = "ok"
    return rec


def finalize() -> dict[str, Any]:
    """Freeze the run and report identifier conflicts.

    Unknown-region identifiers are **not** reconciled against known regions:
    guessing from a coincidentally equal body could merge different national
    applications and would make keys depend on batch composition.  They are
    reported instead.
    """
    global _finalized, _final_conflicts
    if _finalized:
        return _summary()

    conflicts: list[dict[str, Any]] = []

    for (namespace, body), regions in sorted(_registry.body_regions.items()):
        if len(regions) < 2:
            continue
        nodes = sorted({node for group in regions.values() for node in group})
        conflicts.append(
            {
                "kind": "cross_region_same_number",
                "namespace": namespace,
                "body": body,
                "regions": sorted(regions),
                "nodes": nodes,
                "reason": "same number body appears in several regions; "
                          "kept as separate applications",
            }
        )

    unknown: dict[tuple[str, str], set[str]] = {}
    for node in sorted(_registry.uf.parent):
        parts = node.split(":", 2)
        if len(parts) == 3 and parts[0] == "??":
            unknown.setdefault((parts[1].lower(), parts[2]), set()).add(node)
    for (code, body), nodes in sorted(unknown.items()):
        conflicts.append(
            {
                "kind": "unresolved_region",
                "namespace": code,
                "body": body,
                "regions": ["??"],
                "nodes": sorted(nodes),
                "reason": "no region evidence; kept separate to avoid a false merge",
            }
        )

    _final_conflicts = _dedupe_conflicts(conflicts)
    _finalized = True
    return _summary()


def _dedupe_conflicts(conflicts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for conflict in conflicts:
        key = (
            conflict.get("kind"),
            conflict.get("namespace"),
            conflict.get("body"),
            tuple(conflict.get("regions", [])),
            tuple(conflict.get("values", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(conflict)
    result.sort(
        key=lambda item: (
            item.get("kind", ""),
            item.get("namespace", ""),
            item.get("body", ""),
            tuple(item.get("regions", [])),
        )
    )
    return result


def conflicts() -> list[dict[str, Any]]:
    return list(_final_conflicts)


def final_key(record: dict[str, Any]) -> str:
    """Return the component representative of a record's application anchor."""
    idmap_data = record.get("idmap") or {}
    anchor = idmap_data.get("application_key")
    if not anchor:
        record_id = record.get("id")
        if record_id not in (None, ""):
            return f"REC:{record_id}"
        return f"FP:{normalizer.record_fingerprint(record)}"
    return _registry.uf.find(anchor)


def node_groups(records: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for record in records:
        root = final_key(record)
        for node in (record.get("idmap") or {}).get("nodes", []):
            bucket = groups.setdefault(root, [])
            if node not in bucket:
                bucket.append(node)
    for nodes in groups.values():
        nodes.sort()
    return groups


def _summary() -> dict[str, Any]:
    components = _registry.uf.components()
    return {
        "nodes": len(_registry.uf.parent),
        "components": len(components),
        "conflicts": len(_final_conflicts),
        "cross_region_conflicts": sum(
            1 for item in _final_conflicts if item.get("kind") == "cross_region_same_number"
        ),
    }


def summary() -> dict[str, Any]:
    return _summary()
