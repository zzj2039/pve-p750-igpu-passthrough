#!/usr/bin/env python3
"""Patch vfio-pci.ko so vfio_pci_igd_init() immediately returns 0.

Usage: patch_vfio.py <in.ko> <out.ko> [--apply]
Without --apply it only reports what would be changed.
"""
import struct, sys

TARGET = 'vfio_pci_igd_init'
PROLOGUE = b'\xf3\x0f\x1e\xfa'  # endbr64
PATCH = b'\x31\xc0\xc3'         # xor eax,eax ; ret


def sections(data):
    e_shoff = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum = struct.unpack_from('<H', data, 0x3C)[0]
    out = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh_name, sh_type = struct.unpack_from('<II', data, off)
        sh_flags, sh_addr, sh_offset, sh_size = struct.unpack_from('<QQQQ', data, off + 8)
        out.append((sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size))
    return out


def symbols(data):
    """Return list of (name, value, shndx) from .symtab."""
    secs = sections(data)
    symtab = None
    strtab = None
    for s in secs:
        if s[1] == 2:      # SHT_SYMTAB
            symtab = s
    if not symtab:
        return []
    # re-read symtab section header to get sh_link
    e_shoff = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    idx = secs.index(symtab)
    sh_link = struct.unpack_from('<I', data, e_shoff + idx * e_shentsize + 0x28)[0]
    strtab = secs[sh_link]
    syms = []
    n = symtab[5] // 24
    for i in range(n):
        o = symtab[4] + i * 24
        name_off, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', data, o)
        end = data.index(b'\0', strtab[4] + name_off)
        name = data[strtab[4] + name_off:end].decode('utf-8', 'replace')
        syms.append((name, value, shndx))
    return syms


def main():
    src, dst = sys.argv[1], sys.argv[2]
    apply = '--apply' in sys.argv
    data = bytearray(open(src, 'rb').read())
    secs = sections(data)
    syms = symbols(data)
    hit = [s for s in syms if s[0] == TARGET]
    if not hit:
        print("symbol %s not found" % TARGET)
        return 1
    name, value, shndx = hit[0]
    sec = secs[shndx]
    sh_addr, sh_offset, sh_size = sec[3], sec[4], sec[5]
    if not (sh_addr <= value < sh_addr + sh_size):
        print("symbol outside its section")
        return 1
    foff = sh_offset + (value - sh_addr)
    print("symbol %s value=%#x section=%d file_off=%#x" % (name, value, shndx, foff))
    head = bytes(data[foff:foff + 8])
    print("bytes: %s" % head.hex())
    if head[:4] != PROLOGUE:
        print("WARNING: no endbr64 at function start")
    pos = foff + 4 if head[:4] == PROLOGUE else foff
    print("patching %#x: %s -> %s" % (pos, bytes(data[pos:pos + 3]).hex(), PATCH.hex()))
    if apply:
        data[pos:pos + 3] = PATCH
        open(dst, 'wb').write(bytes(data))
        print("written to", dst)
    return 0


sys.exit(main())
