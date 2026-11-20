"""Case-insensitive, non-mutating search over the full XML document.

The GUI's Treeview may show only a subset of nodes (lazy population), so
searching the ElementTree directly ensures collapsed nodes are included.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def find_xml_nodes(root: ET.Element, query: str) -> list[tuple[ET.Element, str | None]]:
    """Return each matching element once and a suitable attribute to highlight.

    A value match (e.g. ``Cave``) selects the corresponding field in the
    right-hand attribute table; a tag match selects just the element.
    """
    needle = query.strip().casefold()
    if not needle:
        return []
    results: list[tuple[ET.Element, str | None]] = []
    for node in root.iter():
        if needle in node.tag.casefold():
            results.append((node, None))
            continue
        matched_attr = next((name for name, value in node.attrib.items()
                             if needle in name.casefold() or needle in value.casefold()), None)
        if matched_attr is not None:
            results.append((node, matched_attr))
        elif node.text and needle in node.text.casefold():
            results.append((node, None))
    return results
