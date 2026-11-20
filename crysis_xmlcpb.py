#!/usr/bin/env python3
"""Read CryEngine XMLCPB binary XML documents and emit readable XML.

XMLCPB reader/writer adapted from the MIT-licensed preysave.py reference
implementation (Shere-Khan510/prey-save-editor). It supports the 16-bit
Crysis 2/3 Remastered and 32-bit Crysis Remastered attribute-set layouts.
Encryption handling is in crysis_remastered_decrypt.py.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

XMLCPB_MAGIC = 0x50425830  # bytes: b"0XBP"

T_STR, T_INT32, T_F1, T_F3, T_F4, T_INT64, T_RAW = range(7)
T_POS16, T_NEG16, T_POS8, T_NEG8, T_F1_1DEC = range(7, 12)
T_F3_1C, T_F3_2C, T_F3_3C, T_F4_1C, T_F4_2C, T_F4_3C, T_F4_4C = range(12, 19)
T_0, T_10, T_255 = 19, 29, 30
T_F3_100, T_F3_010, T_F3_001 = 31, 32, 33
T_CONSTSTR = 34

CONST_STRINGS = [
    "n", "table", "RigidBodyEx", "bRigidBodyActive", "false", "true",
    "Idle", "bActive", "Light", "h", "s", "vec", "ParticleEffect",
    "SmartObject", "b", "TagPoint", "GrabableLedge", "health", "nParticleSlot",
    "zero", "enabled", "state", "Alive", "currentSlot", "LastHit", "pos",
    "impulse", "shooterId", "bIgnoreObstruction", "bIgnoreCulling",
]

_FIXED_SIZE = {
    12: 9, 13: 5, 14: 1,
    15: 13, 16: 9, 17: 5, 18: 1,
}
_F32 = struct.Struct("<f")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

# Reversible XML-safe representations for XMLCPB names/strings that XML 1.0
# cannot express directly. The prefixes are reserved and escaped on export.
_XMLCPB_TAG_ESCAPE_PREFIX = "_XMLCPB_TAGHEX_"
_XMLCPB_STRING_ESCAPE_PREFIX = "__XMLCPB_STRHEX__"


class XmlCpbError(ValueError):
    pass


class Attr:
    __slots__ = ("name", "type", "value")

    def __init__(self, name: str, type_: int, value):
        self.name = name
        self.type = type_
        self.value = value


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs=None):
        self.tag = tag
        self.attrs = attrs or []
        self.children = []
        self.parent = None


def _read_strtable(data: bytes, pos: int, count: int, byte_size: int):
    if count < 0 or byte_size < 0 or pos + 4 * count + byte_size > len(data):
        raise XmlCpbError("Invalid string table bounds.")
    offsets = struct.unpack_from(f"<{count}I", data, pos) if count else ()
    base = pos + 4 * count
    out = []
    for off in offsets:
        if off >= byte_size:
            raise XmlCpbError("String offset is outside the table.")
        end = data.find(b"\0", base + off, base + byte_size)
        if end < 0:
            raise XmlCpbError("Missing null terminator in XMLCPB string table.")
        out.append(data[base + off:end].decode("utf-8", "replace"))
    return out, base + byte_size


def detect_attribute_set_entry_width(data: bytes, fields: tuple[int, ...]) -> int:
    """Detect XMLCPB's attribute-set entry width (2 bytes for Crysis 2/3, 4 for CR1)."""
    if len(fields) != 16 or fields[0] != XMLCPB_MAGIC:
        raise XmlCpbError("Invalid XMLCPB header.")
    ntags, stags, nnames, snames, nstrs, sstrs, nsets, ssets, nodes_size, _ = fields[5:15]
    if nodes_size > len(data):
        raise XmlCpbError("Invalid XMLCPB node-section size.")
    pos = nodes_size
    _, pos = _read_strtable(data, pos, ntags, stags)
    _, pos = _read_strtable(data, pos, nnames, snames)
    _, pos = _read_strtable(data, pos, nstrs, sstrs)
    if pos + 3 * nsets + ssets == len(data):
        return 2
    if pos + 5 * nsets + ssets == len(data):
        return 4
    raise XmlCpbError(
        f"XMLCPB attribute-set layout does not match the header (pos={pos}, "
        f"nsets={nsets}, ssets={ssets}, total={len(data)})."
    )


def _read_cvec(data: bytes, pos: int, n: int):
    if pos >= len(data):
        raise XmlCpbError("Truncated XMLCPB vector.")
    flags = data[pos]
    pos += 1
    values = []
    for i in range(n):
        mode = (flags >> (2 * i)) & 3
        if mode == 0:
            values.append(0.0)
        elif mode == 1:
            values.append(1.0)
        elif mode == 2:
            values.append(-1.0)
        else:
            if pos + 4 > len(data):
                raise XmlCpbError("Truncated XMLCPB vector component.")
            values.append(_F32.unpack_from(data, pos)[0])
            pos += 4
    return tuple(values), pos


