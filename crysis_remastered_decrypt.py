#!/usr/bin/env python3
"""Decrypt Crysis Remastered, Crysis 2 Remastered and Crysis 3 Remastered files.

The key and file wrapper are detected from the matching game executable:
- Crysis Remastered: CRYePC1 + 5 zero padding bytes + 256-byte key
- Crysis 2 Remastered: 256-byte key immediately before the CRYePC2 marker
- Crysis 3 Remastered: C3KePC2 + 1 zero padding byte + 256-byte key

Examples:
  python crysis_remastered_decrypt.py --exe CrysisRemastered.exe save.CSF save.meta attributes_pc.xml
  python crysis_remastered_decrypt.py --exe Crysis2Remastered.exe save.CSF save.meta attributes_pc.xml profile.xml
  python crysis_remastered_decrypt.py --exe Crysis3Remastered.exe save.CSF save.meta attributes.xml profile.xml
  python crysis_remastered_decrypt.py --exe Crysis3Remastered.exe --out-dir decrypted *.CSF *.meta *.xml

For .CSF files, the script also expands the chunked zlib container and writes
an uncompressed .bin. Any trailing bytes after the final complete chunk are
preserved in a separate .footer file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable

CRYSIS1_MAGIC = b"CRYePC1"  # Crysis Remastered.
CRYSIS2_MAGIC = b"CRYePC2"
CRYSIS3_MAGIC = b"C3KePC2"
SUPPORTED_MAGICS = (CRYSIS1_MAGIC, CRYSIS2_MAGIC, CRYSIS3_MAGIC)
KEY_LENGTH = 256
GAME_CIPHER_LAYOUTS = {
    CRYSIS1_MAGIC: {"padding_length": 13, "key_offset_after_magic": 20},
    # In the tested Crysis 2 Remastered PC executable, the 256-byte cipher
    # key is stored immediately before the unique CRYePC2 marker.
    CRYSIS2_MAGIC: {"key_offset_before_magic": KEY_LENGTH},
    CRYSIS3_MAGIC: {"padding_length": 1, "key_offset_after_magic": 8},
}
DROP_BYTES = 1024
MAX_CHUNK_SIZE = 128 * 1024 * 1024


class DecryptError(Exception):
    pass


KEY_BACKUP_EXTENSION = ".crysiskey"


def load_key_backup(path: Path | str) -> tuple[bytes, bytes]:
    """Load and verify a portable game-specific cipher key backup."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != "crysis-save-editor-key-v1":
            raise ValueError("Unsupported key backup format")
        magic = payload["magic"].encode("ascii")
        key = base64.b64decode(payload["key_base64"], validate=True)
        if magic not in SUPPORTED_MAGICS or len(key) != KEY_LENGTH:
            raise ValueError("Invalid game signature or key length")
        if hashlib.sha256(key).hexdigest() != payload["sha256"]:
            raise ValueError("Key checksum mismatch")
        return magic, key
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise DecryptError(f"Invalid key backup: {exc}") from exc


def save_key_backup(source: Path | str, destination: Path | str) -> Path:
    """Export a self-contained key file without exporting the game executable."""
    magic, key = extract_cipher(Path(source))
    target = Path(destination)
    payload = {
        "format": "crysis-save-editor-key-v1",
        "game": {CRYSIS1_MAGIC: "Crysis Remastered", CRYSIS2_MAGIC: "Crysis 2 Remastered", CRYSIS3_MAGIC: "Crysis 3 Remastered"}[magic],
        "magic": magic.decode("ascii"),
        "key_base64": base64.b64encode(key).decode("ascii"),
        "sha256": hashlib.sha256(key).hexdigest(),
    }
    # Refuse silent overwrite of existing backups.
    with target.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return target


