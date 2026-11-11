#!/bin/bash
# 重新给当前内核的 vfio-pci.ko 打补丁：让 vfio_pci_igd_init() 直接返回 0
# 用法: /root/repatch-vfio.sh [内核版本，默认当前]
set -e
KVER=${1:-$(uname -r)}
KO=/lib/modules/$KVER/kernel/drivers/vfio/pci/vfio-pci.ko
[ -f "$KO" ] || { echo "找不到 $KO"; exit 1; }
cp -n "$KO" "$KO.orig-backup" || true
python3 - "$KO" "$KO.new" <<'EOF'
import sys, struct
src, out = sys.argv[1], sys.argv[2]
data = bytearray(open(src,'rb').read())
if data[-27:-1] != b'~Module signature appende':
    pass
e_shoff=struct.unpack_from('<Q',data,0x28)[0]; es=struct.unpack_from('<H',data,0x3A)[0]; n=struct.unpack_from('<H',data,0x3C)[0]
secs=[]
for i in range(n):
    o=e_shoff+i*es
    nm,ty=struct.unpack_from('<II',data,o)
    fl,ad,of,sz=struct.unpack_from('<QQQQ',data,o+8)
    secs.append((nm,ty,fl,ad,of,sz,o))
symtab=[s for s in secs if s[1]==2][0]
sh_link=struct.unpack_from('<I',data,symtab[6]+0x28)[0]
strtab=secs[sh_link]
tgt=None
for i in range(symtab[5]//24):
    o=symtab[4]+i*24
    no,info,oth,shndx,val,size=struct.unpack_from('<IBBHQQ',data,o)
    end=data.index(b'\0',strtab[4]+no); name=data[strtab[4]+no:end].decode()
    if name=='vfio_pci_igd_init': tgt=(val,shndx)
if not tgt: print("未找到符号"); sys.exit(1)
val,shndx=tgt; sec=secs[shndx]; foff=sec[4]+(val-sec[3])
pos = foff+5 if data[foff]==0xE8 else foff
data[pos:pos+3]=b'\x31\xc0\xc3'
MAGIC=b'~Module signature appended~\n'
i=data.rfind(MAGIC)
if i>0:
    siglen=struct.unpack_from('>I',data,i-12+8)[0]
    start=i+len(MAGIC)-(12+siglen+len(MAGIC))
    data=data[:start]
open(out,'wb').write(bytes(data))
print("已生成补丁模块 %d 字节" % len(data))
EOF
mv "$KO.new" "$KO"
depmod -a "$KVER"
echo "完成：$KO 已打补丁（原版备份在 $KO.orig-backup）"
echo "需要重启才生效"