def parse_xmlcpb(data: bytes, fields: tuple[int, ...]):
    """Parse decompressed XMLCPB content using its 16-word 64-byte header."""
    if len(fields) != 16 or fields[0] != XMLCPB_MAGIC:
        raise XmlCpbError("Invalid XMLCPB header.")

    (ntags, stags, nnames, snames, nstrs, sstrs, nsets, ssets,
     nodes_size, num_nodes) = fields[5:15]
    if nodes_size > len(data) or num_nodes == 0:
        raise XmlCpbError("Invalid XMLCPB node-section size.")

    nodes = data[:nodes_size]
    pos = nodes_size
    tags, pos = _read_strtable(data, pos, ntags, stags)
    names, pos = _read_strtable(data, pos, nnames, snames)
    strings, pos = _read_strtable(data, pos, nstrs, sstrs)

    # Crysis 2/3 Remastered use 16-bit offsets and attribute headers;
    # Crysis Remastered uses 32-bit offsets and 32-bit attribute headers.
    if pos + 3 * nsets + ssets == len(data):
        entry_width = 2
        set_offsets = struct.unpack_from(f"<{nsets}H", data, pos + nsets) if nsets else ()
    elif pos + 5 * nsets + ssets == len(data):
        entry_width = 4
        set_offsets = struct.unpack_from(f"<{nsets}I", data, pos + nsets) if nsets else ()
    else:
        raise XmlCpbError(
            f"XMLCPB size does not match the header (pos={pos}, "
            f"nsets={nsets}, ssets={ssets}, total={len(data)})."
        )
    counts = data[pos:pos + nsets]
    set_data = data[pos + (1 + entry_width) * nsets:pos + (1 + entry_width) * nsets + ssets]
    entry_code = "H" if entry_width == 2 else "I"
    sets = []
    for i, count in enumerate(counts):
        off = set_offsets[i]
        if off + entry_width * count > len(set_data):
            raise XmlCpbError("Attribute set exceeds the attribute-set table.")
        vals = struct.unpack_from(f"<{count}{entry_code}", set_data, off) if count else ()
        sets.append(vals)

    objs = []
    child_refs = []
    p = 0
    node_id = 0

    while p < nodes_size:
        if p + 2 > nodes_size:
            raise XmlCpbError("Truncated node header.")
        h = _U16.unpack_from(nodes, p)[0]
        p += 2
        tag_id = h & 0x3FF
        if tag_id >= len(tags):
            raise XmlCpbError(f"Invalid tag identifier: {tag_id}.")
        sequence = (h >> 14) & 1
        has_attrs = h >> 15
        child_count = (h >> 12) & 3
        starts = []
        consecutive = False

        if child_count == 3:
            if p >= nodes_size:
                raise XmlCpbError("Truncated child count.")
            c = nodes[p]
            p += 1
            if c & 0x80:
                if p >= nodes_size:
                    raise XmlCpbError("Truncated extended child count.")
                child_count = ((c << 8) | nodes[p]) & 0x3FFF
                p += 1
            elif c & 0x40:
                child_count = (c & 7) + 2
                consecutive = True
                starts = [node_id - ((c >> 3) & 7) - child_count - 1]
            else:
                child_count = c

        blocks = (1 if child_count & 0x3F else 0) + (child_count >> 6)
        set_id = None
        if has_attrs:
            if p >= nodes_size:
                raise XmlCpbError("Missing attribute-set identifier.")
            set_id = ((h >> 2) & 0x300) | nodes[p]
            p += 1
            if set_id == 0x3FF:
                if p + 2 > nodes_size:
                    raise XmlCpbError("Truncated extended attribute-set identifier.")
                set_id = _U16.unpack_from(nodes, p)[0]
                p += 2
            if set_id >= len(sets):
                raise XmlCpbError(f"Invalid attribute set: {set_id}.")

        if consecutive:
            pass
        elif sequence:
            if child_count:
                starts = [node_id - child_count]
        elif blocks == 1:
            if p >= nodes_size:
                raise XmlCpbError("Missing child distance.")
            c = nodes[p]
            p += 1
            if not c & 0x80:
                distance = c
            else:
                if p + 2 > nodes_size:
                    raise XmlCpbError("Truncated extended child distance.")
                distance = ((c & 0x3F) << 16) | _U16.unpack_from(nodes, p)[0]
                p += 2
            starts = [node_id - distance - child_count]
        elif blocks > 1:
            remaining = child_count
            for _ in range(blocks):
                if p + 3 > nodes_size:
                    raise XmlCpbError("Truncated child-block table.")
                c = nodes[p]
                distance = ((c & 0x3F) << 16) | _U16.unpack_from(nodes, p + 1)[0]
                p += 3
                block_count = min(64, remaining)
                remaining -= block_count
                starts.append(node_id - block_count - distance)

        kids = []
        for i in range(child_count):
            ref_index = starts[i >> 6] + (i & 0x3F)
            if ref_index < 0 or ref_index >= node_id:
                raise XmlCpbError(f"Invalid child index {ref_index} in node {node_id}.")
            kids.append(ref_index)

        attrs = []
        if has_attrs:
            for attr_header in sets[set_id]:
                value_type = attr_header & 0x3F
                name_id = attr_header >> 6
                if name_id >= len(names):
                    raise XmlCpbError(f"Invalid attribute-name identifier: {name_id}.")
                name = names[name_id]

                if value_type == T_STR:
                    if p + 2 > nodes_size:
                        raise XmlCpbError("Truncated attribute string index.")
                    str_id = _U16.unpack_from(nodes, p)[0]
                    p += 2
                    if str_id >= len(strings):
                        raise XmlCpbError("Invalid attribute string index.")
                    value = strings[str_id]
                elif value_type >= T_CONSTSTR:
                    const_id = value_type - T_CONSTSTR
                    if const_id >= len(CONST_STRINGS):
                        raise XmlCpbError(f"Unknown constant string: {value_type}.")
                    value = CONST_STRINGS[const_id]
                elif value_type == T_INT32:
                    value = struct.unpack_from("<i", nodes, p)[0]; p += 4
                elif value_type == T_F1:
                    value = _F32.unpack_from(nodes, p)[0]; p += 4
                elif value_type == T_F3:
                    value = struct.unpack_from("<3f", nodes, p); p += 12
                elif value_type == T_F4:
                    value = struct.unpack_from("<4f", nodes, p); p += 16
                elif value_type == T_INT64:
                    value = struct.unpack_from("<q", nodes, p)[0]; p += 8
                elif value_type == T_RAW:
                    raw_len = _U32.unpack_from(nodes, p)[0]
                    if p + 4 + raw_len > nodes_size:
                        raise XmlCpbError("Truncated raw attribute.")
                    value = bytes(nodes[p + 4:p + 4 + raw_len]); p += 4 + raw_len
                elif value_type == T_POS16:
                    value = _U16.unpack_from(nodes, p)[0]; p += 2
                elif value_type == T_NEG16:
                    value = -_U16.unpack_from(nodes, p)[0]; p += 2
                elif value_type == T_POS8:
                    value = nodes[p]; p += 1
                elif value_type == T_NEG8:
                    value = -nodes[p]; p += 1
                elif value_type == T_F1_1DEC:
                    value = struct.unpack_from("<b", nodes, p)[0] * 0.1
                    value = _F32.unpack(_F32.pack(value))[0]
                    p += 1
                elif T_F3_1C <= value_type <= T_F3_3C:
                    value, p = _read_cvec(nodes, p, 3)
                elif T_F4_1C <= value_type <= T_F4_4C:
                    value, p = _read_cvec(nodes, p, 4)
                elif T_0 <= value_type <= T_10:
                    value = value_type - T_0
                elif value_type == T_255:
                    value = 255
                elif value_type == T_F3_100:
                    value = (1.0, 0.0, 0.0)
                elif value_type == T_F3_010:
                    value = (0.0, 1.0, 0.0)
                elif value_type == T_F3_001:
                    value = (0.0, 0.0, 1.0)
                else:
                    raise XmlCpbError(f"Unknown attribute type: {value_type}.")
                if p > nodes_size:
                    raise XmlCpbError("Attribute read exceeded the node section.")
                attrs.append(Attr(name, value_type, value))

        objs.append(Node(tags[tag_id], attrs))
        child_refs.append(kids)
        node_id += 1

    if node_id != num_nodes or p != nodes_size:
        raise XmlCpbError(
            f"Node count or read position does not match the header: "
            f"nodes={node_id}/{num_nodes}, pos={p}/{nodes_size}."
        )

    for node, kids in zip(objs, child_refs):
        for child_id in kids:
            child = objs[child_id]
            child.parent = node
            node.children.append(child)
    return objs[-1], fields


