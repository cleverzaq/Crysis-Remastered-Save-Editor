#!/usr/bin/env python3
"""Rebuild edited Crysis Remastered and Crysis 3 Remastered files.

Supported formats:
- .CSF: XMLCPB binary container + zlib chunks + MD5 trailer + game-specific encryption.
- XML/.meta files: plain XML payload + game-specific encryption (no zlib layer).

The original template is used to verify the source format and, for CSF, to
preserve XMLCPB value types. Output is always written to a separate path.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from crysis_remastered_decrypt import (
    DecryptError,
    cstreamcipher_xor,
    decrypt_file_bytes,
    extract_cipher,
)
from crysis_xmlcpb import (
    XmlCpbError,
    _node_signature,
    detect_attribute_set_entry_width,
    pack_csf_container,
    parse_xmlcpb,
    serialize_xmlcpb,
    verify_csf_container,
    xml_element_to_node,
)


def encrypt_file_bytes(payload: bytes, key: bytes, magic: bytes) -> bytes:
    """Encrypt a decrypted payload using the selected game's wrapper and stream cipher."""
    return magic + cstreamcipher_xor(payload, key)


def _atomic_write_validated(output_path: Path, encrypted: bytes, validator) -> None:
    """Write to a temporary file, validate bytes from disk, then atomically publish."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=str(output_path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        validator(Path(temp_name).read_bytes())
        os.replace(temp_name, output_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _parse_xml_bytes(data: bytes, label: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise XmlCpbError(f"{label} is not valid XML: {exc}") from exc


def rebuild_encrypted_xml(
    xml_path: Path, template_path: Path, exe_path: Path, output_path: Path
) -> dict:
    """Rebuild encrypted XML/.meta/profile.xml files without adding compression.

    In the tested Crysis Remastered, Crysis 2 Remastered and Crysis 3 Remastered
    builds, .meta and profile/config XML files decrypt directly to XML bytes. They do not contain
    zlib streams. The original encrypted file is used as the format template.
    """
    for label, path in (
        ("XML input", xml_path),
        ("Encrypted template", template_path),
        ("Game EXE", exe_path),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {path}")

    xml_resolved = xml_path.resolve()
    template_resolved = template_path.resolve()
    output_resolved = output_path.resolve()
    if output_resolved in (xml_resolved, template_resolved):
        raise ValueError("Output path must be different from both the XML input and template")

    magic, key = extract_cipher(exe_path)
    encrypted_template = template_path.read_bytes()
    template_payload = decrypt_file_bytes(encrypted_template, key, template_path.name, magic)
    _parse_xml_bytes(template_payload, f"Decrypted template {template_path.name}")

    edited_xml = xml_path.read_bytes()
    edited_root = _parse_xml_bytes(edited_xml, f"XML input {xml_path.name}")
    encrypted_output = encrypt_file_bytes(edited_xml, key, magic)

    def validate(candidate: bytes) -> None:
        decrypted = decrypt_file_bytes(candidate, key, output_path.name, magic)
        if decrypted != edited_xml:
            raise XmlCpbError("Encrypted XML round-trip did not reproduce the input bytes")
        parsed = _parse_xml_bytes(decrypted, f"Rebuilt file {output_path.name}")
        if ET.tostring(parsed, encoding="utf-8") != ET.tostring(edited_root, encoding="utf-8"):
            raise XmlCpbError("Rebuilt XML tree differs from the edited XML input")

    validate(encrypted_output)
    _atomic_write_validated(output_path, encrypted_output, validate)

    return {
        "output_size": len(encrypted_output),
        "xml_size": len(edited_xml),
        "root_tag": edited_root.tag,
        "encrypted_round_trip_valid": True,
        "compression": f"none (source format stores raw XML after {magic.decode('ascii')} decryption)",
    }


def rebuild_save(xml_path: Path, template_path: Path, exe_path: Path, output_path: Path) -> dict:
    """Create and validate an encrypted .CSF save for either supported game."""
    for label, path in (("XML input", xml_path), ("Template save", template_path), ("Game EXE", exe_path)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} does not exist: {path}")

    xml_resolved = xml_path.resolve()
    template_resolved = template_path.resolve()
    output_resolved = output_path.resolve()
    if output_resolved == template_resolved:
        raise ValueError("Output path must be different from the template save path")
    if output_resolved == xml_resolved:
        raise ValueError("Output path must be different from the input XML path")

    try:
        magic, key = extract_cipher(exe_path)
        encrypted_template = template_path.read_bytes()
        decrypted_template = decrypt_file_bytes(encrypted_template, key, template_path.name, magic)
        expanded_template, template_fields = verify_csf_container(decrypted_template)
        template_root, parsed_fields = parse_xmlcpb(expanded_template, template_fields)
        if parsed_fields != template_fields:
            raise XmlCpbError("Template XMLCPB header changed unexpectedly during parsing")

        try:
            xml_root = ET.parse(xml_path).getroot()
        except ET.ParseError as exc:
            raise XmlCpbError(f"Invalid XML input: {exc}") from exc

        edited_root = xml_element_to_node(xml_root, template_root)
        entry_width = detect_attribute_set_entry_width(expanded_template, template_fields)
        xmlcpb_data, output_fields = serialize_xmlcpb(
            edited_root, template_fields, attribute_set_entry_width=entry_width
        )
        decrypted_output = pack_csf_container(xmlcpb_data, output_fields)

        expanded_output, checked_fields = verify_csf_container(decrypted_output)
        parsed_output_root, _ = parse_xmlcpb(expanded_output, checked_fields)
        if _node_signature(parsed_output_root) != _node_signature(edited_root):
            raise XmlCpbError("Round-trip validation failed: rebuilt XMLCPB tree differs from edited XML")

        encrypted_output = encrypt_file_bytes(decrypted_output, key, magic)

        def validate(candidate: bytes) -> None:
            decrypted_again = decrypt_file_bytes(candidate, key, output_path.name, magic)
            expanded_again, final_fields = verify_csf_container(decrypted_again)
            final_root, _ = parse_xmlcpb(expanded_again, final_fields)
            if _node_signature(final_root) != _node_signature(edited_root):
                raise XmlCpbError("Final encrypted-file validation failed")

        validate(encrypted_output)
        _atomic_write_validated(output_path, encrypted_output, validate)
        final_fields = output_fields
        final_root = parsed_output_root

        return {
            "output_size": len(encrypted_output),
            "node_count": final_fields[14],
            "root_tag": final_root.tag,
            "xmlcpb_size": len(expanded_output),
            "md5_valid": True,
            "encrypted_round_trip_valid": True,
        }
    except (DecryptError, XmlCpbError, OSError, ValueError, struct.error):
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild edited Crysis Remastered, Crysis 2 Remastered or Crysis 3 Remastered XML/.CSF files."
    )
    parser.add_argument("--exe", required=True, type=Path, help="Matching game executable (CrysisRemastered.exe, Crysis2Remastered.exe or Crysis3Remastered.exe)")
    parser.add_argument("--template", required=True, type=Path, help="Original encrypted source file (.CSF, .meta, or .xml)")
    parser.add_argument("--xml", required=True, type=Path, help="Edited XML file")
    parser.add_argument("--output", required=True, type=Path, help="Output path; must differ from input and template")
    parser.add_argument(
        "--format", choices=("auto", "csf", "xml"), default="auto",
        help="Output format. auto selects CSF for .CSF templates and raw encrypted XML for .xml/.meta templates.",
    )
    args = parser.parse_args()

    file_format = args.format
    if file_format == "auto":
        suffix = args.template.suffix.lower()
        if suffix == ".csf":
            file_format = "csf"
        elif suffix in (".xml", ".meta"):
            file_format = "xml"
        else:
            print(f"[ERROR] Cannot infer output format from template extension: {suffix or '(none)'}", file=sys.stderr)
            return 2

    try:
        if file_format == "csf":
            result = rebuild_save(args.xml, args.template, args.exe, args.output)
            print(f"[OK] Rebuilt CSF save: {args.output}")
            print(f"[INFO] File size: {result['output_size']:,} bytes")
            print(f"[INFO] XMLCPB nodes: {result['node_count']:,}; root: <{result['root_tag']}>")
            print("[OK] XMLCPB MD5: valid")
            print("[OK] Encryption/decryption round-trip: valid")
        else:
            result = rebuild_encrypted_xml(args.xml, args.template, args.exe, args.output)
            print(f"[OK] Rebuilt encrypted XML file: {args.output}")
            print(f"[INFO] File size: {result['output_size']:,} bytes; XML payload: {result['xml_size']:,} bytes")
            print(f"[INFO] Root element: <{result['root_tag']}>")
            print(f"[INFO] Compression: {result['compression']}")
            print("[OK] Encryption/decryption and XML validation: valid")
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print("[INFO] The template and input XML were not modified.")
    print("[INFO] In-game loading has not been independently confirmed; keep a backup of the original file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
