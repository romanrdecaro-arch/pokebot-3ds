"""
GARC (Game ARChive) parser — reads a GARC's header tables out of the
running game's RAM, then exposes its sub-files by index.

GARC layout in memory (mirrors the on-disk format):

    +------------+ ← garc_ptr (from gfl::fs::ver4::ArcFileAccessor)
    | CRAG hdr   |   magic 'CRAG', size 0x1C, fields below
    +------------+
    | OTAF (FATO)|   array of u32 offsets into FATB, one per file
    +------------+ ← fato_ptr
    | BTAF (FATB)|   per-file: bitflags + start + end + length
    +------------+ ← fatb_ptr
    | BMIF (FIMB)|   raw file image bytes
    +------------+ ← fimb_ptr

Section magics on disk are big-endian-spelled (CRAG, OTAF, BTAF, BMIF)
but stored in little-endian on x86/ARM, so we read the four bytes raw.

This module currently only reads the structural metadata — file count,
sub-file offsets/sizes — so we can identify which game file (a/X/Y/Z)
a discovered GARC is by content fingerprint. Decompression of LZ11-
compressed entries comes later when we actually need to read inside.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .citra_rpc import CitraRPC


@dataclass
class GarcHeader:
    """Parsed CRAG header. All offsets are absolute (RAM addresses)."""
    base_addr:    int           # where the CRAG header starts in RAM
    header_size:  int           # u16 at +0x4 (typically 0x1C)
    version:      int           # u16 at +0x6
    section_count: int          # u32 at +0x8 (always 4 in Gen 6)
    data_offset:  int           # u32 at +0xC, offset to FIMB from base
    file_size:    int           # u32 at +0x10, total GARC size
    last_file_size: int         # u32 at +0x14
    fato_offset:  int           # absolute address of FATO section
    fato_count:   int           # number of files in archive


def read_garc_header(rpc: "CitraRPC", base_addr: int) -> GarcHeader | None:
    """Parse the CRAG header at ``base_addr``. Returns None on bad magic."""
    try:
        hdr = rpc.read(base_addr, 0x1C)
    except Exception:
        return None
    if len(hdr) < 0x1C or hdr[:4] != b"CRAG":
        return None
    header_size, version = struct.unpack_from("<HH", hdr, 4)
    section_count, data_offset, file_size, last_file_size = \
        struct.unpack_from("<IIII", hdr, 8)
    fato_offset = base_addr + header_size
    # FATO has its own header: magic 'OTAF' + u32 size + u16 count + u16 pad
    try:
        fato_hdr = rpc.read(fato_offset, 0xC)
    except Exception:
        return None
    if len(fato_hdr) < 0xC or fato_hdr[:4] != b"OTAF":
        return None
    fato_count = struct.unpack_from("<H", fato_hdr, 8)[0]
    return GarcHeader(
        base_addr=base_addr, header_size=header_size, version=version,
        section_count=section_count, data_offset=data_offset,
        file_size=file_size, last_file_size=last_file_size,
        fato_offset=fato_offset, fato_count=fato_count,
    )


def fingerprint(rpc: "CitraRPC", header: GarcHeader) -> dict:
    """Cheap properties for distinguishing one GARC from another.

    Returns a dict of:
      - file_count
      - file_size
      - data_size       (file_size - data_offset)
      - first_entry_size (size of file index 0)
      - last_entry_size  (size of last file)

    Useful for matching against the published GARC content list — e.g.
    the Pokémon stats archive (a/2/1/8) has a known file count, the
    encounter zonedata (a/0/1/2) has another, and so on.
    """
    out = {
        "file_count":   header.fato_count,
        "file_size":    header.file_size,
        "data_size":    header.file_size - header.data_offset,
    }
    # FATB starts right after the FATO section. FATO body is a u32
    # offset-into-FATB per file plus a 0xC header.
    fato_body_size = 4 * header.fato_count
    fatb_offset = header.fato_offset + 0xC + fato_body_size
    try:
        fatb_hdr = rpc.read(fatb_offset, 0xC)
        if len(fatb_hdr) >= 0xC and fatb_hdr[:4] == b"BTAF":
            entry_count = struct.unpack_from("<I", fatb_hdr, 8)[0]
            out["fatb_entry_count"] = entry_count
            # First entry: u32 bitflags, u32 start, u32 end, u32 length
            try:
                first = rpc.read(fatb_offset + 0xC, 0x10)
                if len(first) == 0x10:
                    _flags, start, end, length = struct.unpack("<IIII", first)
                    out["first_entry_size"] = length
                    out["first_entry_span"] = end - start
            except Exception:
                pass
    except Exception:
        pass
    return out