def _is_xml10_text(value: str) -> bool:
    return all(
        ch in "\t\n\r" or "\x20" <= ch <= "\ud7ff"
        or "\ue000" <= ch <= "\ufffd"
        or "\U00010000" <= ch <= "\U0010ffff"
        for ch in value
    )


def _xml_safe_tag(tag: str) -> str:
    # Restrict direct names to a conservative ASCII XML-name subset. Any other
    # name is represented as UTF-8 hex and decoded again by the rebuild path.
    import re
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", tag) and not tag.startswith(_XMLCPB_TAG_ESCAPE_PREFIX):
        return tag
    return _XMLCPB_TAG_ESCAPE_PREFIX + tag.encode("utf-8").hex()


def _decode_xmlcpb_tag(tag: str) -> str:
    if not tag.startswith(_XMLCPB_TAG_ESCAPE_PREFIX):
        return tag
    encoded = tag[len(_XMLCPB_TAG_ESCAPE_PREFIX):]
    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise XmlCpbError(f"Invalid escaped XMLCPB element name: {tag!r}") from exc


def _xml_value(value):
    if isinstance(value, str):
        if value.startswith(_XMLCPB_STRING_ESCAPE_PREFIX) or not _is_xml10_text(value):
            return _XMLCPB_STRING_ESCAPE_PREFIX + value.encode("utf-8").hex()
        return value
    if isinstance(value, tuple):
        return ",".join(format(float(v), ".9g") for v in value)
    if isinstance(value, bytes):
        return "raw:" + value.hex()
    if isinstance(value, float):
        return format(value, ".9g")
    return str(value)