def extract_cipher(exe_path: Path) -> tuple[bytes, bytes]:
    """Return (file_magic, 256-byte key) from an EXE or key backup."""
    exe_path = Path(exe_path)
    if exe_path.suffix.lower() == KEY_BACKUP_EXTENSION:
        return load_key_backup(exe_path)
    try:
        exe = exe_path.read_bytes()
    except OSError as exc:
        raise DecryptError(f"Could not read EXE: {exc}") from exc

    matches: list[tuple[bytes, int]] = []
    for marker in SUPPORTED_MAGICS:
        pos = 0
        while True:
            found = exe.find(marker, pos)
            if found < 0:
                break
            matches.append((marker, found))
            pos = found + 1

    if len(matches) != 1:
        marker_names = ", ".join(marker.decode("ascii") for marker in SUPPORTED_MAGICS)
        raise DecryptError(
            f"Expected exactly one supported save-key marker ({marker_names}) in EXE; "
            f"found {len(matches)}."
        )

    marker, marker_offset = matches[0]
    layout = GAME_CIPHER_LAYOUTS[marker]
    if "key_offset_before_magic" in layout:
        key_start = marker_offset - layout["key_offset_before_magic"]
        if key_start < 0:
            raise DecryptError("The EXE is truncated; could not read the 256-byte key before CRYePC2.")
        key = exe[key_start:marker_offset]
        if len(key) != KEY_LENGTH:
            raise DecryptError("The EXE is truncated; could not read the 256-byte key before CRYePC2.")
    else:
        key_start = marker_offset + layout["key_offset_after_magic"]
        key = exe[key_start:key_start + KEY_LENGTH]
        if len(key) != KEY_LENGTH:
            raise DecryptError("The EXE is truncated; could not read the 256-byte key.")

        padding_start = marker_offset + len(marker)
        padding_end = marker_offset + layout["key_offset_after_magic"]
        padding = exe[padding_start:padding_end]
        if padding != b"\x00" * layout["padding_length"]:
            raise DecryptError(
                f"Unexpected data layout after {marker.decode('ascii')} in EXE; stopped for safety."
            )
    return marker, key


def extract_key(exe_path: Path) -> bytes:
    """Compatibility wrapper returning only the key from a supported EXE."""
    return extract_cipher(exe_path)[1]


def cstreamcipher_xor(data: bytes, key: bytes) -> bytes:
    """Crytek CStreamCipher: RC4-style KSA/PRGA with the first 1024 bytes dropped."""
    if not key:
        raise DecryptError("Encryption key is empty.")

    s = list(range(256))
    j = 0
    key_len = len(key)

    # Key Scheduling Algorithm (KSA)
    for i in range(256):
        j = (j + s[i] + key[i % key_len]) & 0xFF
        s[i], s[j] = s[j], s[i]

    # Discard the first 1024 bytes of the keystream.
    i = 0
    j = 0
    for _ in range(DROP_BYTES):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        _ = s[(s[i] + s[j]) & 0xFF]

    out = bytearray(len(data))
    for n, value in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = value ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def decrypt_file_bytes(
    encrypted: bytes, key: bytes, source_name: str, expected_magic: bytes | None = None
) -> bytes:
    """Decrypt a supported save/config wrapper, optionally enforcing its game marker."""
    if expected_magic is None:
        actual_magic = next((m for m in SUPPORTED_MAGICS if encrypted.startswith(m)), None)
    else:
        actual_magic = expected_magic if encrypted.startswith(expected_magic) else None
    if actual_magic is None:
        expected = expected_magic.decode("ascii") if expected_magic else " or ".join(
            item.decode("ascii") for item in SUPPORTED_MAGICS
        )
        raise DecryptError(f"{source_name}: missing expected header {expected}.")
    # The 7-byte identifier is clear text; stream-cipher data starts immediately after it.
    return cstreamcipher_xor(encrypted[len(actual_magic):], key)


def decompress_chunked_zlib(data: bytes) -> tuple[bytes, bytes, int]:
    """Expand CSF's [compressed_len:u32][plain_len:u32][zlib bytes] chunks."""
    result = bytearray()
    offset = 0
    chunks = 0

    while offset + 8 <= len(data):
        compressed_len, plain_len = struct.unpack_from("<II", data, offset)
        if (
            compressed_len == 0
            or plain_len == 0
            or compressed_len > MAX_CHUNK_SIZE
            or plain_len > MAX_CHUNK_SIZE
            or offset + 8 + compressed_len > len(data)
        ):
            break

        block_start = offset + 8
        block_end = block_start + compressed_len
        compressed = data[block_start:block_end]
        try:
            plain = zlib.decompress(compressed)
        except zlib.error:
            break
        if len(plain) != plain_len:
            break

        result.extend(plain)
        chunks += 1
        offset = block_end

    if chunks == 0:
        raise DecryptError("No valid zlib blocks were found in the CSF data.")
    return bytes(result), data[offset:], chunks


