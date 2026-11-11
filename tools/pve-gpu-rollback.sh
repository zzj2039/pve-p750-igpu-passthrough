#!/bin/bash
# 撤销核显直通配置
cp /root/pve-gpu-backup-*/grub /etc/default/grub
rm -f /etc/modprobe.d/vfio-igpu.conf /etc/modprobe.d/blacklist-igpu.conf /etc/modules-load.d/vfio.conf
update-initramfs -u -k all
update-grub
echo "rollback done, reboot to apply"