def _decode_xmlcpb_string(text: str) -> str:
    if not text.startswith(_XMLCPB_STRING_ESCAPE_PREFIX):
        return text
    encoded = text[len(_XMLCPB_STRING_ESCAPE_PREFIX):]
    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise XmlCpbError("Invalid escaped XMLCPB string attribute") from exc


def to_xml(root: Node) -> str:
    def build(node: Node):
        element = ET.Element(_xml_safe_tag(node.tag))
        for attr in node.attrs:
            element.set(attr.name, _xml_value(attr.value))
        for child in node.children:
            element.append(build(child))
        return element

    root_element = build(root)
    ET.indent(root_element, space="  ")
    return ET.tostring(root_element, encoding="unicode", xml_declaration=True)


def parse_csf_decrypted_bytes(container: bytes):
    """Parse a decrypted CSF container entirely in memory.

    The function validates the XMLCPB MD5 trailer, expands every zlib chunk,
    and converts the XMLCPB document into an ElementTree without creating
    intermediate files on disk.
    """
    if len(container) < 64:
        raise XmlCpbError("CSF file is too short to contain an XMLCPB header.")
    trailer = container[-64:]
    fields = struct.unpack("<16I", trailer)
    if fields[0] != XMLCPB_MAGIC:
        raise XmlCpbError(f"Missing 0XBP signature in the final 64 bytes: {trailer[:4]!r}.")

    compressed_body = container[:-64]
    zeroed_trailer = trailer[:4] + bytes(16) + trailer[20:]
    actual_md5 = hashlib.md5(compressed_body + zeroed_trailer).digest()
    if actual_md5 != trailer[4:20]:
        raise XmlCpbError("XMLCPB MD5 mismatch; the container may be corrupted.")

    expanded = bytearray()
    offset = 0
    while offset < len(compressed_body):
        if offset + 8 > len(compressed_body):
            raise XmlCpbError("Truncated zlib block header.")
        compressed_len, plain_len = struct.unpack_from("<II", compressed_body, offset)
        if compressed_len == 0 or plain_len == 0 or offset + 8 + compressed_len > len(compressed_body):
            raise XmlCpbError("Invalid zlib block size.")
        block = compressed_body[offset + 8:offset + 8 + compressed_len]
        try:
            plain = zlib.decompress(block)
        except zlib.error as exc:
            raise XmlCpbError(f"Zlib decompression failed: {exc}") from exc
        if len(plain) != plain_len:
            raise XmlCpbError(
                f"Decompressed block size mismatch ({len(plain)} != {plain_len})."
            )
        expanded.extend(plain)
        offset += 8 + compressed_len

    root, _ = parse_xmlcpb(bytes(expanded), fields)
    # Build the editable ElementTree directly from the in-memory XML string.
    return ET.fromstring(to_xml(root)), fields, len(expanded)


def parse_csf_decrypted(path: Path, out_path: Path):
    """Parse a decrypted CSF file and write a readable XML export."""
    root, fields, expanded_size = parse_csf_decrypted_bytes(path.read_bytes())
    xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(xml, encoding="utf-8")
    return root, fields, expanded_size


# XMLCPB writer (16-bit Crysis 2/3 and 32-bit Crysis Remastered variants)

def _type_kind(type_id: int) -> str:
    if type_id == T_STR or type_id >= T_CONSTSTR:
        return "str"
    if type_id in (T_INT32, T_POS16, T_NEG16, T_POS8, T_NEG8) or T_0 <= type_id <= T_255:
        return "int"
    if type_id in (T_F1, T_F1_1DEC):
        return "float"
    if type_id in (T_F3, T_F3_1C, T_F3_2C, T_F3_3C, T_F3_100, T_F3_010, T_F3_001):
        return "vec3"
    if type_id in (T_F4, T_F4_1C, T_F4_2C, T_F4_3C, T_F4_4C):
        return "vec4"
    if type_id == T_INT64:
        return "int64"
    if type_id == T_RAW:
        return "raw"
    raise XmlCpbError(f"Unsupported XMLCPB attribute type: {type_id}")


