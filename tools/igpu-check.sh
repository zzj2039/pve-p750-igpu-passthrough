#!/bin/bash
echo "== 核显驱动 =="; readlink /sys/bus/pci/devices/0000:00:02.0/driver
echo "== IOMMU 组 =="; cat /sys/kernel/iommu_groups/0/type
echo "== 显示器连接状态 =="
modprobe i915 2>/dev/null; echo 0000:00:02.0 > /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null
echo 0000:00:02.0 > /sys/bus/pci/drivers/i915/bind 2>/dev/null; sleep 3
for d in /sys/class/drm/card*-*; do [ -f "$d/status" ] && echo "  $(basename $d): $(cat $d/status)"; done
echo 0000:00:02.0 > /sys/bus/pci/drivers/i915/unbind 2>/dev/null
echo 0000:00:02.0 > /sys/bus/pci/drivers/vfio-pci/bind 2>/dev/null
echo "== 尝试启动 VM 100 =="
qm start 100 && sleep 6 && qm status 100
