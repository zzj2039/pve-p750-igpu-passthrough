#!/usr/bin/env python3
"""Patch vfio-pci.ko to work around a wrong PCI class code on the host bridge.

Why: `vfio_pci_igd_cfg_init()` requires device 00:00.0 to be a Host Bridge
(PCI class 0x060000). Some BIOSes enumerate the CPU host bridge as 0x060400
(PCI-to-PCI bridge), which makes the function return -EINVAL and the kernel
print "Failed to setup Intel IGD regions", so the iGPU cannot be assigned to
a VM.

Modes
-----
igdclass (default, preferred):
    Rewrite the immediate of `cmpl $0x60000, 0x44(%rax)` inside
    vfio_pci_igd_init() to the actual class of 00:00.0 (0x060400), so the
    check passes and everything else (OpRegion / host-bridge / LPC regions)
    is registered normally.

blunt (fallback):
    Make vfio_pci_igd_init() return 0 immediately. The device becomes usable
    but QEMU sees a plain PCI device instead of an Intel IGD (no VBT).

Both modes strip the module signature (CONFIG_MODULE_SIG_FORCE is normally
off, and a modified module keeps its now-invalid signature otherwise).

Usage:
    patch_vfio.py <src.ko> <dst.ko> [--mode igdclass|blunt]
"""
import struct
import sys

SIG_TRAILER = b'~Module signature appended~\n'
CLASS_CMP = bytes.fromhex('81784400000600')   # cmpl $0x60000,0x44(%rax)
CLASS_IMM = 0x00060400                        # actual class of 00:00.0 here
BLUNT = b'\x31\xc0\xc3'                       # xor eax,eax ; ret


def sections(data):
    e_shoff = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum = struct.unpack_from('<H', data, 0x3C)[0]
    out = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh_name, sh_type = struct.unpack_from('<II', data, off)
        sh_flags, sh_addr, sh_offset, sh_size = struct.unpack_from('<QQQQ', data, off + 8)
        out.append({'name': sh_name, 'type': sh_type, 'addr': sh_addr,
                    'off': sh_offset, 'size': sh_size})
    return out


def symbols(data, secs):
    symtab = next((s for s in secs if s['type'] == 2), None)  # SHT_SYMTAB
    if symtab is None:
        return []
    e_shoff = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    idx = secs.index(symtab)
    sh_link = struct.unpack_from('<I', data, e_shoff + idx * e_shentsize + 0x28)[0]
    strtab = secs[sh_link]
    out = []
    for i in range(symtab['size'] // 24):
        o = symtab['off'] + i * 24
        name_off, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', data, o)
        end = data.index(b'\0', strtab['off'] + name_off)
        name = data[strtab['off'] + name_off:end].decode('utf-8', 'replace')
        out.append({'name': name, 'value': value, 'shndx': shndx, 'size': size})
    return out


def strip_signature(data):
    i = data.rfind(SIG_TRAILER)
    if i < 0:
        return data, False
    sig_len = struct.unpack_from('>I', data, i - 12 + 8)[0]
    start = i + len(SIG_TRAILER) - (12 + sig_len + len(SIG_TRAILER))
    return data[:start], True


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    mode = 'igdclass'
    if '--mode' in sys.argv:
        mode = sys.argv[sys.argv.index('--mode') + 1]

    data = bytearray(open(src, 'rb').read())
    secs = sections(data)
    sym = next((s for s in symbols(data, secs) if s['name'] == 'vfio_pci_igd_init'), None)
    if sym is None:
        print('error: symbol vfio_pci_igd_init not found', file=sys.stderr)
        return 1

    sec = secs[sym['shndx']]
    foff = sec['off'] + (sym['value'] - sec['addr'])
    body = bytes(data[foff:foff + (sym['size'] or 0x400)])

    if mode == 'igdclass':
        idx = body.find(CLASS_CMP)
        if idx < 0:
            print('warning: class check not found, falling back to blunt mode',
                  file=sys.stderr)
            mode = 'blunt'
        else:
            imm = foff + idx + 3
            old = struct.unpack_from('<I', data, imm)[0]
            struct.pack_into('<I', data, imm, CLASS_IMM)
            print('igdclass: %#x cmpl imm %#010x -> %#010x' % (sym['value'] + idx, old, CLASS_IMM))

    if mode == 'blunt':
        pos = foff + 5 if data[foff] == 0xE8 else foff
        data[pos:pos + 3] = BLUNT
        print('blunt: patched %#x with %s' % (pos, BLUNT.hex()))

    out, stripped = strip_signature(bytes(data))
    open(dst, 'wb').write(out)
    print('mode=%s signature_stripped=%s size=%d -> %s' % (mode, stripped, len(out), dst))
    return 0


if __name__ == '__main__':
    sys.exit(main())