def _parse_xml_value(text: str, template_attr: Attr | None = None):
    """Convert an XML attribute string while retaining the template's data kind."""
    import re

    text = _decode_xmlcpb_string(text)
    if template_attr is not None:
        kind = _type_kind(template_attr.type)
        if kind == "str":
            return text
        if kind in ("int", "int64"):
            try:
                return int(text.strip(), 10)
            except ValueError:
                try:
                    number = float(text.strip())
                except ValueError as exc:
                    raise XmlCpbError(f"Attribute {template_attr.name!r} must contain a number: {text!r}") from exc
                if number.is_integer():
                    return int(number)
                return T_F1, number
        if kind == "float":
            try:
                return float(text.strip())
            except ValueError as exc:
                raise XmlCpbError(f"Attribute {template_attr.name!r} must contain a number: {text!r}") from exc
        if kind in ("vec3", "vec4"):
            parts = [part.strip() for part in text.split(",")]
            expected = 3 if kind == "vec3" else 4
            if len(parts) != expected:
                raise XmlCpbError(f"Attribute {template_attr.name!r} must contain {expected} comma-separated values")
            try:
                return tuple(float(part) for part in parts)
            except ValueError as exc:
                raise XmlCpbError(f"Attribute {template_attr.name!r} contains an invalid vector: {text!r}") from exc
        if kind == "raw":
            if not text.startswith("raw:"):
                raise XmlCpbError(f"Raw attribute {template_attr.name!r} must use raw:HEX notation")
            try:
                return bytes.fromhex(text[4:])
            except ValueError as exc:
                raise XmlCpbError(f"Raw attribute {template_attr.name!r} contains invalid hexadecimal data") from exc

    if text.startswith("raw:"):
        try:
            return T_RAW, bytes.fromhex(text[4:])
        except ValueError as exc:
            raise XmlCpbError("Raw attributes must use raw:HEX notation") from exc

    # Infer common scalar types for attributes that do not exist in the template.
    stripped = text.strip()
    if re.fullmatch(r"[+-]?\d+", stripped):
        return T_INT32, int(stripped, 10)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?|[+-]?\d+[eE][+-]?\d+", stripped):
        return T_F1, float(stripped)
    if "," in stripped:
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) in (3, 4):
            try:
                vector = tuple(float(part) for part in parts)
                return (T_F3 if len(parts) == 3 else T_F4), vector
            except ValueError:
                pass
    return T_STR, text


def xml_element_to_node(element, template_node: Node | None = None) -> Node:
    """Create the internal tree from XML, reusing attribute types from a template tree."""
    if not isinstance(element.tag, str):
        raise XmlCpbError("XML comments and processing instructions are not supported as save nodes")
    template_attrs = {attr.name: attr for attr in template_node.attrs} if template_node else {}
    attrs = []
    for name, text in element.attrib.items():
        original = template_attrs.get(name)
        parsed = _parse_xml_value(text, original)
        if isinstance(parsed, tuple) and len(parsed) == 2 and isinstance(parsed[0], int) and parsed[0] in range(64):
            type_id, value = parsed
        elif original is not None:
            type_id, value = original.type, parsed
        else:
            type_id, value = parsed
        attrs.append(Attr(name, type_id, value))

    node = Node(_decode_xmlcpb_tag(element.tag), attrs)
    if element.text and element.text.strip():
        raise XmlCpbError(f"Text content is not supported in XMLCPB save nodes: <{element.tag}>")

    template_children_by_tag: dict[str, list[Node]] = {}
    if template_node is not None:
        for child in template_node.children:
            template_children_by_tag.setdefault(child.tag, []).append(child)
    used_per_tag: dict[str, int] = {}

    for child_element in list(element):
        if not isinstance(child_element.tag, str):
            continue
        template_child = None
        child_tag = _decode_xmlcpb_tag(child_element.tag)
        candidates = template_children_by_tag.get(child_tag, [])
        used = used_per_tag.get(child_element.tag, 0)
        if used < len(candidates):
            template_child = candidates[used]
            used_per_tag[child_element.tag] = used + 1
        child = xml_element_to_node(child_element, template_child)
        child.parent = node
        node.children.append(child)
        if child_element.tail and child_element.tail.strip():
            raise XmlCpbError(f"Mixed text content is not supported after <{child_element.tag}>")
    return node


def _choose_integer_type(value: int) -> tuple[int, bytes]:
    if 0 <= value <= 10:
        return T_0 + value, b""
    if value == 255:
        return T_255, b""
    if 0 < value < 256:
        return T_POS8, bytes([value])
    if -256 < value < 0:
        return T_NEG8, bytes([-value])
    if 0 < value < 65536:
        return T_POS16, _U16.pack(value)
    if -65536 < value < 0:
        return T_NEG16, _U16.pack(-value)
    if -(2 ** 31) <= value < 2 ** 31:
        return T_INT32, struct.pack("<i", value)
    if -(2 ** 63) <= value < 2 ** 63:
        return T_INT64, struct.pack("<q", value)
    raise XmlCpbError(f"Integer is outside the supported 64-bit range: {value}")


def _encode_compressed_vector(values: tuple[float, ...]) -> tuple[int, bytes]:
    flags = 0
    body = bytearray()
    constant_count = 0
    for index, raw_value in enumerate(values):
        value = _F32.unpack(_F32.pack(float(raw_value)))[0]
        if value == 0.0:
            mode = 0
        elif value == 1.0:
            mode = 1
        elif value == -1.0:
            mode = 2
        else:
            mode = 3
            body.extend(_F32.pack(value))
        if mode != 3:
            constant_count += 1
        flags |= mode << (2 * index)
    return constant_count, bytes([flags]) + bytes(body)


