"""Read and edit Crysis 3 Remastered campaign attributes without adding unknown fields.

All edits here are in-memory ElementTree edits. The GUI's existing encrypted save
pipeline is responsible for encrypted writes, verification, and automatic backups.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import xml.etree.ElementTree as ET

# Campaign order is NOT the alphabetical order of EMPS_SPLevelByDifficulty/init.
# River and River2 are both associated with mission 5.
CRYSIS3_MISSIONS = (
    ("Jailbreak", "Post-Human"),
    ("Fields", "Welcome to the Jungle"),
    ("Canyon", "The Root of All Evil"),
    ("Swamp", "Safeties Off"),
    ("River2", "Red Star Rising"),
    ("Islands", "Only Human"),
    ("Cave", "Gods and Monsters"),
)
LEVEL_IDS = tuple(level for level, _ in CRYSIS3_MISSIONS)
_MISSION_BY_LEVEL = {level.casefold(): (index, name) for index, (level, name) in enumerate(CRYSIS3_MISSIONS)}
_MISSION_BY_LEVEL["river"] = _MISSION_BY_LEVEL["river2"]
_DIFFICULTY_FIELDS = (
    "Singleplayer.LastSelectedDifficulty",
    "SP/Difficulty",
    "Option.g_difficultyLevel",
    "Difficulty",
)
_COMPLETED_FIELD = "SP/PersistantStats/EMPS_SPLevelByDifficulty/init"
_SAVE_FIELD = "Singleplayer.LastSavedGame"
_NEXT_ID_FIELD = "Singleplayer.SaveRunningID"
_SAVE_NAME = re.compile(r"^(\d+)_([A-Za-z][A-Za-z0-9]*)_([A-Za-z0-9_-]+)\.CSF$", re.IGNORECASE)


@dataclass(frozen=True)
class CampaignSnapshot:
    last_completed_mission: str
    current_mission: str
    current_level_id: str
    last_save_id: int | None
    next_save_id: int | None
    last_save_file: str
    difficulty: str
    completed_levels: tuple[str, ...]


def _nodes(root: ET.Element) -> dict[str, ET.Element]:
    result = {}
    for node in root.iter("Attr"):
        name = node.get("name")
        if name is not None:
            if name in result:
                raise ValueError(f"Profile contains duplicate attribute: {name}")
            result[name] = node
    return result


def _value(nodes: dict[str, ET.Element], name: str) -> str:
    node = nodes.get(name)
    return node.get("value", "") if node is not None else ""


def split_save_filename(filename: str) -> tuple[int, str, str] | None:
    """Return (save number, level id, checkpoint), or None for unknown formats."""
    if not isinstance(filename, str) or len(filename) > 255 or filename != Path(filename).name:
        return None
    match = _SAVE_NAME.fullmatch(filename)
    if match is None:
        return None
    number, level, checkpoint = match.groups()
    if not checkpoint or checkpoint in (".", "..") or ".." in checkpoint:
        return None
    return int(number), level, checkpoint


def format_save_filename(save_id: int, level_id: str, checkpoint: str) -> str:
    if not isinstance(save_id, int) or save_id < 0 or save_id > 2147483647:
        raise ValueError("Last Save ID must be an integer between 0 and 2147483647.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", level_id):
        raise ValueError("Current Level ID must contain only letters and numbers, starting with a letter.")
    if not checkpoint or "/" in checkpoint or "\\" in checkpoint or ".." in checkpoint:
        raise ValueError("Invalid checkpoint in save filename.")
    result = f"{save_id}_{level_id}_{checkpoint}.CSF"
    if len(result) > 255:
        raise ValueError("The save filename is too long.")
    return result


def _unsigned_integer(value: str, label: str) -> int:
    if not re.fullmatch(r"\d{1,10}", value.strip()):
        raise ValueError(f"{label} must be a non-negative integer.")
    result = int(value.strip())
    if result > 2147483647:
        raise ValueError(f"{label} exceeds the maximum supported value (2147483647).")
    return result


def next_save_id_for(last_save_id: str | int) -> int:
    """Return the next save counter for an edited LastSavedGame ID."""
    save_id = _unsigned_integer(str(last_save_id), "Last Save ID")
    if save_id >= 2147483647:
        raise ValueError("Last Save ID is too large to calculate SaveRunningID (ID + 1).")
    return save_id + 1


def _completed_levels(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    parts = text.strip(",").split(",")
    if len(parts) % 2:
        raise ValueError("The completed-levels attribute does not contain level/value pairs.")
    completed = set()
    for index in range(0, len(parts), 2):
        name, score = parts[index].strip(), parts[index + 1].strip()
        if not re.fullmatch(r"\d+", score):
            raise ValueError("Completed-level entries must contain non-negative integer values.")
        if int(score) > 0:
            completed.add(name.casefold())
    # The original ordering in the XML is alphabetical; sort it by campaign order.
    return tuple(sorted(completed, key=lambda key: (_MISSION_BY_LEVEL.get(key, (1000, ""))[0], key)))


def read_campaign_progress(root: ET.Element) -> CampaignSnapshot:
    nodes = _nodes(root)
    filename = _value(nodes, _SAVE_FIELD)
    parsed = split_save_filename(filename)
    save_id, level = (parsed[0], parsed[1]) if parsed else (None, "")
    next_raw = _value(nodes, _NEXT_ID_FIELD)
    next_id = int(next_raw) if re.fullmatch(r"\d+", next_raw) else None
    difficulty = next((_value(nodes, name) for name in _DIFFICULTY_FIELDS if _value(nodes, name)), "")
    recorded = _completed_levels(_value(nodes, _COMPLETED_FIELD))
    known_completed = [(_MISSION_BY_LEVEL[key][0], _MISSION_BY_LEVEL[key][1]) for key in recorded if key in _MISSION_BY_LEVEL]
    current = _MISSION_BY_LEVEL.get(level.casefold(), (None, "Unknown"))[1] if level else "Unknown"
    return CampaignSnapshot(
        last_completed_mission=max(known_completed)[1] if known_completed else "Unknown",
        current_mission=current,
        current_level_id=level,
        last_save_id=save_id,
        next_save_id=next_id,
        last_save_file=filename,
        difficulty=difficulty,
        completed_levels=recorded,
    )


def apply_campaign_values(
    root: ET.Element, *, last_save_file: str, last_save_id: str | int,
    current_level_id: str, difficulty: str | int, next_save_id: str | int | None = None,
) -> list[str]:
    """Update existing attributes after *all* inputs have passed validation."""
    nodes = _nodes(root)
    for name in (_SAVE_FIELD, _NEXT_ID_FIELD):
        if name not in nodes or nodes[name].get("value") is None:
            raise ValueError(f"The target profile is missing required field: {name}")
    parsed = split_save_filename(last_save_file.strip())
    if parsed is None:
        raise ValueError("Last Save File must have the form 81_Cave_1288.CSF.")
    save_id = _unsigned_integer(str(last_save_id), "Last Save ID")
    next_id = (_unsigned_integer(str(next_save_id), "Next Save ID")
               if next_save_id is not None else next_save_id_for(save_id))
    level_id = current_level_id.strip()
    if parsed[0] != save_id or parsed[1].casefold() != level_id.casefold():
        raise ValueError("Last Save ID and Current Level ID must match the Last Save File name.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", level_id):
        raise ValueError("Invalid Current Level ID.")
    difficulty_num = _unsigned_integer(str(difficulty), "Difficulty")
    if not 1 <= difficulty_num <= 5:
        raise ValueError("Difficulty must be between 1 and 5.")
    changes = {
        _SAVE_FIELD: last_save_file.strip(),
        _NEXT_ID_FIELD: str(next_id),
    }
    for name in _DIFFICULTY_FIELDS:
        if name in nodes:
            changes[name] = str(difficulty_num)
    if not any(name in nodes for name in _DIFFICULTY_FIELDS):
        raise ValueError("This profile does not contain a difficulty attribute.")
    changed = []
    for name, value in changes.items():
        if nodes[name].get("value") != value:
            nodes[name].set("value", value)
            changed.append(name)
    return changed


def restore_campaign_values(target: ET.Element, source: ET.Element) -> list[str]:
    """Restore save refs, next ID, difficulty and completed levels from a plain XML export.

    Preserve all fields absent from the source, and never add fields to the target.
    Nothing is written to disk here; GUI save verifies encrypted round trips.
    """
    if source.tag != "Attributes" or target.tag != "Attributes":
        raise ValueError("Both profiles must have an <Attributes> root.")
    source_nodes = _nodes(source)
    target_nodes = _nodes(target)
    required = (_SAVE_FIELD, _NEXT_ID_FIELD)
    if any(key not in source_nodes for key in required):
        raise ValueError("The source XML is missing LastSavedGame or SaveRunningID.")
    source_snapshot = read_campaign_progress(source)
    if source_snapshot.last_save_id is None or source_snapshot.next_save_id is None:
        raise ValueError("Source XML contains an invalid save filename or save ID.")
    if not source_snapshot.difficulty:
        raise ValueError("Source XML does not contain a difficulty value.")
    if _COMPLETED_FIELD in source_nodes and _COMPLETED_FIELD in target_nodes:
        _completed_levels(_value(source_nodes, _COMPLETED_FIELD))
    # Validate against a detached clone, so unexpected errors cannot partially edit the profile.
    import copy
    staged = copy.deepcopy(target)
    changed = apply_campaign_values(
        staged,
        last_save_file=source_snapshot.last_save_file,
        last_save_id=source_snapshot.last_save_id,
        current_level_id=source_snapshot.current_level_id,
        next_save_id=source_snapshot.next_save_id,
        difficulty=source_snapshot.difficulty,
    )
    stage_nodes = _nodes(staged)
    if _COMPLETED_FIELD in source_nodes and _COMPLETED_FIELD in target_nodes:
        incoming = _value(source_nodes, _COMPLETED_FIELD)
        if _value(stage_nodes, _COMPLETED_FIELD) != incoming:
            stage_nodes[_COMPLETED_FIELD].set("value", incoming)
            changed.append(_COMPLETED_FIELD)
    # Replace only changed <Attr value> strings, leaving the full tree structure untouched.
    for name in changed:
        target_nodes[name].set("value", stage_nodes[name].get("value", ""))
    return changed


def load_exported_profile(path: str | Path) -> ET.Element:
    """A user-selected decrypted XML export, never the binary/encrypted profile."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"XML file not found: {path}")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("XML export exceeds the 32 MiB safety limit.")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError("This is not a plain XML export. Export/decrypt the profile first.") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read the selected XML file: {exc}") from exc
    if root.tag != "Attributes":
        raise ValueError("The selected file is not an <Attributes> profile export.")
    return root
