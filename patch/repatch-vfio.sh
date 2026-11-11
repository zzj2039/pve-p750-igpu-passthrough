#!/bin/bash
# 重新给指定内核的 vfio-pci.ko 打补丁。
#
# 背景：某些 BIOS 把 00:00.0（CPU Host Bridge）的 PCI class 枚举成 0x0604
# 而不是 0x0600，导致内核 vfio_pci_igd_cfg_init() 返回 -EINVAL，
# 核显无法直通。补丁让这处校验接受实际的 class 值。
#
# 用法：
#   ./repatch-vfio.sh                 # 当前内核
#   ./repatch-vfio.sh 6.14.11-4-pve   # 指定内核
#
# 需要 patch_vfio.py 在同一目录（或 /root 下）。
set -e

KVER=${1:-$(uname -r)}
KO=/lib/modules/$KVER/kernel/drivers/vfio/pci/vfio-pci.ko
ORIG=$KO.orig-backup

[ -f "$KO" ] || { echo "找不到 $KO"; exit 1; }
# 首次运行时把当前（原版）模块存一份，之后始终从这份原件重新生成
[ -f "$ORIG" ] || cp "$KO" "$ORIG"

HERE=$(cd "$(dirname "$0")" && pwd)
PATCHER=""
for p in "$HERE/patch_vfio.py" /root/patch_vfio.py ./patch_vfio.py; do
    [ -f "$p" ] && { PATCHER=$p; break; }
done
[ -n "$PATCHER" ] || { echo "找不到 patch_vfio.py"; exit 1; }

python3 "$PATCHER" "$ORIG" "$KO.new"
mv "$KO.new" "$KO"
depmod -a "$KVER"

echo "完成：$KO"
echo "原版备份：$ORIG"
echo "重启后生效（或先 modprobe -r vfio_pci 再 insmod 新的）"