def _encode_attribute(attr: Attr, strings: list[str], string_index: dict[str, int]) -> tuple[int, bytes]:
    type_id = attr.type
    value = attr.value
    kind = _type_kind(type_id)

    if kind == "str":
        value = str(value)
        if type_id >= T_CONSTSTR and CONST_STRINGS[type_id - T_CONSTSTR] == value:
            return type_id, b""
        const_index = {text: index for index, text in enumerate(CONST_STRINGS)}.get(value)
        if const_index is not None and type_id != T_STR:
            return T_CONSTSTR + const_index, b""
        index = string_index.get(value)
        if index is None:
            index = len(strings)
            if index > 0xFFFF:
                raise XmlCpbError("XMLCPB string table exceeds the 16-bit string-index limit")
            strings.append(value)
            string_index[value] = index
        return T_STR, _U16.pack(index)

    if kind == "int":
        integer = int(value)
        if type_id == T_INT32 and -(2 ** 31) <= integer < 2 ** 31:
            return type_id, struct.pack("<i", integer)
        if type_id == T_POS16 and 0 <= integer <= 0xFFFF:
            return type_id, _U16.pack(integer)
        if type_id == T_NEG16 and -0xFFFF <= integer <= 0:
            return type_id, _U16.pack(-integer)
        if type_id == T_POS8 and 0 <= integer <= 0xFF:
            return type_id, bytes([integer])
        if type_id == T_NEG8 and -0xFF <= integer <= 0:
            return type_id, bytes([-integer])
        if T_0 <= type_id <= T_10 and integer == type_id - T_0:
            return type_id, b""
        if type_id == T_255 and integer == 255:
            return type_id, b""
        return _choose_integer_type(integer)

    if kind == "float":
        number = float(value)
        if type_id == T_F1_1DEC:
            scaled = round(number / _F32.unpack(_F32.pack(0.1))[0])
            if -128 <= scaled <= 127:
                reconstructed = _F32.unpack(_F32.pack(scaled * _F32.unpack(_F32.pack(0.1))[0]))[0]
                if reconstructed == _F32.unpack(_F32.pack(number))[0]:
                    return type_id, struct.pack("<b", scaled)
        return T_F1, _F32.pack(number)

    if kind in ("vec3", "vec4"):
        values = tuple(float(component) for component in value)
        component_count = 3 if kind == "vec3" else 4
        if len(values) != component_count:
            raise XmlCpbError(f"Attribute {attr.name!r} requires {component_count} vector components")
        if type_id == T_F3_100 and values == (1.0, 0.0, 0.0):
            return type_id, b""
        if type_id == T_F3_010 and values == (0.0, 1.0, 0.0):
            return type_id, b""
        if type_id == T_F3_001 and values == (0.0, 0.0, 1.0):
            return type_id, b""
        if type_id == (T_F3 if component_count == 3 else T_F4):
            return type_id, struct.pack("<%df" % component_count, *values)
        constant_count, payload = _encode_compressed_vector(values)
        if constant_count == 0:
            return (T_F3 if component_count == 3 else T_F4), struct.pack("<%df" % component_count, *values)
        base_type = T_F3_1C if component_count == 3 else T_F4_1C
        return base_type + constant_count - 1, payload

    if kind == "int64":
        integer = int(value)
        if not -(2 ** 63) <= integer < 2 ** 63:
            raise XmlCpbError(f"Attribute {attr.name!r} is outside the signed 64-bit range")
        return T_INT64, struct.pack("<q", integer)

    if kind == "raw":
        raw = bytes(value)
        return T_RAW, struct.pack("<I", len(raw)) + raw

    raise XmlCpbError(f"Cannot encode attribute {attr.name!r}")


