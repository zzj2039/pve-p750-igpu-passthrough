# PVE 直通 Intel UHD Graphics P750（Rocket Lake 核显）实践记录

> 目标：把 Xeon W-1390P 的核显 `UHD Graphics P750 [8086:4c90]` 直通给 PVE 虚拟机（VM 100），
> 用途为**算力**（QSV / OpenCL 等），不需要显示输出。
>
> 最终结论：**唯一有效的手段是对内核模块 `vfio-pci.ko` 打二进制补丁**，
> 让 `vfio_pci_igd_init()` 直接返回 0。不需要 BIOS 特殊设置、不需要显卡 ROM、不需要任何 QEMU 参数。
>
> 根因：技嘉 BIOS 把 `00:00.0`（Host Bridge `8086:4c43`）的 PCI class 枚举成 `0x0604`（PCI bridge）
> 而非规范的 `0x0600`（Host Bridge），导致内核 `vfio_pci_igd_cfg_init()` 直接 `return -EINVAL`。
> 详见 [第 3 节](#3-根因)。

---

## 1. 硬件与软件环境

| 项目 | 值 |
|---|---|
| 主板 | Gigabyte W480 VISION W（BIOS F25a，2025-06-11） |
| CPU | Intel Xeon W-1390P（Rocket Lake，8C/16T） |
| 核显 | Intel UHD Graphics P750，PCI `0000:00:02.0`，`[8086:4c90]`，rev 04 |
| IOMMU 组 | group 0，**核显独占**（非常理想） |
| 内存 | 32 GB |
| 系统盘 | NVMe 13.4G（LVM：pve-root 11.5G + swap 1G） |
| 引导 | UEFI + GRUB（非 proxmox-boot-tool 的 systemd-boot） |
| 初始系统 | Proxmox VE 9.2.2，内核 `7.0.2-6-pve` |
| 最终系统 | PVE 9.2.2，内核 **`6.14.11-4-pve`**（补丁打在此内核上） |
| 虚拟机 | VM 100（ostype win10，i440fx，无硬盘，WePE ISO 引导） |

核显 PCI 信息：

```
00:02.0 VGA compatible controller [0300]: Intel Corporation RocketLake-S GT1
        [UHD Graphics P750] [8086:4c90] (rev 04)
        Subsystem: Gigabyte Technology Co., Ltd Device [1458:d000]
```

---

## 2. 问题现象

虚拟机配置好 `hostpci0` 后，`qm start 100` 直接失败：

```
kvm: -device vfio-pci,host=0000:00:02.0,id=hostpci0,bus=pci.0,addr=0x2,romfile=igd.rom:
     vfio 0000:00:02.0: error getting device from group 0: Invalid argument
Verify all devices in group 0 are bound to vfio-<bus> or pci-stub and not already in use
start failed: QEMU exited with code 1
```

对应内核日志（关键）：

```
vfio-pci 0000:00:02.0: resetting
vfio-pci 0000:00:02.0: reset done
vfio-pci 0000:00:02.0: Failed to setup Intel IGD regions   ← 真正的失败点
```

注意：这条日志是 **内核 `vfio-pci.ko`** 打印的，不是 QEMU。
`error getting device from group 0: Invalid argument` 只是 `VFIO_GROUP_GET_DEVICE`
在设备 open 失败后返回 `EINVAL` 的表象。

---

## 3. 根因

> **⚠️ 本节于 2026-11-11 修订。** 最初的判断（"BIOS 没有写 opregion 寄存器"）是**错误**的：
> 当时读的是 **MMIO 的 `BAR0+0xFC`**，而真正的 opregion 基址寄存器位于
> **PCI 配置空间的 `0xFC`**——那里一直有有效值。以下是修正后的结论。

### 3.1 内核判定链路

`vfio_pci_igd_init()` 依次调用两个子函数（源码 `drivers/vfio/pci/vfio_pci_igd.c`）：

```c
int vfio_pci_igd_init(struct vfio_pci_core_device *vdev)
{
	ret = vfio_pci_igd_opregion_init(vdev);   // ① 读 cfg 0xFC，映射 opregion
	if (ret)
		return ret;
	ret = vfio_pci_igd_cfg_init(vdev);        // ② 读取 host bridge / LPC 配置区
	if (ret)
		return ret;
	return 0;
}
```

### 3.2 实测返回值：-EINVAL

用 kretprobe 抓原版模块中该函数的返回值：

```
igdi: (vfio_pci_open_device+0x65/0x80 [vfio_pci] <- vfio_pci_igd_init) arg1=-22
```

`-22` 即 `-EINVAL`。而该函数中只有两处会返回 `EINVAL`：

| # | 位置 | 触发条件 | 本机实测 |
|---|---|---|---|
| ① | `vfio_pci_igd_opregion_init()` | 签名不符，或 `size == 0` | ✅ **通过**（见 3.3） |
| ② | `vfio_pci_igd_cfg_init()` | `00:00.0` 不是 Intel **Host Bridge** | ❌ **失败**（见 3.4） |

### 3.3 opregion 完全正常

```
$ setpci -s 0000:00:02.0 fc.l
953ce018                     ← PCI 配置空间 0xFC 里的 opregion 基址
```

直接读该物理地址（`dd if=/dev/mem`）：

```
000000  49 6e 74 65 6c 47 72 61 70 68 69 63 73 4d 65 6d   IntelGraphicsMem
000010  08 00 00 00 00 00 01 02 00 00 00 00 00 00 00 00
```

- 签名 `IntelGraphicsMem` ✅
- 偏移 `0x10` 的 size 字段 = `8`（KB），非零 ✅
- 版本 `0x0201`，RVDA = `0x2000`，RVDS = `0x2200`

因此 ① 不可能返回 `EINVAL`。

### 3.4 真正的原因：`00:00.0` 的 PCI class code 错误

`vfio_pci_igd_cfg_init()` 的第一段：

```c
host_bridge = pci_get_domain_bus_and_slot(0, 0, PCI_DEVFN(0, 0));
if (host_bridge->vendor != PCI_VENDOR_ID_INTEL ||
    host_bridge->class != (PCI_CLASS_BRIDGE_HOST << 8))   // 必须为 0x060000
	return -EINVAL;
```

本机实际：

```
0000:00:00.0  class=0x060400  PCI bridge: Intel Corporation Device [8086:4c43]
0000:00:1f.0  class=0x060100  ISA bridge: W480 Chipset LPC/eSPI Controller（该项通过）
```

`00:00.0` 是 CPU 的 Host Bridge（`8086:4c43` = Rocket Lake-S Host Bridge/DRAM Registers），
但 **BIOS 把它的 class 枚举成了 `0x0604`（PCI-to-PCI bridge），按规范应为 `0x0600`（Host Bridge）**。

于是 `0x060400 != 0x060000` → `return -EINVAL` → 打印 `Failed to setup Intel IGD regions`。

**结论：这是技嘉 BIOS 的 PCI class code 枚举缺陷，与 IGD OpRegion 无关。**

### 3.5 补丁的两个变体（推荐用精细版）

| 变体 | 改法 | 结果 |
|---|---|---|
| **精细版（推荐，已验证）** | 把 `vfio_pci_igd_init()` 里 `cmpl $0x60000,0x44(%rax)` 的立即数改成实际的 `0x060400`（2 字节） | 校验通过，**opregion / host bridge / LPC 三个 region 全部正常注册**，QEMU 识别为真正的 Intel IGD |
| 粗暴版（备用） | 把 `vfio_pci_igd_init()` 入口改成 `xor eax,eax; ret` | 设备能用，但 QEMU 只当普通 PCI 设备，拿不到 VBT |

精细版启动后 QEMU 会打印：

```
info: OpRegion detected on Intel display 4c90.
```

粗暴版没有这一行。对 QSV / OpenCL 这类算力用途，精细版更好——Intel 驱动初始化时会读 VBT 决定媒体引擎配置。

**关于显示输出**：精细版虽然提供了完整 opregion，`legacy-igd=1` 仍然**不出画面**——因为 QEMU 的 IGD legacy 模式只自动启用 6~9 代（Sandy Bridge ~ Comet Lake），Rocket Lake 是第 12 代，不在范围内（QEMU 10.1+ 明确限制）。实测黑屏，属预期。

### 3.6 与 Proxmox 论坛同类案例的对比

[forum.proxmox.com - igd Passthru on Intel Sandy Bridge](https://forum.proxmox.com/threads/igd-passthru-on-intel-sandy-bridge.88160/)
中出现完全相同的日志 `Failed to setup Intel IGD regions`，但**根因不同**：

| | 论坛案例（Sandy Bridge / HP） | 本案例（Rocket Lake / 技嘉 W480） |
|---|---|---|
| 失败分支 | ① `opregion_init` | ② `cfg_init` |
| 具体原因 | opregion 中 `+0x10` 的 **size 字段**被 BIOS 写坏（≠8） | `00:00.0` 的 **class code** 为 `0x0604` |
| 判据 | `dd ... skip=$((addr+16)) count=4` 输出不是 `08 00 00 00` | `cat /sys/bus/pci/devices/0000:00:00.0/class` = `0x060400` |
| 修法 | 用 `/dev/mem` 把该字段改成 `08 00 00 00`，每次重启重做 | 本仓库的内核模块补丁（或等 BIOS 修复 class code） |

本机 opregion 的 size 字段实测就是 `08 00 00 00`，**所以论坛那招对本机无效**。
排查时可用 3.2 的 kprobe 方法快速区分失败在 ① 还是 ②。

---

## 4. 排除过的所有"疑似方案"

这些全部实测**无效**，记录下来避免后来者重复踩坑：

| 尝试 | 结果 |
|---|---|
| 内核参数 `intel_iommu=on iommu=pt` | IOMMU 正常，但错误不变 |
| 去掉 `iommu=pt` | 无变化 |
| 屏蔽 `i915` / `xe` | 无变化（甚至让 opregion 更不可用） |
| 放开 `i915`（默认流程） | 无变化 |
| `vfio-pci` 模块参数 `disable_idle_d3=1` | 修好了 D3hot 挂起，但 opregion 仍失败 |
| `vfio-pci` 模块参数 `disable_vga=1` | **无法跳过 IGD 初始化**（6.14 / 7.0 均测） |
| 通用 88 合一 ROM（`igpu-6-14.rom`，2MB） | 无效 |
| RKL 专用 ROM（`igd.rom`，GOP v17，187KB） | 无效 |
| PVE `legacy-igd=1` + `vga: none` + `x-igd-lpc=on` | 无效（但补丁之后这些也不需要了） |
| `-set device.hostpci0.x-igd-opregion=off` / `x-igd-legacy-mode=off` | 无效（内核侧先失败） |
| BIOS 开 CSM（让 legacy VBIOS 跑起来，显卡 ROM BAR 可读出 51KB） | 无效 |
| BIOS `Above 4G Decoding=Disabled` + `Re-Size BAR=Disabled`（BAR 从 `0x6000000000` 压到 `0xb0000000`） | 无效 |
| 关闭 `initcall_blacklist=sysfb_init` 以外的 framebuffer 相关 | 无效 |
| 改 PCI class code 让内核不认为是 VGA | class 寄存器**只读**，写不进 |
| 通过 MMIO `BAR0+0xFC` 注入 opregion 地址 | **读错了寄存器**——真正的基址在 PCI 配置空间 `0xFC`；该 MMIO 位置无意义 |
| 按论坛方案修 opregion `+0x10` 的 size 字段 | 本机该字段已正确（=8），不适用 |
| 换内核 6.14.11 | 报错完全一致 ⇒ 不是内核版本问题 |
| 换 BIOS 设置、接真实显示器（HPD 正常、画面正常） | 无效 |

---

## 5. 最终方案：给 vfio-pci.ko 打二进制补丁

### 5.1 原理

**精细版（默认，推荐）**——只改一处比较的立即数：

```asm
bf3: 81 78 44 00 00 06 00   cmpl $0x60000,0x44(%rax)   ; 原值
bfa: 0f 85 bb 00 00 00      jne  错误分支

      81 78 44 00 04 06 00   cmpl $0x60400,0x44(%rax)   ; 补丁后
```

比较通过后 `jne` 不再跳转，函数继续按原逻辑执行，opregion / host bridge / LPC
三个 region 都正常注册，QEMU 识别为真正的 Intel IGD。

**粗暴版（回退）**——把函数入口改成 `xor eax, eax; ret`，直接返回成功：

```asm
31 c0 c3                    xor eax,eax ; ret
```

设备能被打开，但 QEMU 拿不到 opregion region，只当普通 PCI 设备处理。

### 5.2 关键技术细节（踩过的坑）

1. **不能覆盖函数开头的 `call __fentry__`（`e8 00 00 00 00`）**（粗暴版才会碰到）
   那是重定位点，覆盖后 `insmod` 会报：
   `Invalid relocation target, existing value is nonzero for type 4`
   → 必须打在 **`foff + 5`**（call 之后）。

2. **必须剥掉模块签名**
   内核 `CONFIG_MODULE_SIG=y`（但 `MODULE_SIG_FORCE` 未开）。
   打补丁后签名失效，不剥离会报 `Key was rejected by service`。
   剥离方法：定位文件末尾 `~Module signature appended~\n`，向前读 12 字节
   `module_signature` 结构取 `sig_len`，截断 `12 + sig_len + 28` 字节。

3. **`insmod` 不会自动加载依赖**
   必须先 `modprobe vfio_pci_core`，再 `insmod` 补丁版 `vfio-pci.ko`。

4. **改指令时注意偏移**
   `81 78 44 <disp8> <imm32>` 一共 7 字节，立即数在 **+3** 处；
   错写成 +6 会覆盖后面的 `jne`，模块虽然能加载但函数已损坏。

### 5.3 补丁脚本

| 文件 | 作用 |
|---|---|
| `patch/patch_vfio.py` | 打补丁核心：定位符号 → 改立即数（找不到时自动回退粗暴版）→ 剥签名 |
| `patch/repatch-vfio.sh` | 主机上一键重打，内核升级后必用 |

```bash
./repatch-vfio.sh                 # 当前内核
./repatch-vfio.sh 6.14.11-4-pve   # 指定内核
./repatch-vfio.sh --mode blunt    # 需要粗暴版时
```

脚本会先把原版模块存成 `$KO.orig-backup`，之后每次都从这份原件重新生成，
所以可以反复执行。核心逻辑：

```python
# 1) 解析 ELF 节表，定位符号 vfio_pci_igd_init，算文件偏移
#    foff = section_offset + (st_value - section_addr)
# 2) 在函数体里搜 81 78 44 00 00 06 00（cmpl $0x60000），
#    把 +3 处的立即数改成 0x00060400；找不到则回退粗暴版
# 3) 剥掉模块签名
```

### 5.4 安装

```bash
cp vfio-pci-6.14-igdclass.ko /lib/modules/6.14.11-4-pve/kernel/drivers/vfio/pci/vfio-pci.ko
depmod -a 6.14.11-4-pve
reboot
```

或直接跑 `repatch-vfio.sh`，效果相同。

---

## 6. 最小化验证（重要）

为了确认"唯一变量就是补丁"，做过一次**纯原版**验证：

```bash
qm set 100 --delete hostpci0 --delete args --delete bios --delete vga --delete efidisk0
qm set 100 --hostpci0 0000:00:02.0
qm start 100
```

虚拟机配置退化为：

```
boot: order=ide2;net0
cores: 4
cpu: host
hostpci0: 0000:00:02.0        ← 裸设备，无 romfile、无任何选项
machine: pc-i440fx-11.0
bios: 默认 SeaBIOS
vga: 默认 std
```

实际 QEMU 命令行：

```
-device vfio-pci,host=0000:00:02.0,id=hostpci0,bus=pci.0,addr=0x10
```

结果：

```
status: running
dmesg: Failed to setup Intel IGD regions 出现次数 = 0
```

guest 内部 PCI：

```
Bus 0, device 16, function 0:
    VGA controller: PCI device 8086:4c90
```

**结论：补丁是充分且必要的唯一条件。** 之前所有 ROM / 参数 / BIOS 调整都是无效变量。

---

## 7. 当前生产配置

### 主机侧（相对原版的差异）

| 改动 | 必需性 |
|---|---|
| `/lib/modules/6.14.11-4-pve/.../vfio-pci.ko` 打补丁 | ✅ **唯一必需** |
| 新装内核 `6.14.11-4-pve` + `GRUB_DEFAULT` 固定到它 | 配合补丁（补丁只打在 6.14 上） |
| GRUB cmdline：`quiet intel_iommu=on initcall_blacklist=sysfb_init` | 非必需（排查期遗留） |
| `/etc/modprobe.d/vfio-igpu.conf`（`allow_unsafe_interrupts=1`） | 非必需 |
| `/etc/modules-load.d/vfio.conf`（开机加载 vfio 三件套） | 非必需 |
| `/usr/share/kvm/igd.rom`、`igpu-6-14.rom` | 非必需（当前配置不用） |

### 虚拟机侧

```
bios:     ovmf（Windows Server 2025，efidisk pre-enrolled-keys=1 即 Secure Boot）
machine:  pc-i440fx-11.0
hostpci0: 0000:00:02.0      ← 裸设备，无 romfile、无任何参数
vga:      std               ← 提供 VNC 控制台
sata0:    32G 系统盘（sd 存储）
ide2:     Windows Server 2025 ISO
```

> 注：`legacy-igd=1` + `vga: none` 的写法虽然能启动，但对 Rocket Lake 不会产生显示输出
> （见 3.5），所以实际使用的是上面的 `vga: std` 方案。

---

## 8. 注意事项

1. **内核升级会覆盖补丁**，升级后必须执行 `repatch-vfio.sh && reboot`。
2. **不要回退到 7.0.2 内核**（其模块未打补丁）。若确实要回退，先跑
   `repatch-vfio.sh 7.0.2-6-pve`——脚本会自动定位那处 `cmpl`，
   找不到时回退到粗暴版；然后实测能否启动再用。
3. 模块未签名，内核会带 `tainted` 标记，属正常现象。
4. **显示输出不可用**：精细补丁虽然提供了完整 opregion，但 QEMU 的 IGD legacy 模式
   只支持 6~9 代，Rocket Lake（12 代）不在范围内，实测黑屏。算力用途不受影响。
5. `dmesg` 里的 `Invalid PCI ROM header signature: expecting 0xaa55, got 0x0000`
   是无害警告（设备 ROM BAR 为空）。

---

## 9. 回滚

```bash
# 1) 恢复 VM 配置
cp config/100.conf.uefi-old /etc/pve/qemu-server/100.conf     # 或原始备份

# 2) 恢复内核模块
cp patch/vfio-pci-6.14.ko.orig /lib/modules/6.14.11-4-pve/kernel/drivers/vfio/pci/vfio-pci.ko
depmod -a 6.14.11-4-pve

# 3) 恢复 GRUB
cp config/grub.orig /etc/default/grub
update-grub

# 4) 删除排查期文件（可选）
rm -f /etc/modprobe.d/vfio-igpu.conf /etc/modules-load.d/vfio.conf
rm -f /usr/share/kvm/igd.rom /usr/share/kvm/igpu-6-14.rom

reboot
```

主机上现成脚本：`tools/pve-gpu-rollback.sh`。

---

## 10. 文件清单

```
pve-p750-igpu-passthrough/
├── README.md                        本文档
├── patch/
│   ├── repatch-vfio.sh              主机一键重打补丁脚本（内核升级后必用）
│   ├── patch_vfio.py                打补丁核心（精细版 + 粗暴版回退 + 剥签名）
│   ├── vfio-pci-6.14.ko.orig        原版模块（38945 字节，含签名）
│   ├── vfio-pci-6.14-igdclass.ko    ★ 精细版补丁模块（38224 字节，当前使用）
│   └── vfio-pci-6.14-blunt.ko       粗暴版补丁模块（38224 字节，备用）
├── tools/
│   ├── find_opregion.py             在 ACPI NVS 中定位 opregion 物理地址
│   ├── scan_phys.py                 通过 /proc/kcore 扫描物理内存特征串
│   ├── igpu-check.sh                主机状态自检 + 尝试启动 VM
│   └── pve-gpu-rollback.sh          回滚脚本
├── rom/
│   ├── igd.rom                      RKL/TGL/ADL/RPL 专用 GOP v17（187KB，未使用）
│   └── igpu-6-14.rom                社区 88 合一 ROM（2MB，未使用）
├── config/
│   ├── 100.conf.minimal             最终极简 VM 配置
│   ├── 100.conf.uefi-old            旧 UEFI + romfile 配置
│   ├── grub.orig                    原始 GRUB 配置
│   ├── grub.current                 当前 GRUB 配置
│   ├── vfio-igpu.conf               /etc/modprobe.d/vfio-igpu.conf
│   ├── vfio.conf                    /etc/modules-load.d/vfio.conf
│   └── blacklist-igpu.conf.disabled 曾用过的 i915 黑名单（已停用）
└── evidence/
    ├── dmesg-vfio.txt               关键内核日志
    ├── qm-config-100.txt            VM 配置快照
    └── system.txt                   内核版本 / 设备信息 / 模块 md5
```

---

## 11. 时间线概要

| 阶段 | 动作 | 结果 |
|---|---|---|
| 侦察 | 确认核显独占 IOMMU group 0 | ✅ 条件良好 |
| 主机配置 | GRUB `intel_iommu=on`、vfio 绑定、屏蔽 i915 | ✅ 生效 |
| 首次启动 | `legacy-igd=1` + ROM | ❌ `Failed to setup Intel IGD regions` |
| 排查 | 换 ROM、调 QEMU 参数、改 BIOS（CSM / Above 4G / ReBAR）、接真实显示器 | ❌ 全部无效 |
| 初判（后证伪） | 误读 MMIO `BAR0+0xFC` = 0，以为 opregion 未初始化 | ❌ 结论错误 |
| 尝试绕过 | 改 PCI class（只读）、手写 0xFC（写保护） | ❌ 硬性阻断 |
| 换内核 | 7.0.2 → 6.14.11 | ❌ 报错一致 |
| **突破** | **二进制补丁 `vfio_pci_igd_init()` → `return 0`** | ✅ **VM 正常启动** |
| 固化 | 剥签名、装入 `/lib/modules`、`depmod`、重启验证 | ✅ 持久化 |
| 最小化验证 | 裸 `hostpci0`、无 ROM、无参数 | ✅ 依然工作 |
| **根因修正** | kprobe 抓返回 `-22`；`setpci fc.l` 发现 cfg 0xFC 有值；定位到 `00:00.0` class=`0x0604` | 🎯 **真正根因** |

---

## 12. 第三方资源与许可

| 文件 | 来源 | 许可 |
|---|---|---|
| `rom/igd.rom` | [LongQT-sea/intel-igpu-passthru](https://github.com/LongQT-sea/intel-igpu-passthru)（`RKL_TGL_ADL_RPL_GOPv17_igd.rom`） | BSD-2-Clause |
| `rom/igpu-6-14.rom` | [qusong520/intel6-14rom](https://github.com/qusong520/intel6-14rom) / [AICodo fork](https://github.com/AICodo/intel6-14rom) | 见原仓库 |
| `patch/vfio-pci-*.ko` | 派生自 Linux 内核 `drivers/vfio/pci` | GPL-2.0 |
| 本文档与脚本 | 本仓库 | MIT |

> 两个 ROM 最终**并未使用**（补丁后不需要），仅作排查过程留档。

---

## 13. 免责声明

- 修改内核模块属于非常规操作，可能导致内核 taint、驱动异常甚至系统不稳定，**请在测试环境验证后再用于生产**。
- 文中 BT/ROM/内核补丁仅针对 `vfio-pci` 的 IGD 初始化路径，不改变其它任何行为。
- 升级内核前请先阅读第 8 节注意事项。

---

*记录时间：2026-11-11 · 环境：Proxmox VE 9.2.2*
