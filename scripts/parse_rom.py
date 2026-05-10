"""
Parse a decrypted .3ds (NCSD) ROM and extract Partition 0's ExeFS files
plus the ExHeader code-section addresses. Useful for picking up the
game's static virtual-memory layout.

Output: /tmp/<rom>/exh.bin, /tmp/<rom>/exefs/code.bin (etc.)

Usage:
    python scripts/parse_rom.py "D:\\3ds\\Pokemon Y\\Pokemon Y...-decrypted.3ds"
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    rom_path = Path(sys.argv[1])
    if not rom_path.exists():
        print(f"ROM not found: {rom_path}")
        sys.exit(1)

    out_dir = rom_path.parent / "_extracted"
    out_dir.mkdir(exist_ok=True)

    with rom_path.open("rb") as f:
        # NCSD header at 0x0
        f.seek(0x100)
        magic = f.read(4)
        if magic != b"NCSD":
            print(f"Not NCSD: magic={magic!r}")
            sys.exit(1)
        f.seek(0x120)
        partition_table = f.read(0x40)
        p0_offset_mu, p0_size_mu = struct.unpack_from("<II", partition_table, 0)
        p0_offset = p0_offset_mu * 0x200
        p0_size   = p0_size_mu * 0x200
        print(f"Partition 0: {p0_offset:#x} .. {p0_offset + p0_size:#x}")

        # NCCH header at p0_offset
        f.seek(p0_offset + 0x100)
        if f.read(4) != b"NCCH":
            print("NCCH magic missing at partition 0")
            sys.exit(1)
        f.seek(p0_offset + 0x150)
        product_code = f.read(0x10).rstrip(b"\x00").decode("ascii", "replace")

        f.seek(p0_offset + 0x180)
        exh_size_full = struct.unpack("<I", f.read(4))[0]   # in bytes (already)
        f.seek(p0_offset + 0x1A0)
        exefs_off_mu, exefs_size_mu = struct.unpack("<II", f.read(8))
        f.seek(p0_offset + 0x1B0)
        romfs_off_mu, romfs_size_mu = struct.unpack("<II", f.read(8))

        exefs_off  = p0_offset + exefs_off_mu * 0x200
        exefs_size = exefs_size_mu * 0x200
        romfs_off  = p0_offset + romfs_off_mu * 0x200
        romfs_size = romfs_size_mu * 0x200
        print(f"  Product:    {product_code}")
        print(f"  ExHeader:   {exh_size_full:#x} bytes")
        print(f"  ExeFS:      {exefs_off:#x} .. {exefs_off + exefs_size:#x}")
        print(f"  RomFS:      {romfs_off:#x} .. {romfs_off + romfs_size:#x}")

        # Extract ExHeader (right after the 0x200 NCCH header).
        f.seek(p0_offset + 0x200)
        exh = f.read(exh_size_full or 0x800)
        (out_dir / "exh.bin").write_bytes(exh)
        # Decode the SCI (System Control Info) section — text/data offsets.
        # ExHeader.SCI starts at offset 0x0; .text params at 0x10:
        #   0x10  u32  text_address   (virtual)
        #   0x14  u32  text_pages
        #   0x18  u32  text_code_size
        #   0x20  u32  ro_address
        #   0x24  u32  ro_pages
        #   0x28  u32  ro_code_size
        #   0x30  u32  data_address
        #   0x34  u32  data_pages
        #   0x38  u32  data_code_size
        text_addr, text_pages, text_size = struct.unpack_from("<III", exh, 0x10)
        ro_addr,   ro_pages,   ro_size   = struct.unpack_from("<III", exh, 0x20)
        data_addr, data_pages, data_size = struct.unpack_from("<III", exh, 0x30)
        bss_size = struct.unpack_from("<I", exh, 0x3C)[0]
        stack_size = struct.unpack_from("<I", exh, 0x1C)[0]
        print()
        print("Code segments (virtual addresses where the loader maps them):")
        print(f"  .text:  {text_addr:#010x} size {text_size:#x} ({text_size//1024} KB)")
        print(f"  .ro:    {ro_addr:#010x} size {ro_size:#x} ({ro_size//1024} KB)")
        print(f"  .data:  {data_addr:#010x} size {data_size:#x} ({data_size//1024} KB)")
        print(f"  .bss:   {bss_size:#x} bytes (sits right after .data)")

        # ExeFS header at exefs_off: 8 entries of 16 bytes each + 32-byte hashes.
        f.seek(exefs_off)
        exefs_hdr = f.read(0x200)
        ex_dir = out_dir / "exefs"
        ex_dir.mkdir(exist_ok=True)
        print()
        print("ExeFS files:")
        for i in range(8):
            entry = exefs_hdr[i * 0x10 : (i + 1) * 0x10]
            name = entry[:8].rstrip(b"\x00").decode("ascii", "replace")
            file_off, file_size = struct.unpack_from("<II", entry, 8)
            if not name:
                continue
            print(f"  {name:>10s}  off={file_off:#x}  size={file_size:#x}")
            f.seek(exefs_off + 0x200 + file_off)
            data = f.read(file_size)
            (ex_dir / name).write_bytes(data)

    print(f"\nWrote ExHeader + ExeFS to {out_dir}")


if __name__ == "__main__":
    main()
