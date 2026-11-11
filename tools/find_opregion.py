#!/usr/bin/env python3
"""Locate the Intel IGD OpRegion in ACPI NVS physical memory via /proc/kcore.
With --write, program its physical address into BAR0+0xFC of the iGPU."""
import struct, sys, os, mmap

SIG = b'IntelGraphicsMem'


def nvs_ranges():
    rngs = []
    for line in open('/proc/iomem'):
        if 'ACPI Non-volatile Storage' in line or 'ACPI Data' in line:
            part = line.split(':')[0].strip()
            try:
                lo, hi = part.split('-')
                rngs.append((int(lo, 16), int(hi, 16) + 1))
            except ValueError:
                continue
    return rngs


def load_segments():
    f = open('/proc/kcore', 'rb')
    hdr = f.read(64)
    e_phoff = struct.unpack_from('<Q', hdr, 32)[0]
    e_phentsize = struct.unpack_from('<H', hdr, 54)[0]
    e_phnum = struct.unpack_from('<H', hdr, 56)[0]
    segs = []
    for i in range(e_phnum):
        f.seek(e_phoff + i * e_phentsize)
        ph = f.read(56)
        if len(ph) < 56:
            break
        p_type, p_flags = struct.unpack_from('<II', ph, 0)
        p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = struct.unpack_from('<QQQQQQ', ph, 8)
        if p_type == 1 and p_memsz > 0:
            segs.append((p_vaddr, p_memsz, p_offset, p_filesz))
    return f, segs


def main():
    f, segs = load_segments()
    direct = [s for s in segs if 0xffff800000000000 <= s[0] <= 0xffffc88000000000]
    direct.sort(key=lambda s: -s[1])
    if not direct:
        print("no direct-map segment found")
        for s in segs[:10]:
            print("  vaddr=%#x size=%#x" % (s[0], s[1]))
        return 1
    p_vaddr, p_memsz, p_offset, p_filesz = direct[0]
    page_offset_base = p_vaddr
    print("direct map: vaddr=%#x size=%#x fileoff=%#x" % (p_vaddr, p_memsz, p_offset))

    found = []
    ranges = nvs_ranges()
    print('scanning ranges:', [(hex(a),hex(b)) for a,b in ranges])
    for lo, hi in ranges:
        for phys in range(lo, hi, 0x1000):
            vaddr = page_offset_base + phys
            off = p_offset + (vaddr - p_vaddr)
            try:
                f.seek(off)
                chunk = f.read(0x100)
            except Exception:
                continue
            if chunk and chunk[:16] == SIG:
                print("FOUND opregion at phys=0x%x hdr=%s" % (phys, chunk[16:32].hex()))
                found.append(phys)

    if not found:
        print("opregion signature not found in NVS ranges")
        return 2

    if len(sys.argv) > 1 and sys.argv[1] == "--write":
        addr = found[0]
        dev = '/sys/bus/pci/devices/0000:00:02.0/resource0'
        fd = os.open(dev, os.O_RDWR | os.O_SYNC)
        try:
            m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            before = struct.unpack('<I', m[0xFC:0x100])[0]
            print("0xFC before = 0x%08X" % before)
            m[0xFC:0x100] = struct.pack('<I', addr)
            m.flush()
            after = struct.unpack('<I', m[0xFC:0x100])[0]
            print("0xFC after  = 0x%08X (wrote 0x%X)" % (after, addr))
            m.close()
        finally:
            os.close(fd)
    return 0


sys.exit(main())
