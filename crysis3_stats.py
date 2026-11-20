"""Profile-backed Crysis 3 campaign statistics and nanosuit controls.

Only edits *existing* attributes whose meaning is supported by the XML.
Does not guess collectible totals, overall completion percentage, or the
undocumented PerkLoadouts encoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET

from campaign_progress import CRYSIS3_MISSIONS

COMPLETED_FIELD = 'SP/PersistantStats/EMPS_SPLevelByDifficulty/init'
TIME_FIELD = 'SP/PersistantStats/EIPS_TimePlayed'
UPGRADE_POINTS_FIELD = 'SP/PersistantStats/EIPS_NanosuitUpgradePoints'
POINTS_ACCUMULATED_FIELD = 'SP/PersistantStats/EIPS_NanosuitUpgradePointsAccumulated'
PURCHASED_COUNT_FIELD = 'PerksPurchased'
LOADOUT_FIELD = 'PerkLoadouts'
DIFFICULTIES = (
    ('Recruit', 1), ('Soldier', 2), ('Veteran', 3),
    ('Supersoldier', 4), ('Post-Human Warrior', 5),
)


def profile_nodes(root: ET.Element) -> dict[str, ET.Element]:
    if root.tag != 'Attributes':
        raise ValueError('Expected an <Attributes> game profile.')
    nodes = {}
    for child in root.iter('Attr'):
        key = child.get('name')
        if key is not None:
            if key in nodes:
                raise ValueError(f'Duplicate profile attribute: {key}')
            nodes[key] = child
    return nodes


def _read_nonnegative(nodes, key, maximum=2147483647):
    if key not in nodes:
        return None
    text = nodes[key].get('value', '')
    if re.fullmatch(r'\d+', text) is None or int(text) > maximum:
        raise ValueError(f'Invalid numeric value in {key}.')
    return int(text)


def _int(value, label, maximum=2147483647):
    text = str(value).strip()
    if not re.fullmatch(r'\d{1,10}', text) or int(text) > maximum:
        raise ValueError(f'{label} must be a whole number from 0 to {maximum}.')
    return int(text)


def _existing(nodes, key):
    if key not in nodes or nodes[key].get('value') is None:
        raise ValueError(f'This profile is missing field: {key}.')
    return nodes[key]


def _edit_nodes(root, changes: dict[str, str]):
    """Validate all target field names BEFORE touching anything."""
    nodes = profile_nodes(root)
    for key in changes:
        _existing(nodes, key)
    changed = []
    for key, value in changes.items():
        if nodes[key].get('value') != value:
            nodes[key].set('value', value)
            changed.append(key)
    return changed


def parse_completed_missions(value: str) -> list[tuple[str, int]]:
    """Ordered name/highest-difficulty pairs; preserve unknown level names."""
    if not value:
        return []
    parts = value.removesuffix(',').split(',')
    if len(parts) % 2:
        raise ValueError('Mission progress has an uneven number of level/value pairs.')
    entries = []
    seen = set()
    for name, number in zip(parts[::2], parts[1::2]):
        key = name.strip().casefold()
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key):
            raise ValueError(f'Invalid mission key: {name!r}.')
        if key in seen:
            raise ValueError(f'Duplicate mission key: {key}.')
        difficulty = _int(number, f'Difficulty for {name}', 5)
        seen.add(key)
        entries.append((key, difficulty))
    return entries


def read_campaign_statistics(root: ET.Element) -> dict:
    nodes = profile_nodes(root)
    mission_values = dict(parse_completed_missions(_existing(nodes, COMPLETED_FIELD).get('value')))
    # Some profiles use the River alias for the fifth mission, others River2.
    if 'river2' not in mission_values and 'river' in mission_values:
        mission_values['river2'] = mission_values['river']
    mission_rows = [(level, label, mission_values.get(level.casefold(), 0)) for level, label in CRYSIS3_MISSIONS]
    total = len(mission_rows)
    progress = [(name, threshold, sum(score >= threshold for _key, _label, score in mission_rows), total)
                for name, threshold in DIFFICULTIES]
    raw_time = _read_nonnegative(nodes, TIME_FIELD)
    return {
        'missions': mission_rows,
        'progress': progress,
        'time_seconds': raw_time,
        'time_display': (format_playtime(raw_time) if raw_time is not None else 'N/A'),
        'purchased': _read_nonnegative(nodes, PURCHASED_COUNT_FIELD),
        'points_available': _read_nonnegative(nodes, UPGRADE_POINTS_FIELD),
        'points_accumulated': _read_nonnegative(nodes, POINTS_ACCUMULATED_FIELD),
    }


def format_playtime(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f'{hours}:{mins:02d}:{secs:02d}'


def parse_playtime(text: str) -> int:
    text = str(text).strip()
    if re.fullmatch(r'\d{1,6}:\d{2}:\d{2}', text):
        hour, minute, second = (int(p) for p in text.split(':'))
        if minute >= 60 or second >= 60:
            raise ValueError('Time requires minutes and seconds from 00 to 59.')
        return _int(hour * 3600 + minute * 60 + second, 'Play time')
    return _int(text, 'Play time')


def set_playtime(root: ET.Element, time_text: str) -> list[str]:
    return _edit_nodes(root, {TIME_FIELD: str(parse_playtime(time_text))})


def set_mission_difficulty(root: ET.Element, mission: str, difficulty: str | int) -> list[str]:
    if mission not in {level for level, _name in CRYSIS3_MISSIONS}:
        raise ValueError('Select one of the seven known campaign missions.')
    value = _int(difficulty, 'Highest completed difficulty', 5)
    nodes = profile_nodes(root)
    previous = parse_completed_missions(_existing(nodes, COMPLETED_FIELD).get('value'))
    result = []
    found = False
    for key, score in previous:
        if key == mission.casefold() or (mission == 'River2' and key == 'river'):
            found = True
            if value:
                result.append((key, value))
        else:
            result.append((key, score))
    if not found and value:
        result.append((mission.casefold(), value))
    # Preserve pair order; game data uses comma-separated pairs with a trailing comma.
    new_value = ''.join(f'{key},{score},' for key, score in result)
    return _edit_nodes(root, {COMPLETED_FIELD: new_value})


def read_distributions(root: ET.Element) -> list[dict]:
    nodes = profile_nodes(root)
    module_ids = sorted({name.split('/')[1] for name in nodes
                         if name.startswith('PerkDistribution/') and name.endswith('/Purchased')})
    rows = []
    for ident in module_ids:
        prefix = f'PerkDistribution/{ident}/'
        for field in ('Purchased', 'Unlocked', 'MaximumTier'):
            _existing(nodes, prefix + field)
        rows.append({
            'id': ident,
            'purchased': _read_nonnegative(nodes, prefix+'Purchased', 1),
            'unlocked': _read_nonnegative(nodes, prefix+'Unlocked', 1),
            'max_tier': _read_nonnegative(nodes, prefix+'MaximumTier', 1),
        })
    return rows


def set_distribution(root: ET.Element, module: str, purchased: str | int, unlocked: str | int,
                     max_tier: str | int) -> list[str]:
    ids = {row['id'] for row in read_distributions(root)}
    if module not in ids:
        raise ValueError('Select a listed nanosuit module.')
    a = _int(purchased, 'Purchased', 1)
    b = _int(unlocked, 'Unlocked', 1)
    c = _int(max_tier, 'Maximum Tier', 1)
    if a and not b:
        raise ValueError('A purchased module must also be unlocked.')
    prefix = f'PerkDistribution/{module}/'
    # Keep the total purchased count coherent with the module flags.
    nodes = profile_nodes(root)
    current = _read_nonnegative(nodes, prefix+'Purchased', 1)
    changes = {prefix+'Purchased': str(a), prefix+'Unlocked': str(b), prefix+'MaximumTier': str(c)}
    if PURCHASED_COUNT_FIELD in nodes:
        old_total = sum(row['purchased'] for row in read_distributions(root))
        changes[PURCHASED_COUNT_FIELD] = str(old_total - current + a)
    return _edit_nodes(root, changes)


def read_perks(root: ET.Element) -> list[dict]:
    nodes = profile_nodes(root)
    names = sorted({name[len('PerkTier/'):] for name in nodes if name.startswith('PerkTier/')})
    result = []
    for key in names:
        result.append({
            'id': key,
            'tier': _read_nonnegative(nodes, 'PerkTier/'+key, 4),
            'active': _read_nonnegative(nodes, 'PerkActive/'+key, 1),
        })
    return result


def set_perk(root: ET.Element, perk: str, tier: str | int, active: str | int) -> list[str]:
    ids = {row['id'] for row in read_perks(root)}
    if perk not in ids:
        raise ValueError('Select an existing perk.')
    tier_num = _int(tier, 'Perk tier', 4)
    active_num = _int(active, 'Active', 1)
    if tier_num == 0 and active_num:
        raise ValueError('A perk with tier 0 cannot be active.')
    changes = {'PerkTier/'+perk: str(tier_num)}
    nodes = profile_nodes(root)
    if 'PerkActive/'+perk in nodes:
        changes['PerkActive/'+perk] = str(active_num)
    elif active_num:
        raise ValueError('This perk does not have an active flag.')
    return _edit_nodes(root, changes)


def read_upgrade_kits(root: ET.Element) -> list[tuple[str, int]]:
    nodes = profile_nodes(root)
    result = []
    for name in sorted(nodes):
        if name.startswith('NanosuitUpgrades/'):
            result.append((name, _read_nonnegative(nodes, name, 1)))
    return result


def set_upgrade_kit(root: ET.Element, key: str, collected: str | int) -> list[str]:
    if key not in {name for name, _value in read_upgrade_kits(root)}:
        raise ValueError('Select an existing upgrade kit.')
    return _edit_nodes(root, {key: str(_int(collected, 'Upgrade kit collected', 1))})


def set_upgrade_points(root: ET.Element, available: str | int) -> list[str]:
    return _edit_nodes(root, {UPGRADE_POINTS_FIELD: str(_int(available, 'Available upgrade points', 999999))})