def serialize_xmlcpb(
    root: Node,
    original_fields: tuple[int, ...] | list[int],
    attribute_set_entry_width: int = 4,
) -> tuple[bytes, list[int]]:
    """Serialize XMLCPB, retaining the template's 2- or 4-byte set-entry layout."""
    if len(original_fields) != 16 or original_fields[0] != XMLCPB_MAGIC:
        raise XmlCpbError("A valid 16-word XMLCPB header is required as the template")
    if attribute_set_entry_width not in (2, 4):
        raise XmlCpbError("XMLCPB attribute-set entry width must be 2 or 4 bytes")

    tags: list[str] = []
    names: list[str] = []
    strings: list[str] = []
    tag_index: dict[str, int] = {}
    name_index: dict[str, int] = {}
    string_index: dict[str, int] = {}
    attribute_sets: list[tuple[int, ...]] = []
    attribute_set_index: dict[tuple[int, ...], int] = {}

    def intern(table: list[str], index: dict[str, int], value: str) -> int:
        if value not in index:
            index[value] = len(table)
            table.append(value)
        return index[value]

    # XMLCPB stores child nodes before their parent and flushes sibling references in groups of 64.
    ordered_nodes: list[Node] = []
    stack: list[tuple[Node, int, list[Node]]] = [(root, 0, [])]
    while stack:
        current, child_index, pending = stack.pop()
        if child_index > 0:
            pending.append(current.children[child_index - 1])
            if len(pending) == 64:
                ordered_nodes.extend(pending)
                pending = []
        if child_index < len(current.children):
            stack.append((current, child_index + 1, pending))
            stack.append((current.children[child_index], 0, []))
        else:
            ordered_nodes.extend(pending)
    ordered_nodes.append(root)
    node_ids = {id(node): index for index, node in enumerate(ordered_nodes)}

    node_data = bytearray()
    for node_id, node in enumerate(ordered_nodes):
        tag_id = intern(tags, tag_index, node.tag)
        if tag_id > 0x3FF:
            raise XmlCpbError("XMLCPB supports at most 1024 distinct element names")

        attribute_headers: list[int] = []
        attribute_data = bytearray()
        for attr in node.attrs:
            encoded_type, payload = _encode_attribute(attr, strings, string_index)
            name_id = intern(names, name_index, attr.name)
            name_id_bits = 10 if attribute_set_entry_width == 2 else 26
            max_name_id = (1 << name_id_bits) - 1
            if name_id > max_name_id:
                raise XmlCpbError(
                    f"XMLCPB attribute-name identifier exceeds its {name_id_bits}-bit field"
                )
            attribute_headers.append((name_id << 6) | encoded_type)
            attribute_data.extend(payload)

        child_count = len(node.children)
        if child_count > 0x3FFF:
            raise XmlCpbError(f"Element <{node.tag}> has too many children for XMLCPB")
        header = tag_id
        extra = bytearray()
        if attribute_headers:
            header |= 0x8000

        blocks = (1 if child_count & 0x3F else 0) + (child_count >> 6)
        starts: list[int] = []
        for block_index in range(blocks):
            block_start_node = node.children[64 * block_index]
            start_id = node_ids[id(block_start_node)]
            block_children = node.children[64 * block_index:64 * block_index + 64]
            for offset, child in enumerate(block_children):
                if node_ids[id(child)] != start_id + offset:
                    raise XmlCpbError("XMLCPB child blocks are not contiguous; check node ordering")
            starts.append(start_id)

        distance = node_id - starts[0] - child_count if blocks == 1 else None
        sequence = blocks == 1 and distance == 0
        consecutive = blocks == 1 and 3 <= child_count <= 9 and 1 <= distance <= 8
        if sequence:
            header |= 0x4000

        if child_count < 3:
            header |= child_count << 12
        else:
            header |= 3 << 12
            if consecutive:
                extra.append(0x40 | ((distance - 1) << 3) | (child_count - 2))
            elif child_count < 0x40:
                extra.append(child_count)
            else:
                extra.extend((0x80 | (child_count >> 8), child_count & 0xFF))

        if attribute_headers:
            set_key = tuple(attribute_headers)
            set_id = attribute_set_index.get(set_key)
            if set_id is None:
                set_id = len(attribute_sets)
                if set_id > 0xFFFF:
                    raise XmlCpbError("XMLCPB supports at most 65536 distinct attribute sets")
                attribute_set_index[set_key] = set_id
                attribute_sets.append(set_key)
            if set_id < 0x3FF:
                header |= ((set_id >> 8) & 3) << 10
                extra.append(set_id & 0xFF)
            else:
                header |= 3 << 10
                extra.append(0xFF)
                extra.extend(_U16.pack(set_id))

        if child_count and not sequence and not consecutive:
            if blocks == 1:
                if distance < 0x80:
                    extra.append(distance)
                else:
                    if distance > 0x3FFFFF:
                        raise XmlCpbError("XMLCPB child-distance field overflow")
                    extra.extend((0xC0 | (distance >> 16),))
                    extra.extend(_U16.pack(distance & 0xFFFF))
            else:
                remaining = child_count
                for block_index in range(blocks):
                    block_count = min(64, remaining)
                    remaining -= block_count
                    block_distance = node_id - block_count - starts[block_index]
                    if block_distance > 0x3FFFFF:
                        raise XmlCpbError("XMLCPB child-distance field overflow")
                    extra.extend((0xC0 | (block_distance >> 16),))
                    extra.extend(_U16.pack(block_distance & 0xFFFF))

        node_data.extend(_U16.pack(header))
        node_data.extend(extra)
        node_data.extend(attribute_data)

    def write_string_table(output: bytearray, values: list[str]) -> tuple[int, int]:
        encoded = [value.encode("utf-8") + b"\0" for value in values]
        offsets = []
        offset = 0
        for item in encoded:
            offsets.append(offset)
            offset += len(item)
        if offset > 0xFFFFFFFF:
            raise XmlCpbError("XMLCPB string table exceeds the 32-bit size limit")
        if offsets:
            output.extend(struct.pack("<%dI" % len(offsets), *offsets))
        output.extend(b"".join(encoded))
        return len(values), offset

    output = bytearray(node_data)
    tag_count, tag_size = write_string_table(output, tags)
    name_count, name_size = write_string_table(output, names)
    string_count, string_size = write_string_table(output, strings)

    set_blob = bytearray()
    set_offsets: list[int] = []
    for attribute_set in attribute_sets:
        if len(attribute_set) > 0xFF:
            raise XmlCpbError("An XMLCPB attribute set cannot contain more than 255 attributes")
        set_offsets.append(len(set_blob))
        if attribute_set:
            if attribute_set_entry_width == 2:
                if any(value > 0xFFFF for value in attribute_set):
                    raise XmlCpbError("XMLCPB 16-bit attribute header overflow")
                set_blob.extend(struct.pack("<%dH" % len(attribute_set), *attribute_set))
            else:
                set_blob.extend(struct.pack("<%dI" % len(attribute_set), *attribute_set))
    size_limit = 0xFFFF if attribute_set_entry_width == 2 else 0xFFFFFFFF
    if len(set_blob) > size_limit or any(offset > size_limit for offset in set_offsets):
        raise XmlCpbError(
            f"XMLCPB attribute-set data exceeds the {attribute_set_entry_width * 8}-bit size limit"
        )

    # Both variants use u8 set counts; Crysis 3 uses u16 offsets/headers,
    # while Crysis Remastered uses u32 offsets/headers.
    output.extend(bytes(len(attribute_set) for attribute_set in attribute_sets))
    if set_offsets:
        offset_code = "H" if attribute_set_entry_width == 2 else "I"
        output.extend(struct.pack("<%d%s" % (len(set_offsets), offset_code), *set_offsets))
    output.extend(set_blob)

    fields = list(original_fields)
    fields[0] = XMLCPB_MAGIC
    fields[5:15] = [
        tag_count, tag_size,
        name_count, name_size,
        string_count, string_size,
        len(attribute_sets), len(set_blob),
        len(node_data), len(ordered_nodes),
    ]
    return bytes(output), fields


