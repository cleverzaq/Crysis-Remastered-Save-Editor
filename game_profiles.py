"""Verified, game-specific attribute-profile categorization and safe editing."""
from __future__ import annotations
from pathlib import Path
import os
from savegame_paths import windows_saved_games_folder
import re
import xml.etree.ElementTree as ET

GAME_PROFILES = {
    "CrysisRemastered": {"magic": b"CRYePC1", "filename": "attributes_pc.xml", "sections": ("Campaign Progress", "Combat Statistics", "Difficulty", "Settings and Other")},
    "Crysis2Remastered": {"magic": b"CRYePC2", "filename": "attributes_pc.xml", "sections": ("Campaign Progress", "Nanosuit Perks", "Combat Statistics", "Collectibles", "Multiplayer", "Difficulty", "Settings and Other")},
    "Crysis3Remastered": {"magic": b"C3KePC2", "filename": "attributes.xml", "sections": ("Campaign Progress", "Nanosuit Upgrades", "Nanosuit Perks", "Combat Statistics", "Collectibles", "Multiplayer", "Difficulty", "Settings and Other")},
}

def identify_game(magic: bytes) -> str | None:
    return next((name for name, config in GAME_PROFILES.items() if config["magic"] == magic), None)

def categorize(game: str, name: str) -> str:
    lower = name.lower()
    if name == "SP/PersistantStats/EMPS_SPLevelByDifficulty/init":
        return "Campaign Progress"
    if name in ("Difficulty", "SP/Difficulty", "Option.g_difficultyLevel") or "difficulty" in lower:
        return "Difficulty"
    if name.endswith(".maxLevelProgress") or name.startswith("Singleplayer."):
        return "Campaign Progress"
    if name.startswith("NanosuitUpgrades/"):
        return "Nanosuit Upgrades"
    if name.startswith(("PerkActive/", "PerkDistribution/", "PerkTier/", "PerkUnlock/", "PerkPurchased/")) or "perk" in lower:
        return "Nanosuit Perks"
    if name.startswith("MP/"):
        return "Multiplayer"
    if any(token in lower for token in ("collect", "dogtag", "intel", "souvenir", "artifact")):
        return "Collectibles"
    if name.startswith("SP/PersistantStats/"):
        return "Combat Statistics"
    return "Settings and Other"

def profile_fields(root: ET.Element, game: str):
    for node in root.iter("Attr"):
        if "name" in node.attrib and "value" in node.attrib:
            name = node.attrib["name"]
            yield categorize(game, name), name, node.attrib["value"], node

def validate_value(old: str, new: str) -> str:
    if not new or not old:
        return new
    if re.fullmatch(r"[+-]?\d+", old):
        if not re.fullmatch(r"[+-]?\d+", new.strip()):
            raise ValueError("This field requires an integer value.")
        return new.strip()
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", old):
        try:
            float(new)
        except ValueError as exc:
            raise ValueError("This field requires a numeric value.") from exc
        return new.strip()
    return new

def locate_profile(save_path: Path, game: str) -> Path | None:
    """Find the correct attributes file for any named Remastered profile.

    Prefer profile matching the selected save's filename prefix; otherwise
    prefer default, then the sole available named profile. Do not choose an
    arbitrary profile when several candidates are equally plausible.
    """
    config = GAME_PROFILES[game]
    source = Path(save_path)
    # An explicitly selected attribute profile is authoritative; don't
    # silently replace Profiles/custom with Profiles/default.
    if (source.is_file() and source.name.casefold() == config['filename'].casefold()
            and source.parent.parent.name.casefold() == 'profiles'):
        return source
    if (source.is_dir() and source.parent.name.casefold() == 'profiles'
            and (source / config['filename']).is_file()):
        return source / config['filename']
    start = source if source.is_dir() else source.parent
    game_roots: list[Path] = []
    def add(root: Path) -> None:
        if root not in game_roots:
            game_roots.append(root)
    for parent in (start, *start.parents):
        if parent.name.casefold() == game.casefold():
            add(parent)
            break
        if parent.name.casefold() == "savegames":
            add(parent.parent)
            break
        if parent.name.casefold() == "profiles":
            add(parent.parent)
            break
    known = windows_saved_games_folder()
    if known is not None:
        add(known / game)
    add(Path(os.environ.get("USERPROFILE") or Path.home()) / "Saved Games" / game)

    filename = config['filename']
    save_stem = Path(save_path).stem.casefold() if Path(save_path).suffix.casefold() == '.csf' else ''
    for root in game_roots:
        profiles = root / 'Profiles'
        if profiles.is_dir():
            candidates = sorted(
                (p for p in profiles.iterdir() if p.is_dir() and (p / filename).is_file()),
                key=lambda p: p.name.casefold(),
            )
            # A prefixed .CSF is authoritative about which profile it belongs to.
            if save_stem:
                matches = [p for p in candidates if save_stem.startswith(p.name.casefold() + '_')]
                if matches:
                    # Only a complete, longest profile prefix may win.
                    longest = max(len(p.name) for p in matches)
                    best = [p for p in matches if len(p.name) == longest]
                    return best[0] / filename if len(best) == 1 else None
                # A numbered unprefixed save may belong to the default profile.
                # An arbitrary stem might refer to an unknown custom profile;
                # never guess 'default' when multiple profiles are present.
                if len(candidates) > 1 and not re.match(r"^\d+_", save_stem):
                    return None
            defaults = [p for p in candidates if p.name.casefold() == 'default']
            if len(defaults) == 1:
                return defaults[0] / filename
            if len(candidates) == 1:
                return candidates[0] / filename
            if len(candidates) > 1:
                return None  # Ask user rather than select a random profile.
        for candidate in (root / filename, profiles / filename):
            if candidate.is_file():
                return candidate
    return None