def output_names(source: Path) -> tuple[str, str | None]:
    suffix = source.suffix.lower()
    if suffix == ".csf":
        return f"{source.stem}.decrypted.csfbin", f"{source.stem}.decompressed.bin"
    if suffix == ".xml":
        return f"{source.stem}.decrypted.xml", None
    if suffix == ".meta":
        return f"{source.stem}.decrypted.meta.xml", None
    return f"{source.name}.decrypted", None


def process_file(
    source: Path, out_dir: Path | None, key: bytes, expected_magic: bytes, do_csf: bool = True
) -> None:
    try:
        encrypted = source.read_bytes()
    except OSError as exc:
        print(f"[ERROR] {source}: could not read: {exc}", file=sys.stderr)
        return

    try:
        decrypted = decrypt_file_bytes(encrypted, key, source.name, expected_magic)
    except DecryptError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return

    # By default, keep extracted files next to their corresponding source file.
    # An explicit --out-dir still overrides this for users who prefer a separate folder.
    destination_dir = out_dir if out_dir is not None else source.parent
    destination_dir.mkdir(parents=True, exist_ok=True)
    raw_name, expanded_name = output_names(source)
    raw_path = destination_dir / raw_name
    raw_path.write_bytes(decrypted)
    print(f"[OK] {source.name} -> {raw_path} ({len(decrypted):,} B)")

    if source.suffix.lower() == ".csf" and do_csf:
        try:
            expanded, footer, chunks = decompress_chunked_zlib(decrypted)
        except DecryptError as exc:
            print(f"[WARNING] {source.name}: decryption succeeded; decompression was skipped: {exc}")
            return

        expanded_path = destination_dir / (expanded_name or f"{source.stem}.decompressed.bin")
        expanded_path.write_bytes(expanded)
        print(
            f"[OK] CSF: {chunks} zlib blocks -> {expanded_path} "
            f"({len(expanded):,} B expanded)"
        )
        if footer:
            footer_path = destination_dir / f"{source.stem}.footer.bin"
            footer_path.write_bytes(footer)
            print(
                f"[INFO] Preserved {len(footer)} trailing bytes outside the zlib blocks: "
                f"{footer_path}"
            )

        # For CSF, also reconstruct the full XMLCPB document as readable XML.
        try:
            from crysis_xmlcpb import parse_csf_decrypted
            xml_path = destination_dir / f"{source.stem}.xml"
            root, fields, expanded_size = parse_csf_decrypted(raw_path, xml_path)
            print(
                f"[OK] XMLCPB: {fields[14]:,} nodes, root <{root.tag}> "
                f"-> {xml_path} ({xml_path.stat().st_size:,} B XML)"
            )
        except ImportError:
            print("[INFO] crysis_xmlcpb.py is missing beside the script; skipped CSF-to-XML conversion.")
        except Exception as exc:
            print(f"[WARNING] CSF decryption succeeded, but XMLCPB conversion failed: {exc}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decrypt Crysis Remastered, Crysis 2 Remastered or Crysis 3 Remastered save/config files."
    )
    parser.add_argument("files", nargs="+", type=Path, help="Files to decrypt (.CSF, .meta, .xml, or other supported encrypted files)")
    parser.add_argument("--exe", required=True, type=Path, help="Path to the matching game executable")
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Optional output directory. By default, extracted files are written next to each input file.",
    )
    parser.add_argument("--no-csf-decompress", action="store_true", help="Do not expand zlib blocks in .CSF files")
    args = parser.parse_args(argv)

    try:
        game_magic, key = extract_cipher(args.exe)
    except DecryptError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print(f"Detected game format {game_magic.decode('ascii')}; key extracted ({len(key)} bytes).")
    if args.out_dir is None:
        print("Output mode: alongside each input file (same directory).")
    else:
        print(f"Output directory override: {args.out_dir}")
    errors_before = 0
    for source in args.files:
        if not source.is_file():
            print(f"[ERROR] File not found: {source}", file=sys.stderr)
            errors_before += 1
            continue
        process_file(source, args.out_dir, key, game_magic, do_csf=not args.no_csf_decompress)

    return 1 if errors_before else 0


if __name__ == "__main__":
    raise SystemExit(main())