def pack_csf_container(xmlcpb_data: bytes, fields: list[int] | tuple[int, ...], chunk_size: int = 0x8000) -> bytes:
    """Build the decrypted CSF container: chunked zlib data + MD5-protected XMLCPB header."""
    import hashlib
    import zlib

    if len(fields) != 16:
        raise XmlCpbError("XMLCPB header must contain exactly 16 32-bit words")
    if chunk_size <= 0:
        raise XmlCpbError("Chunk size must be positive")

    compressed_body = bytearray()
    for offset in range(0, len(xmlcpb_data), chunk_size):
        block = xmlcpb_data[offset:offset + chunk_size]
        compressed = zlib.compress(block, 5)
        compressed_body.extend(struct.pack("<II", len(compressed), len(block)))
        compressed_body.extend(compressed)

    header_fields = list(fields)
    header_fields[0] = XMLCPB_MAGIC
    header_fields[1:5] = [0, 0, 0, 0]
    header = struct.pack("<16I", *header_fields)
    digest = hashlib.md5(bytes(compressed_body) + header).digest()
    header = header[:4] + digest + header[20:]
    return bytes(compressed_body) + header


def verify_csf_container(container: bytes) -> tuple[bytes, tuple[int, ...]]:
    """Validate the CSF container MD5 and decompress all zlib blocks."""
    import hashlib
    import zlib

    if len(container) < 64:
        raise XmlCpbError("CSF container is too short")
    trailer = container[-64:]
    fields = struct.unpack("<16I", trailer)
    if fields[0] != XMLCPB_MAGIC:
        raise XmlCpbError("CSF container is missing the 0XBP trailer")
    zeroed = trailer[:4] + bytes(16) + trailer[20:]
    if hashlib.md5(container[:-64] + zeroed).digest() != trailer[4:20]:
        raise XmlCpbError("CSF container MD5 validation failed")

    body = container[:-64]
    expanded = bytearray()
    offset = 0
    while offset < len(body):
        if offset + 8 > len(body):
            raise XmlCpbError("Truncated zlib chunk header")
        compressed_size, uncompressed_size = struct.unpack_from("<II", body, offset)
        end = offset + 8 + compressed_size
        if compressed_size == 0 or end > len(body):
            raise XmlCpbError("Invalid zlib chunk size")
        try:
            block = zlib.decompress(body[offset + 8:end])
        except zlib.error as exc:
            raise XmlCpbError(f"Invalid zlib block: {exc}") from exc
        if len(block) != uncompressed_size:
            raise XmlCpbError("Zlib chunk expanded size does not match its header")
        expanded.extend(block)
        offset = end
    return bytes(expanded), fields


def _node_signature(node: Node):
    def normalize(value):
        if isinstance(value, float):
            return _F32.unpack(_F32.pack(value))[0]
        if isinstance(value, tuple):
            return tuple(normalize(component) for component in value)
        return value

    return (
        node.tag,
        tuple((attr.name, normalize(attr.value)) for attr in node.attrs),
        tuple(_node_signature(child) for child in node.children),
    )

def main():
    parser = argparse.ArgumentParser(description="Convert a decrypted CSF XMLCPB container to readable XML.")
    parser.add_argument("input", type=Path, help="Decrypted .csfbin container (after removing file encryption)")
    parser.add_argument("-o", "--output", type=Path, help="Output XML path")
    args = parser.parse_args()
    target = args.output or args.input.with_suffix(".xml")
    try:
        root, fields, size = parse_csf_decrypted(args.input, target)
    except (OSError, XmlCpbError, struct.error, IndexError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"[OK] XMLCPB: {fields[14]:,} nodes, {size:,} data bytes -> {target}")
    print(f"[ROOT] {root.tag}; attributes={len(root.attrs)}; children={len(root.children)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
