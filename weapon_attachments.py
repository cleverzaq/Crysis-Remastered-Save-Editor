"""Conservative Crysis 3 Remastered weapon attachment checklist and diagnostics.

Attachment unlock inference from XML, validated by comparison of the user's
19/20 exports with a separate reportedly 100% Crysis 3 Remastered profile.
The 20 achievement attachment identifiers are present in the 100% reference.
Each known identifier's presence/value is evidence, not proof of the game's
internal counter algorithm. User checklist lives OUTSIDE the game save.
Inspection never edits game data. Explicit unlock/lock commands edit the loaded XML in memory; saving is handled by the main editor.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Optional
import xml.etree.ElementTree as ET

STAT_PREFIX = 'SP/PersistantStats/EMPS_AttachmentUnlocked'
INITIALIZER = STAT_PREFIX + '/init'
VALID_STATES = ('unknown', 'acquired', 'missing')
GUIDE_URL = 'https://www.gry-online.pl/poradniki/crysis-3/dodatki-do-broni/zbeaad'


@dataclass(frozen=True)
class Attachment:
    number: int
    name: str
    weapon: str
    mission: str
    location: str
    profile_key: Optional[str] = None


ATTACHMENTS = (
    Attachment(1, 'Iron Sights', 'Hammer II', 'Post-Human',
               'Granted automatically near the beginning of the campaign.', 'ironsight'),
    Attachment(2, 'Silencer', 'Hammer II', 'Post-Human',
               'Granted automatically with your starting pistol.', 'silencer'),
    Attachment(3, 'Rifle Laser Sight', 'Hammer II', 'Post-Human',
               'Granted automatically at the start.', 'lasersight'),
    Attachment(4, 'Reflex Sight', 'SCAR Mod 2', 'Post-Human',
               'Pick up the Reflex SCAR on the bridge near the helicopter reinforcement encounter.', 'reflex'),
    Attachment(5, 'Single Shot', 'SCAR Mod 2', 'Post-Human',
               'The same Reflex SCAR on the bridge can unlock Single Shot.', 'single'),
    Attachment(6, 'Assault Scope', 'SCAR Mod 2', 'Post-Human',
               'Search the CELL guard near the two large drilling generators.', 'assaultscope'),
    Attachment(7, 'Foregrip', 'SCARAB Mod 2', 'Welcome to the Jungle',
               'Check SCARAB rifles carried by the patrol in the flooded area.', 'foregrip'),
    Attachment(8, 'Extended Ammo Box', 'SCARAB Mod 2', 'Welcome to the Jungle',
               'Check the flooded-area patrol, near the damaged turret.', 'extendedclipscar'),
    Attachment(9, 'Double Magazines (Feline)', 'Feline X3', 'Welcome to the Jungle',
               'Search the room with the broken rail tracks on the way to Psycho.', 'doubleclipfeline'),
    Attachment(10, 'Extended Magazine (Feline)', 'Feline X3', 'Welcome to the Jungle',
               'Look for a Feline in the broken-rail area; confirmed XML key extendedclipfeline.', 'extendedclipfeline'),
    Attachment(11, 'Match Barrel', 'Hammer II', 'Welcome to the Jungle',
               'Check weapons in the zipline room.', 'matchbarrel'),
    Attachment(12, 'Sniper Scope', 'DSG-1', 'The Root of All Evil',
               'Hack the orange weapon crate after escaping the blocked elevator.', 'sniperscope'),
    Attachment(13, 'Muzzle Brake', 'SCAR Mod 2', 'The Root of All Evil',
               'Search CELL enemies on the dam.', 'muzzlebreak'),
    Attachment(14, 'Tech Scope', 'DSG-1', 'The Root of All Evil',
               'Near the elevator down to System X, check the floor beside the hacking crate.', 'techscope'),
    Attachment(15, 'Mini-Typhoon', 'SCARAB Mod 2', 'Safeties Off',
               'Search the three CELL soldiers in the building on the right near mission start.', 'typhoonattachment'),
    Attachment(16, 'Bayonet', 'Marshall', 'Safeties Off',
               'Rare Marshall shotgun on a CELL soldier near the first large encounter.', 'bayonet'),
    Attachment(17, 'Double Magazines (Jackal)', 'Alpha Jackal', 'Red Star Rising',
               'An Alpha Jackal on crates by the zipline after two makeshift bridges.', 'doubleclipjackal'),
    Attachment(18, 'Grenade Launcher', 'SCAR Mod 2', 'Red Star Rising',
               'Search CELL soldiers fighting Stalkers after the Mindcarrier.', 'grenadelauncher'),
    Attachment(19, 'Extended Ammo Drum 12g', 'Alpha Jackal', 'Red Star Rising',
               'Search Alpha Jackal weapons in the CELL command-center/elevator area.', 'extendedclipjackal'),
    Attachment(20, 'Flashlight', 'M12 Nova', 'Gods and Monsters',
               'Granted automatically at the beginning of the final mission.', 'flashlight'),
)

assert len(ATTACHMENTS) == 20
assert [x.number for x in ATTACHMENTS] == list(range(1, 21))


def _attrs(root: ET.Element) -> dict[str, str]:
    if root.tag != 'Attributes':
        raise ValueError('Not a CryEngine <Attributes> profile.')
    return {tag.get('name', ''): tag.get('value', '') for tag in root.iter('Attr')}


def _split_pairs(data: str) -> dict[str, str]:
    """Decode comma-delimited key/value pairs without treating them as unlocks."""
    items = data.rstrip(',').split(',') if data else []
    if len(items) % 2:
        return {}
    return {k.casefold(): v for k, v in zip(items[::2], items[1::2]) if k}


def inspect_profile(root: Optional[ET.Element]) -> dict:
    """Return raw diagnostics only; no unlock assumptions from /init."""
    if root is None:
        return {'initializer_count': 0, 'initializer_flags': {}, 'explicit_flags': {},
                'raw_flags': {}, 'auto_states': {}, 'available': False}
    attributes = _attrs(root)
    initializer = _split_pairs(attributes.get(INITIALIZER, ''))
    # Preserve direct flags, if the game/profile happens to provide them.
    direct = {}
    for key, value in attributes.items():
        if key.casefold().startswith((STAT_PREFIX + '/').casefold()) and key.casefold() != INITIALIZER.casefold():
            direct[key[len(STAT_PREFIX) + 1:].casefold()] = value
    # Some CryEngine revisions use the aggregate map rather than children.
    aggregate = _split_pairs(attributes.get(STAT_PREFIX, ''))
    direct.update(aggregate)  # aggregate is explicit non-/init storage
    raw = {row.number: direct[row.profile_key.casefold()]
           for row in ATTACHMENTS if row.profile_key and row.profile_key.casefold() in direct}
    # These 20 keys were cross-checked between the 19/20 and 100% profiles.
    # Do not treat *_tutorial or unrelated initializer entries as extra attachments.
    # When an existing initialized profile has no key, mark the attachment missing
    # relative to the 100% reference. For a profile without an initializer,
    # statuses remain unknown rather than labeling everything missing.
    auto = {}
    if INITIALIZER in attributes:
        for item in ATTACHMENTS:
            key = (item.profile_key or '').casefold()
            value = direct.get(key, initializer.get(key))
            if value == '1':
                auto[item.number] = 'acquired'
            elif value is None or value == '0':
                auto[item.number] = 'missing'
            else:
                auto[item.number] = 'unknown'
    return {'initializer_count': len(initializer), 'initializer_flags': initializer,
            'explicit_flags': direct, 'raw_flags': raw,
            'auto_states': auto, 'available': True}



def _looks_like_crysis3_profile(root: ET.Element) -> bool:
    """Conservative format guard before creating a missing game-owned attribute.

    A brand-new XML file with only an <Attributes> element is not sufficient:
    it might be a different CryEngine game or an unrelated document.
    """
    names = set(_attrs(root))
    return (any(name.startswith('SP/PersistantStats/') for name in names)
            and any(name.startswith(prefix) for name in names for prefix in (
                'NanosuitUpgrades/', 'PerkDistribution/',
                'SP/PersistantStats/EMPS_SPLevelByDifficulty/',
                'SP/PersistantStats/EMPS_SPWeaponByName/')))


def _initializer_for_edit(root: ET.Element) -> tuple[Optional[ET.Element], list[tuple[str, str]]]:
    """Read existing unlock entries, allowing an empty or missing initializer.

    Creation is deferred to the edit function, only for a recognized Crysis 3
    profile and only when the user actually unlocks an attachment.
    """
    _attrs(root)
    nodes = [n for n in root.iter('Attr') if n.get('name') == INITIALIZER]
    if len(nodes) > 1:
        raise ValueError('Duplicate attachment initializer; refusing an ambiguous edit.')
    if not nodes:
        if not _looks_like_crysis3_profile(root):
            raise ValueError('No attachment initializer or recognizable Crysis 3 profile markers.')
        # If another representation exists, do not invent a competing map.
        if any(n.get('name', '').casefold().startswith(STAT_PREFIX.casefold() + '/')
               or n.get('name', '').casefold() == STAT_PREFIX.casefold()
               for n in root.iter('Attr')):
            raise ValueError('Different attachment unlock storage detected; no /init entry to edit safely.')
        return None, []
    node = nodes[0]
    data = node.get('value', '')
    if not data:
        return node, []
    raw_items = data.split(',')
    if raw_items[-1] == '':
        raw_items.pop()
    if len(raw_items) % 2:
        raise ValueError('Malformed attachment initializer (odd number of name/value entries).')
    pairs = list(zip(raw_items[::2], raw_items[1::2]))
    if any(not key or not val for key, val in pairs):
        raise ValueError('Malformed attachment initializer (empty name or value).')
    lowered = [key.casefold() for key, _ in pairs]
    if len(set(lowered)) != len(lowered):
        raise ValueError('Attachment initializer has duplicate keys; refusing an ambiguous edit.')
    return node, pairs


def _insert_initializer(root: ET.Element, value: str) -> ET.Element:
    """Create the missing /init beside other alphabetical Attr profile entries."""
    new = ET.Element('Attr', {'name': INITIALIZER, 'value': value})
    # Export profiles have a flat sorted Attributes tree, insert into that order
    # without disturbing the values or order of existing nodes.
    children = list(root)
    for i, child in enumerate(children):
        if child.tag == 'Attr' and child.get('name', '') > INITIALIZER:
            root.insert(i, new)
            return new
    root.append(new)
    return new


def _set_attachment_entries(root: ET.Element, numbers: list[int], *, unlocked: bool) -> list[str]:
    node, pairs = _initializer_for_edit(root)
    targets = {}
    for number in numbers:
        if type(number) is not int or not 1 <= number <= len(ATTACHMENTS):
            raise ValueError('Attachment number must be between 1 and 20.')
        key = ATTACHMENTS[number - 1].profile_key
        if not key:
            raise ValueError('Unknown attachment identifier.')
        targets[key.casefold()] = key

    # The reference profiles use /init, but a profile may also expose explicit
    # per-attachment flags. Avoid writing conflicting states in that format.
    attrs = _attrs(root)
    explicit = _split_pairs(attrs.get(STAT_PREFIX, ''))
    for target in targets:
        per_key = STAT_PREFIX + '/' + target
        if per_key in attrs or target in explicit:
            raise ValueError(f'{target}: explicit unlock flags also exist. Editing is disabled for this layout.')

    index = {key.casefold(): i for i, (key, _) in enumerate(pairs)}
    if unlocked:
        for key in targets:
            if key in index:
                i = index[key]
                pairs[i] = (pairs[i][0], '1')
            else:
                # Matches the alphabetically ordered initializer from the 100% profile.
                i = next((i for i, (name, _) in enumerate(pairs)
                          if name.casefold() > key), len(pairs))
                pairs.insert(i, (targets[key], '1'))
                index = {name.casefold(): j for j, (name, _) in enumerate(pairs)}
    else:
        pairs = [(key, val) for key, val in pairs if key.casefold() not in targets]

    new_value = ''.join(f'{key},{val},' for key, val in pairs)
    if node is None:
        if not new_value:  # Lock in a fresh profile is a no-op.
            return []
        _insert_initializer(root, new_value)
        return [INITIALIZER]
    if new_value == node.get('value', ''):
        return []
    node.set('value', new_value)
    return [INITIALIZER]


def set_attachment_unlocked(root: ET.Element, number: int, unlocked: bool = True) -> list[str]:
    """Update one of the 20 verified /init keys in memory, never the disk."""
    if type(unlocked) is not bool:
        raise ValueError('Unlocked must be a boolean.')
    return _set_attachment_entries(root, [number], unlocked=unlocked)


def unlock_all_attachments(root: ET.Element) -> list[str]:
    """Set all 20 keys to 1 without touching other attachment entries."""
    return _set_attachment_entries(root, list(range(1, 21)), unlocked=True)

def default_checklist_path() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return base / 'CrysisSaveEditor' / 'weapon_attachments.json'


def profile_identifier(path: Optional[str | Path]) -> str:
    if path is None:
        return 'unassigned'
    normalized = str(Path(path).expanduser().resolve(strict=False)).casefold()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]


class ChecklistStore:
    """Manually tracked data only. Versioned, best-effort atomic persistence."""
    def __init__(self, path: Optional[Path] = None):
        self.path = path or default_checklist_path()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {'version': 1, 'profiles': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            raise ValueError('Checklist file could not be read. Your game save was not changed.')
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
            raise ValueError('Unsupported checklist format. Game saves were not touched.')
        return data

    def load(self, path: Optional[str | Path]) -> dict:
        data = self._read().get('profiles', {}).get(profile_identifier(path), {})
        if not isinstance(data, dict):
            return {'states': {}, 'game_count': None}
        raw = data.get('states', {})
        states = {int(k): v for k, v in raw.items()
                  if str(k).isdigit() and 1 <= int(k) <= 20 and v in VALID_STATES} if isinstance(raw, dict) else {}
        game_count = data.get('game_count')
        if type(game_count) is not int or not 0 <= game_count <= 20:
            game_count = None
        return {'states': states, 'game_count': game_count}

    def save(self, path: Optional[str | Path], states: dict[int, str], game_count: Optional[int]):
        if game_count is not None and (type(game_count) is not int or not 0 <= game_count <= 20):
            raise ValueError('In-game count must be between 0 and 20.')
        clean_states = {}
        for number, state in states.items():
            if type(number) is not int or not 1 <= number <= 20 or state not in VALID_STATES:
                raise ValueError('Invalid checklist entry.')
            if state != 'unknown':
                clean_states[str(number)] = state
        data = self._read()
        data['profiles'][profile_identifier(path)] = {'states': clean_states, 'game_count': game_count}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix='.weapon_attachments.', suffix='.tmp', dir=str(self.path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(data, out, indent=2, ensure_ascii=False)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            os.replace(filename, self.path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)


def summary(states: dict[int, str]) -> dict[str, int]:
    acquired = sum(states.get(i, 'unknown') == 'acquired' for i in range(1, 21))
    missing = sum(states.get(i, 'unknown') == 'missing' for i in range(1, 21))
    return {'acquired': acquired, 'missing': missing, 'unknown': 20 - acquired - missing}


def export_checklist_csv(path: Path, states: dict[int, str], diagnostics: dict):
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(('No', 'Attachment', 'Weapon', 'Mission', 'Auto Profile Status', 'Manual Status', 'XML Key', 'Raw Explicit Flag', 'Location'))
        for item in ATTACHMENTS:
            writer.writerow((item.number, item.name, item.weapon, item.mission,
                             diagnostics.get('auto_states', {}).get(item.number, 'unknown'),
                             states.get(item.number, 'unknown'), item.profile_key or '',
                             diagnostics['raw_flags'].get(item.number, ''), item.location))


def export_detected_csv(path: Path | str, diagnosis: dict) -> None:
    """Export the real XML inference only, without any manual checklist data."""
    with Path(path).open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.writer(fp)
        writer.writerow(('Number', 'Attachment', 'Weapon', 'Mission', 'XML key', 'Detected in XML'))
        for item in ATTACHMENTS:
            status = diagnosis.get('auto_states', {}).get(item.number, 'unknown')
            writer.writerow((item.number, item.name, item.weapon, item.mission,
                             item.profile_key or '', status))
