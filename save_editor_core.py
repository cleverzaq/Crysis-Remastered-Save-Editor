#!/usr/bin/env python3
"""Core operations for the Crysis Remastered Save Editor GUI.

Uses the existing decrypt/rebuild/XMLCPB implementation in this folder.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import csv
import json
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

from crysis_remastered_decrypt import decrypt_file_bytes, extract_cipher
from crysis_remastered_rebuild import rebuild_encrypted_xml, rebuild_save
from crysis_xmlcpb import parse_csf_decrypted, parse_csf_decrypted_bytes


class SaveEditorError(Exception):
    pass


@dataclass
class SaveDocument:
    path: Path
    exe_path: Path
    magic: bytes
    key: bytes
    kind: str
    root: ET.Element
    baseline_root: ET.Element | None
    file_size: int
    node_count: int
    source_hash: str
    decompressed_size: int | None = None


def serialize_xml(root: ET.Element, pretty: bool = True) -> bytes:
    clone = copy.deepcopy(root)
    if pretty:
        ET.indent(clone, space="  ")
    return ET.tostring(clone, encoding="utf-8", xml_declaration=True)


def _xml_signature(element: ET.Element):
    """Structural comparison that ignores indentation-only whitespace."""
    text = element.text if element.text and element.text.strip() else None
    tail = element.tail if element.tail and element.tail.strip() else None
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        text,
        tuple(_xml_signature(child) for child in list(element)),
        tail,
    )


def _equivalent_xml_value(expected: str, actual: str, *, binary_xml: bool) -> bool:
    """Accept lossless numeric formatting changes made by the XMLCPB codec.

    Do not normalize plain encrypted XML or arbitrary nonnumeric strings.
    Numeric equivalence is deliberately exact: no tolerance for changed values.
    """
    if expected == actual:
        return True
    if not binary_xml:
        return False
    # A decimal point or exponent distinguishes a float representation from
    # an ordinary integer/string identifier. Never accept NaN or infinity.
    if not any(c in expected + actual for c in '.eE'):
        return False
    try:
        a, b = Decimal(expected), Decimal(actual)
        return a.is_finite() and b.is_finite() and a == b
    except InvalidOperation:
        return False


def _xml_equivalent(expected: ET.Element, actual: ET.Element, *, binary_xml: bool) -> bool:
    """Compare XML iteratively, preserving tags, attribute names and child order."""
    stack = [(expected, actual)]
    while stack:
        left, right = stack.pop()
        if left.tag != right.tag or left.attrib.keys() != right.attrib.keys():
            return False
        if any(not _equivalent_xml_value(value, right.attrib[key], binary_xml=binary_xml)
               for key, value in left.attrib.items()):
            return False
        for field in ('text', 'tail'):
            a, b = getattr(left, field), getattr(right, field)
            if (a.strip() if a and a.strip() else None) != (b.strip() if b and b.strip() else None):
                return False
        if len(left) != len(right):
            return False
        stack.extend(zip(left, right))
    return True


def _count_nodes(root: ET.Element) -> int:
    return sum(1 for _ in root.iter())


def open_encrypted_save(save_path: Path | str, exe_path: Path | str, *, snapshot_baseline: bool = True) -> SaveDocument:
    save_path = Path(save_path).expanduser().resolve()
    exe_path = Path(exe_path).expanduser().resolve()
    if not save_path.is_file():
        raise SaveEditorError(f"Save file not found: {save_path}")
    if not exe_path.is_file():
        raise SaveEditorError(f"Game EXE not found: {exe_path}")

    magic, key = extract_cipher(exe_path)
    encrypted = save_path.read_bytes()
    decrypted = decrypt_file_bytes(encrypted, key, save_path.name, magic)
    suffix = save_path.suffix.lower()

    decompressed_size = None
    if suffix == ".csf":
        try:
            # Encrypted bytes, decrypted CSF data, expanded zlib/XMLCPB data,
            # and the editable ElementTree are processed in memory only.
            root, _, decompressed_size = parse_csf_decrypted_bytes(decrypted)
        except Exception as exc:
            raise SaveEditorError(f"Unable to read the CSF/XMLCPB container: {exc}") from exc
        kind = "csf"
    elif suffix in (".meta", ".xml"):
        try:
            root = ET.fromstring(decrypted)
        except ET.ParseError as exc:
            raise SaveEditorError(f"The decrypted file is not valid XML: {exc}") from exc
        kind = "xml"
    else:
        raise SaveEditorError("Supported files: .CSF, .meta, and encrypted .xml")

    return SaveDocument(
        path=save_path,
        exe_path=exe_path,
        magic=magic,
        key=key,
        kind=kind,
        root=root,
        baseline_root=copy.deepcopy(root) if snapshot_baseline else None,
        file_size=len(encrypted),
        node_count=_count_nodes(root),
        source_hash=hashlib.sha256(encrypted).hexdigest(),
        decompressed_size=decompressed_size,
    )


def create_backup_file(source_path: Path | str, label: str | None = None) -> Path:
    """Create a timestamped backup beside a save file without modifying it."""
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise SaveEditorError(f"File not found: {source}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = f"-{label}" if label else ""
    backup = source.with_name(f"{source.name}.bak{suffix}-{stamp}")
    try:
        shutil.copy2(source, backup)
    except OSError as exc:
        raise SaveEditorError(f"Unable to create backup: {exc}") from exc
    return backup


def list_backup_files(target_path: Path | str) -> list[Path]:
    """Return timestamped backups that belong to the given original file."""
    target = Path(target_path).expanduser().resolve()
    if not target.parent.is_dir():
        return []
    return sorted(
        (p for p in target.parent.glob(f"{target.name}.bak-*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def restore_backup_file(
    backup_path: Path | str, target_path: Path | str, exe_path: Path | str
) -> tuple[SaveDocument, Path]:
    """Validate a backup, preserve the current file, then atomically restore it."""
    backup = Path(backup_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    exe = Path(exe_path).expanduser().resolve()
    if not backup.is_file():
        raise SaveEditorError(f"Backup file not found: {backup}")
    if not target.is_file():
        raise SaveEditorError(f"Original save file not found: {target}")
    if backup.parent != target.parent or not backup.name.startswith(target.name + ".bak-"):
        raise SaveEditorError("The selected backup does not belong to the currently opened save.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staged = target.with_name(f".{target.stem}.restore-{stamp}{target.suffix}")
    rollback_backup: Path | None = None
    try:
        shutil.copy2(backup, staged)
        # Keep the original extension on the staged file so the correct parser is used.
        open_encrypted_save(staged, exe)
        rollback_backup = create_backup_file(target, label="pre-restore")
        os.replace(staged, target)
        try:
            restored = open_encrypted_save(target, exe)
        except Exception:
            shutil.copy2(rollback_backup, target)
            raise
        return restored, rollback_backup
    except SaveEditorError:
        raise
    except Exception as exc:
        raise SaveEditorError(f"Unable to restore backup: {exc}") from exc
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass


def save_over_encrypted_file(document: SaveDocument) -> dict:
    """Rebuild and atomically replace the opened encrypted file after making a backup."""
    target = document.path.resolve()
    if not target.is_file():
        raise SaveEditorError("The original save file no longer exists. Reopen it.")
    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    if current_hash != document.source_hash:
        raise SaveEditorError("The file on disk has changed since it was opened. Reopen it to avoid overwriting external changes.")
    if document.baseline_root is not None and document.root.tag != document.baseline_root.tag:
        raise SaveEditorError("The root XML element cannot be changed.")

    xml_bytes = serialize_xml(document.root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staged = target.with_name(f".{target.name}.{stamp}.staged")

    with tempfile.TemporaryDirectory(prefix="crysis-save-edit-") as work_dir:
        xml_path = Path(work_dir) / "edited.xml"
        xml_path.write_bytes(xml_bytes)
        try:
            if document.kind == "csf":
                result = rebuild_save(xml_path, target, document.exe_path, staged)
            else:
                result = rebuild_encrypted_xml(xml_path, target, document.exe_path, staged)

            # Create a restorable copy only after the candidate has passed format and round-trip checks.
            backup = create_backup_file(target)
            os.replace(staged, target)

            # Final on-disk verification after replacement; roll back automatically on failure.
            try:
                reopened = open_encrypted_save(target, document.exe_path)
                if not _xml_equivalent(document.root, reopened.root, binary_xml=document.kind == "csf"):
                    raise SaveEditorError("Post-save verification does not match the edited XML.")
            except Exception:
                shutil.copy2(backup, target)
                raise
            document.root = reopened.root
            document.baseline_root = copy.deepcopy(reopened.root)
            final_bytes = target.read_bytes()
            document.file_size = len(final_bytes)
            document.node_count = _count_nodes(reopened.root)
            document.source_hash = hashlib.sha256(final_bytes).hexdigest()
            document.decompressed_size = reopened.decompressed_size
            return {**result, "backup": backup, "path": target, "file_size": document.file_size}
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def validate_import_compatibility(document: SaveDocument, incoming: ET.Element) -> None:
    """Reject a different save/profile format before replacing any in-memory XML.

    A SaveGame's outer sections and metadata identify its family and checkpoint.
    Comparing their *names* allows ordinary XML value editing, but prevents
    the known Crysis 2 -> Crysis 3 cross-game import. This is an import safety
    check, not a proof that every edited save will load in the game.
    """
    current = document.baseline_root if document.baseline_root is not None else document.root
    if incoming.tag != current.tag:
        raise SaveEditorError(f"Root element mismatch: expected <{current.tag}>, got <{incoming.tag}>.")
    if getattr(document, "kind", "xml") == "csf":
        if current.tag != "SaveGame":
            raise SaveEditorError("Unexpected CSF root; import requires a matching SaveGame.")
        expected_sections = tuple(child.tag for child in current)
        imported_sections = tuple(child.tag for child in incoming)
        if expected_sections != imported_sections:
            raise SaveEditorError("Incompatible SaveGame sections. This XML may belong to a different Crysis game.")
        for section in ("TerrainState", "GameState"):
            original = current.find(section)
            replacement = incoming.find(section)
            if original is None or replacement is None or set(original.attrib) != set(replacement.attrib):
                raise SaveEditorError(f"Incompatible SaveGame {section} schema. Import the XML exported from this save.")
        original_meta = current.find("Metadata")
        imported_meta = incoming.find("Metadata")
        if original_meta is None or imported_meta is None:
            raise SaveEditorError("SaveGame metadata is missing from the imported XML.")
        # These are identifiers for the save/engine format and checkpoint, not
        # ordinary editable gameplay statistics.
        for key in ("version", "build", "level", "gameRules", "checkPointName"):
            if original_meta.get(key) != imported_meta.get(key):
                raise SaveEditorError(f"SaveGame metadata mismatch ({key}). Import XML from the same save slot.")
    elif current.tag in ("Attributes", "Metadata"):
        # Profile versions 18/32/34 identify three incompatible game schemas;
        # all three .meta files share version 24, so check identifying fields
        # where present without enforcing mutable statistics or timestamps.
        keys = ("Version", "version") if current.tag == "Attributes" else ("version", "build", "level")
        for key in keys:
            if current.get(key) != incoming.get(key):
                raise SaveEditorError(f"Incompatible {current.tag} schema ({key}). Use the matching game's exported XML.")
        if current.tag == "Attributes":
            old_names = {el.get("name") for el in current.findall("Attr") if el.get("name")}
            new_names = {el.get("name") for el in incoming.findall("Attr") if el.get("name")}
            if old_names and new_names:
                overlap = len(old_names & new_names) / max(len(old_names), len(new_names))
                if overlap < 0.5:
                    raise SaveEditorError("Most profile attribute names differ. XML probably belongs to another profile/game.")
    else:
        # Other encrypted XML documents (e.g., actionmaps.xml) still require
        # the same immediate layout, not just the same outer element name.
        if tuple(child.tag for child in current) != tuple(child.tag for child in incoming):
            raise SaveEditorError("Imported XML has a different document structure.")


def import_xml_into_document(document: SaveDocument, xml_path: Path | str) -> ET.Element:
    xml_path = Path(xml_path)
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise SaveEditorError(f"Unable to import XML: {exc}") from exc
    validate_import_compatibility(document, root)
    return root


def _element_to_json(element: ET.Element) -> dict:
    return {
        "tag": element.tag,
        "attributes": dict(element.attrib),
        "children": [_element_to_json(child) for child in list(element)],
    }


def export_json(root: ET.Element, path: Path | str) -> None:
    payload = _element_to_json(root)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(root: ET.Element, path: Path | str) -> None:
    """Export flattened XML data (one row per attribute, empty nodes get a blank row)."""
    with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(("path", "tag", "attribute", "value"))

        def walk(node: ET.Element, path_parts: list[str]):
            sibling_counts: dict[str, int] = {}
            for child in list(node):
                sibling_counts[child.tag] = sibling_counts.get(child.tag, 0) + 1
            # Root is represented with its own tag and zero index.
            here = path_parts + [f"{node.tag}[0]"] if not path_parts else path_parts
            if node.attrib:
                for key, value in node.attrib.items():
                    writer.writerow(("/" + "/".join(here), node.tag, key, value))
            else:
                writer.writerow(("/" + "/".join(here), node.tag, "", ""))
            counts: dict[str, int] = {}
            for child in list(node):
                counts[child.tag] = counts.get(child.tag, 0) + 1
                walk(child, here + [f"{child.tag}[{counts[child.tag]-1}]"])

        walk(root, [])


def find_inventory_context(root: ET.Element) -> tuple[ET.Element | None, ET.Element | None, list[ET.Element]]:
    """Find *the player's* inventory, not an arbitrary NPC inventory.

    Some chapter saves omit CurrentItem. Select the Inventory extension that
    belongs to the same GameObject as the Player extension; only use the old
    CurrentItem heuristic when a unique candidate exists.
    """
    parents = {child: node for node in root.iter() for child in node}

    def ancestor(node: ET.Element, tag: str) -> ET.Element | None:
        while node is not None and node.tag != tag:
            node = parents.get(node)
        return node

    players = [node for node in root.iter()
               if node.tag == "Extension" and node.get("name") == "Player"]
    players.sort(key=lambda node: bool(node.get("OwnerId")), reverse=True)
    inventory = None
    player_extension = None
    player_game_object = None
    for player in players:
        obj = ancestor(player, "GameObject")
        if obj is None:
            continue
        candidates = [node for node in obj.iter() if node.tag == "InventoryItems"]
        if candidates:
            inventory = next((node for node in candidates if "CurrentItem" in node.attrib), candidates[0])
            player_extension = player
            player_game_object = obj
            break
    if player_extension is None and players:
        player_extension = players[0]
        player_game_object = ancestor(player_extension, "GameObject")

    if inventory is None:
        candidates = [node for node in root.iter()
                      if node.tag == "InventoryItems" and "CurrentItem" in node.attrib]
        if len(candidates) == 1:
            inventory = candidates[0]
        elif not candidates:
            all_inventories = [node for node in root.iter() if node.tag == "InventoryItems"]
            if len(all_inventories) == 1:
                inventory = all_inventories[0]

    owner_id = player_extension.get("OwnerId", "") if player_extension is not None else ""
    if not owner_id and player_game_object is not None:
        entity = ancestor(player_game_object, "Entity")
        if entity is not None:
            owner_id = entity.get("id", "")

    weapon_extensions = []
    if owner_id:
        weapon_extensions = [node for node in root.iter()
                             if node.tag == "Extension" and node is not player_extension
                             and (node.get("ownerId") == owner_id or node.get("OwnerId") == owner_id)
                             and node.get("name")]
    return inventory, player_extension, weapon_extensions


def parse_ltl_inventory_snapshot(root: ET.Element) -> ET.Element | None:
    """Parse the long-term inventory's embedded XML for *read-only* display.

    Its data are binary XMLCPB RAW attributes represented as raw:<hex>. Do not
    expose parsed elements for editing: changing them would not change the
    encoded original. Malformed / absent snapshots simply return None.
    """
    ltl = root.find("LTLInventory")
    if ltl is None:
        return None
    raw = ltl.get("LTLInventoryData", "")
    if not raw.startswith("raw:"):
        return None
    try:
        raw_hex = raw[4:]
        if len(raw_hex) > 4 * 1024 * 1024:  # 2 MiB uncompressed cap
            return None
        data = bytes.fromhex(raw_hex).rstrip(b"\x00")
        result = ET.fromstring(data)
        return result if result.tag == "Inventory" else None
    except (ValueError, ET.ParseError, UnicodeError):
        return None


def find_player_stats_context(root: ET.Element) -> tuple[ET.Element | None, list[ET.Element]]:
    """Locate a real player's actor and related statistics, never an NPC by order.

    `playerState=1` is not a reliable player identifier: NPC actors in some
    Crysis 2 chapter saves also have it set. Prefer an actor in the GameObject
    that actually owns an Extension with name="Player". Only use playerState
    when it uniquely identifies a candidate. Return no result on ambiguity.
    """
    actors = [node for node in root.iter() if node.tag == "CActor"]
    if not actors:
        return None, []
    parents = {child: parent for parent in root.iter() for child in parent}

    def ancestor(node: ET.Element, tag: str) -> ET.Element | None:
        while node is not None and node.tag != tag:
            node = parents.get(node)
        return node

    player_objects = {
        obj
        for extension in root.iter("Extension")
        if extension.get("name") == "Player"
        for obj in [ancestor(extension, "GameObject")]
        if obj is not None
    }
    matching = [actor for actor in actors if ancestor(actor, "GameObject") in player_objects]
    if len(matching) == 1:
        actor = matching[0]
    elif matching:
        # A save with multiple actors in multiple Player objects is ambiguous.
        # Some games store an extra CActor in the same object; do not guess.
        return None, []
    elif player_objects:
        # A Player extension exists but has no resolvable CActor. Do not edit an NPC.
        return None, []
    elif len(actors) == 1:
        actor = actors[0]
    else:
        flagged = [node for node in actors if node.get("playerState") == "1"]
        if len(flagged) != 1:
            return None, []
        actor = flagged[0]

    game_object = ancestor(actor, "GameObject")
    if game_object is None:
        return actor, [actor]
    related = [actor]
    for element in game_object.iter():
        if element is actor:
            continue
        if element.tag in {"haveNanoSuit", "PlayerStats", "PlayerMovement", "PlayerRotation", "PlayerLinkStats"}:
            related.append(element)
    return actor, related


def iter_attribute_rows(elements: list[ET.Element]):
    for element in elements:
        for name, value in element.attrib.items():
            yield element, name, value


def batch_decrypt_folder(
    input_dir: Path | str,
    output_dir: Path | str,
    exe_path: Path | str,
    recursive: bool = True,
    progress=None,
) -> dict:
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    exe_path = Path(exe_path).resolve()
    if not input_dir.is_dir():
        raise SaveEditorError(f"Input folder does not exist: {input_dir}")
    if input_dir == output_dir:
        raise SaveEditorError("Input and output folders must be different. Choose a separate XML export folder.")
    magic, key = extract_cipher(exe_path)
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    supported = [p for p in iterator if p.is_file() and p.suffix.lower() in (".csf", ".meta", ".xml")]
    # Never process files that are already in the destination tree.
    supported = [p for p in supported if output_dir != p and output_dir not in p.parents]
    results = {"ok": 0, "failed": 0, "skipped": 0, "files": []}
    total = len(supported)
    for index, source in enumerate(supported, 1):
        relative = source.relative_to(input_dir)
        destination_base = output_dir / relative
        if source.suffix.lower() == ".csf":
            out = destination_base.with_name(destination_base.stem + ".xml")
        else:
            out = destination_base.with_name(destination_base.name + ".decrypted.xml")
        if out.exists():
            results["skipped"] += 1
            results["files"].append((str(source), str(out), "SKIPPED: output already exists (not overwritten)"))
        else:
            try:
                decrypted = decrypt_file_bytes(source.read_bytes(), key, source.name, magic)
                if source.suffix.lower() == ".csf":
                    # Parse CSF entirely in RAM; no intermediate decrypted files.
                    root, _, _ = parse_csf_decrypted_bytes(decrypted)
                else:
                    root = ET.fromstring(decrypted)
                payload = serialize_xml(root)
                out.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation also protects against a race between the
                # initial exists() check and the actual disk write.
                created = False
                try:
                    with out.open("xb") as stream:
                        created = True
                        stream.write(payload)
                except Exception:
                    if created:
                        out.unlink(missing_ok=True)  # remove only our partial export
                    raise
                results["ok"] += 1
                results["files"].append((str(source), str(out), "OK"))
            except FileExistsError:
                results["skipped"] += 1
                results["files"].append((str(source), str(out), "SKIPPED: output already exists (not overwritten)"))
            except Exception as exc:
                results["failed"] += 1
                results["files"].append((str(source), "", str(exc)))
        if progress:
            progress(index, total, source, results["files"][-1][2])
    return results
