#!/usr/bin/env python3
"""Scan physical memory via /proc/kcore for the IGD OpRegion signature."""
import struct, sys

SIG = b'IntelGraphicsMem'
CHUNK = 1 << 20  # 1 MiB


def segs():
    f = open('/proc/kcore', 'rb')
    hdr = f.read(64)
    e_phoff = struct.unpack_from('<Q', hdr, 32)[0]
    e_phentsize = struct.unpack_from('<H', hdr, 54)[0]
    e_phnum = struct.unpack_from('<H', hdr, 56)[0]
    out = []
    for i in range(e_phnum):
        f.seek(e_phoff + i * e_phentsize)
        ph = f.read(56)
        if len(ph) < 56:
            break
        p_type = struct.unpack_from('<I', ph, 0)[0]
        p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = struct.unpack_from('<QQQQQQ', ph, 8)
        if p_type == 1 and p_memsz > 0:
            out.append((p_vaddr, p_memsz, p_offset))
    return f, out


def main():
    limit = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x100000000
    f, sg = segs()
    direct = [s for s in sg if 0xffff800000000000 <= s[0] <= 0xffffc88000000000]
    direct.sort(key=lambda s: -s[1])
    if not direct:
        print("no direct map")
        return 1
    p_vaddr, p_memsz, p_offset = direct[0]
    print("direct map base=%#x size=%#x" % (p_vaddr, p_memsz))
    end = min(limit, p_memsz)
    hits = []
    phys = 0
    while phys < end:
        n = min(CHUNK, end - phys)
        try:
            f.seek(p_offset + phys)
            buf = f.read(n)
        except Exception as e:
            phys += n
            continue
        if not buf:
            break
        idx = buf.find(SIG)
        while idx != -1:
            hits.append(phys + idx)
            idx = buf.find(SIG, idx + 1)
        phys += len(buf)
    print("scanned 0..%#x, hits=%d" % (end, len(hits)))
    for h in hits[:10]:
        print("  HIT phys=0x%x  aligned4k=%s" % (h, h % 0x1000 == 0))
    if hits:
        h = hits[0]
        try:
            f.seek(p_offset + (h & ~0xfff))
            hdr = f.read(0x60)
            print("  header:", hdr[:0x40].hex())
        except Exception as e:
            print("  header read err", e)
    return 0


sys.exit(main())
