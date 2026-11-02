# RDMA 原理

本文是 RDMA 的**原理性介绍**：它是什么、一块网卡为什么有两套接口、由哪些对象组成、一次操作怎么走、为什么要注册内存、传输类型、GPUDirect RDMA、流量类别与交换机队列。内容是通用知识（公开资料的一般说法），**不依赖本仓库的环境**；正文不放本文自己的实测数据，少数地方会引用本仓库其他文章的实测结论，并标明出处。

- 想看本仓库的实验、实测结论、环境与上手步骤：见 [README.md](README.md)。
- 文中标 *（实验见 …）* 的地方，指向仓库里对应的实测文章。
- 个别论断本仓库未验证，文中会明确标出。

## 目录

1. [概览：RDMA 是什么](#1)
2. [一块网卡，两套接口：以太网与 RDMA](#2)
3. [对象与队列模型](#3)
4. [内存注册与固定页面](#4)
5. [建立连接](#5)
6. [传输类型与可靠性：RC、UC、UD](#6)
7. [SEND / WRITE / READ 对比](#7)
8. [GPU 显存的 RDMA（GPUDirect RDMA）](#8)
9. [流量类别与交换机队列](#9)
10. [与单机 GPU P2P 的概念对照](#10)
11. [术语速查](#11)

<a id="1"></a>
## 1. 概览：RDMA 是什么

**RDMA**（Remote Direct Memory Access）：网卡直接读写对端内存，**对端 CPU 不参与数据搬运**（单边操作）。它带来三个特性：

| 特性 | 含义 |
|---|---|
| **内核旁路** | 应用在用户态直接向网卡提交任务，数据不经过内核网络栈 |
| **零拷贝** | 网卡在应用内存与网络之间直接 DMA，不在内核缓冲区中转 |
| **协议卸载** | 分包、确认、重传等传输协议由网卡硬件完成 |

结果是低延迟、高吞吐、低 CPU 占用。

**"内核旁路"具体指什么**：建连、注册内存、创建 QP / CQ 等**控制操作**要经过内核（用户态库通过 `/dev/infiniband/uverbsN` 发 ioctl）；但 QP 建好之后，**发起一次传输和取完成项这两步通常没有系统调用**——提交 WQE 靠写一个映射进用户态的网卡门铃寄存器页（doorbell），取完成项靠读映射进用户态的 CQ 内存。*（本仓库用 `strace` 实测过：发起 WRITE / READ 与轮询完成项期间 `RDMA_VERBS_IOCTL` 为 0 次，见 [py-multi-node-cpu.md §1.4](py-multi-node-cpu.md#1)；"映射页就是门铃页"这一用途是推断，没有读驱动代码验证。）*

**承载网络**：RDMA 与底层网络技术无关，可运行在 **InfiniBand、RoCE、iWARP** 之上。**RoCE v2** 把 RDMA 封装在 UDP/IP 里（目的端口 4791），可以跨三层转发，是以太网数据中心里最常见的形态。**Soft-RoCE（RXE）** 用 CPU 在内核里模拟 RDMA，适合学习与功能测试，性能远低于硬件 RoCE。

**一次通信的整体轮廓**：

```
 控制面（建连）                                数据面（传数据）
 应用 ─ 经 netdev 取得 IP / GID / 路由        应用 ─ verbs 直接向网卡提交 WQE
      ─ 交换 QP 号、地址、rkey                      网卡 DMA ⇄ 网络 ⇄ 对端网卡 DMA
 （借助以太网接口 / TCP / rdma_cm）             内核旁路，对端 CPU 不参与
```

<a id="2"></a>
## 2. 一块网卡，两套接口：以太网与 RDMA

一块 RDMA 网卡（如 ConnectX）对 Linux 暴露**两套并列的接口**，对应**同一块物理网卡、同一个物理端口**。

> 本章部分内容（为什么拆成两个子系统、控制面与数据面、容器为什么两者都要）参考了仓库里的 [understanding_RoCE.MD](understanding_RoCE.MD)（DOKS worker 的 RDMA / RoCE 网络说明）。**那篇是说明性文档，其中的论断本仓库未逐项验证**；有本仓库实测佐证的会注明。

### 2.1 两套接口对照

| | 以太网接口（netdev） | RDMA 接口（verbs） |
|---|---|---|
| Linux 里的名字 | `eth0`、`fabric0`（`ip link` 可见） | `mlx5_0`（`ibv_devices`、`rdma link`、`/sys/class/infiniband/`） |
| 设备节点 | 无（走 socket） | `/dev/infiniband/uverbsN` |
| 用户态 API | socket（TCP / UDP）、`ip`、`ethtool` | `libibverbs`（PD / QP / CQ / MR）、`rdma_cm` |
| 内核子系统 | 网络子系统（netdev） | RDMA 子系统（`ib_core` + 网卡驱动如 `mlx5_ib`） |
| 编程模型 | **面向报文**：`send()` / `recv()` / `connect()` | **面向硬件资源**：`ibv_create_qp()` / `ibv_reg_mr()` / `ibv_post_send()` |
| 数据路径 | 应用 → 内核 TCP/IP 栈 → 驱动 → 网卡 | 应用 → 网卡（用户态直接提交，内核旁路） |
| 擅长 | 通用 IP 网络：HTTP、SSH、ping、Kubernetes 网络、监控、路由、VLAN、防火墙 | 零拷贝、硬件卸载、低延迟、高吞吐 |

### 2.2 为什么 Linux 把它们拆成两个子系统

这是**有意的设计**，反映的是 Linux 内核架构，而不是硬件的限制：

```
              Physical ConnectX NIC（同一块物理网卡）
                      │
      ┌───────────────┴───────────────┐
      │                               │
 Network Device                  RDMA Device
 fabric0 … / eth0                /dev/infiniband/uverbsN
      │                               │
 Linux 网络子系统（netdev）       RDMA 子系统（ib_core）
```

socket 是面向报文的模型，QP / MR / CQ 是面向硬件资源的模型；把后者塞进 socket API，要扩展一批本来就不属于网络子系统的概念。所以 Linux 让两个子系统各自独立、**共享同一块物理网卡**，同一块硬件上既能跑传统以太网应用，也能跑 RDMA 应用。**一个接口无法替代另一个。**

### 2.3 它们怎么相互作用

**依赖方向**：RDMA 依附 netdev（链路、地址、邻居解析来自它）；**数据方向**：RDMA 数据绕开 netdev。

1. **绑定**：每个 RDMA 设备都对应一个 netdev，可用 `rdma link` 查看（如 `link rxe_eth1/1 ... netdev eth1`）。链路状态、MAC、MTU 等**物理层属性来自 netdev**：`ip link set eth1 down` 会让对应 RDMA 端口不可用。硬件 RoCE 网卡是"一个物理端口、两个软件表示"；Soft-RoCE 则是 `rdma link add rxe_eth1 type rxe netdev eth1` 人为把 RDMA 设备挂到已有 netdev 上。 *（实验见 [soft-roce.MD](soft-roce.MD)）*
2. **地址**：RoCE v2 用 GID 寻址，**GID 表由 netdev 的 IP 地址生成**。Soft-RoCE 里 `eth1` 配 `10.128.16.11`，GID 表就出现 `::ffff:10.128.16.11`，另有基于 MAC 的链路本地项 `fe80::…`。因此改 netdev 的 IP，GID 随之变化；对端用旧 GID 建连会失败。链路本地（`fe80::`）的 GID 不能跨机路由，要选 ULA 或 IPv4 映射那一项。 *（实验见 [soft-roce.MD](soft-roce.MD)、[c-multi-node-cpu.md §2.1](c-multi-node-cpu.md#2-1)）*
3. **二层 / 三层解析**（公开资料，本仓库未单独验证）：目的 IP → 目的 MAC 的邻居解析（ARP / NDP）与路由选择一般由内核 netdev 栈完成，结果交给 RDMA 驱动。所以 RoCE 不是完全独立于以太网：地址解析、路由、链路状态依赖 netdev，**只有数据收发绕过内核栈**。
4. **控制面与数据面**：
   - **控制面（建连时）依赖以太网接口**：从 netdev 一侧取得 IP 地址、GID、路由、邻居发现、MTU、VLAN、QoS 等通信所必需的网络信息；
   - **数据面（连接建好后）绕开内核协议栈**：QP 建好之后，数据由网卡 RDMA 引擎直接收发，不再经过 TCP/IP 栈。
5. **报文分流**：网卡收到以太网帧后在硬件里按规则分流——RoCE v2 报文（UDP 目的端口 4791）交给 **RDMA 引擎**，直接 DMA 到已注册的 MR，**不产生 netdev 的收发统计**；其他报文（TCP、ARP 等）交给 netdev。本仓库有直接证据：一次运行里约 1.3 GiB 的 RDMA 数据完整出现在 `ethtool -S` 的 RDMA vport 计数器里，而 `fabric` 内核网口的 `tx_bytes` 为 0。 *（实测，见 [c-multi-node-cpu.md §5.5](c-multi-node-cpu.md#5-5)）*
6. **Soft-RoCE 不同**：RXE 用内核软件实现 RDMA，报文由内核经 UDP socket 走 netdev 发出，因此**占用 CPU、也走 netdev 路径**，这正是它性能远低于硬件 RoCE 的原因。

### 2.4 共享什么、不共享什么

| | 是否共享 | 说明 |
|---|---|---|
| 物理端口与线路带宽 | **共享** | 两套接口流量一起占用同一条链路 |
| MAC、MTU、链路状态 | **共享** | 来自 netdev；RoCE 的 `active_mtu`（如 4096）要小于 netdev MTU 扣除头开销后的值，交换机 MTU 要更大 |
| IP 地址 | **共享（单向派生）** | RDMA 的 GID 由 netdev IP 生成 |
| 交换机队列 / QoS | **同一套硬件，靠 DSCP 分队列** | RDMA 流量用 `traffic_class` 标 DSCP，与 TCP 一起进交换机，靠独立队列隔离（§9） |
| 协议栈、socket、`rdma_cm` 连接 | 不共享 | 内核栈处理 TCP/IP；RDMA 协议由网卡硬件处理 |
| 流量统计 | 不共享 | netdev 计数器看不到 RDMA 数据，要看 `ethtool -S` 里的 RDMA 专用计数器 |
| 资源空间 | 互不影响 | RDMA 的 QP 与 TCP 的 socket 是两个独立的资源空间，UDP 4791 由 RDMA 引擎占用 |

另一点（公开资料，本仓库未验证）：netdev 上的 `tcpdump` 一般抓不到被硬件旁路的 RDMA 报文，需要网卡镜像 / 专用抓包工具或交换机侧镜像。

### 2.5 为什么 Pod / 容器里两者都需要

| 资源 | 提供什么 | 没有它会怎样 |
|---|---|---|
| **RDMA 设备**（`/dev/infiniband/uverbsN`） | verbs 接口：创建 QP / CQ、注册 MR、发起 RDMA READ / WRITE | 无法做任何 RDMA 操作 |
| **以太网接口**（`fabric0` 等） | RoCE v2 所需的网络上下文：IP、MTU、VLAN、QoS、路由、GID 解析、对端发现 | 即使有 RDMA 硬件，也无法确定怎样到达对端（DOKS 文档的说法） |

本仓库的佐证与边界：

- **实测**：Pod 里能看到偶数号的 `uverbs` 设备，能用 `ethtool -S fabric0` 读对应网口的计数器、用 `ping6` 经 fabric 网口互通，且 RDMA 设备与网口一一对应（`mlx5_0` ↔ `fabric0`、`mlx5_14` ↔ `fabric14`）。 *（见 [c-multi-node.md §2.1](c-multi-node.md#2-1)、[c-multi-node-cpu.md §1.3](c-multi-node-cpu.md#1-3)）*
- 本仓库的程序用手工 OOB 建连，还需要一个能互通的 `eth0`，所以实际用到**三类**东西：`uverbs` 设备、fabric 网口（RoCE 地址与路由）、`eth0`（OOB）。
- **未测**："去掉 Pod 里的 fabric 网口后 RDMA 是否一定失败"——本仓库没做过这个对照。
- **命名差异**：DOKS 文档里 8 个 NIC 对应 `fabric0–fabric7`；本集群 Pod 里是 `mlx5_0/2/…/14` 与对应的 `fabric0/2/…/14`，只有偶数号可用。以 `ibv_devices`、`rdma link`、`nvidia-smi topo -m` 的实际输出为准。

### 2.6 端到端分层图与排错分层

```
                  应用层
┌──────────────────────────────────────────────────┐
│  RDMA 应用（vLLM、NCCL、MPI 等） /  普通网络应用   │
└──────────────────────────────────────────────────┘
          │ libibverbs                │ socket API
          ▼                           ▼
                  操作系统
┌───────────────────────┐   ┌──────────────────────┐
│ RDMA 子系统           │   │ Linux 网络子系统      │
│ /dev/infiniband       │   │ fabric0… / eth0       │
│ ib_core + mlx5 驱动   │   │ TCP/IP、路由、邻居、   │
│                       │   │ VLAN、QoS、防火墙      │
└───────────┬───────────┘   └───────────┬──────────┘
            └─────────────┬─────────────┘
                          ▼
┌──────────────────────────────────────────────────┐
│   ConnectX RDMA / RoCE 网卡（RoCE v2 over IP）    │
└──────────────────────────────────────────────────┘
                          ▼
                    Ethernet fabric
```

**排错时分层看**：链路与 IP 用 `ip` / `ethtool`；RDMA 设备、GID、端口状态用 `rdma link` / `ibv_devinfo`；RDMA 流量用 `ethtool -S` 的 RDMA 计数器，不要只看 netdev 统计。

**用户态软件栈**（上图左边"RDMA 子系统"之上，再展开一层）：

```
 应用层库（按用途选）
   NCCL / MPI / UCX                    集合通信、消息传递
   MTE（Mooncake Transfer Engine）      点对点大块搬运，带拓扑感知、多网卡、容错（见 mte.md）
   pyverbs                              rdma-core 的 Python 绑定，薄封装，适合学习与验证（见 py-multi-node-cpu.md）
        │
   libibverbs（verbs API）、librdmacm（连接管理）          ← rdma-core 提供
        │  配合厂商 provider（如 libmlx5：网卡的用户态驱动）
 ────── 用户态 / 内核边界 ──────
   /dev/infiniband/uverbsN     控制操作（ioctl）、映射门铃页
   /dev/infiniband/rdma_cm     连接管理
   NETLINK_RDMA 套接字          设备发现、查询
        │
   内核：ib_uverbs → ib_core → mlx5_ib → mlx5_core → 网卡
```

- **provider** 是 libibverbs 与具体网卡之间的一层：verbs API 是通用的，每家网卡（mlx5、bnxt_re、rxe 等）由各自的 provider 库实现。
- 有两种用法：libibverbs 在打开设备时**动态加载** provider 插件（本仓库实测 pyverbs 是这样，它读 `/etc/libibverbs.d/*.driver` 并逐个加载插件）；也可以像 MTE 那样**直接链接** `libmlx5`（本仓库实测）。
- libibverbs 发现设备靠 netlink 和对 `/dev/infiniband/uverbsN` 的 `stat`，本仓库实测的那个版本（rdma-core 39）不读 `/sys`；其他版本可能不同。
- 这些库来自 rdma-core 包；容器里只要有这些**用户态**库和设备节点，内核驱动与固件在宿主机，不需要在容器里装。*（实测见 [py-multi-node-cpu.md §1](py-multi-node-cpu.md#1)、[mte.md §2.4](mte.md#2-4)）*

### 2.7 VRF：每块网卡有独立路由表时

数据中心里常给每块 RDMA 网卡配一个独立的 **VRF**（Virtual Routing and Forwarding）。本节是通用说明（公开资料的一般说法）；本仓库集群上的实测见 [py-multi-node-cpu.md §6](py-multi-node-cpu.md#6)。

**VRF 是什么**：Linux 里的一种三层隔离。创建一个 VRF 设备（如 `vrf100`），把网口（如 `fabric0`）挂到它下面（`master vrf100`），这个网口的路由就改由该 VRF **自己的路由表**（如表 100）管理。`ip rule` 里有一条 `lookup [l3mdev-table]`，让属于某个 VRF 的流量去查对应的表。**主路由表里可以完全没有这些网口的网段路由。**

**为什么给 RDMA 网卡用**（常见动机，**推测**）：多块网卡各接不同的 rail（独立的叶交换机），每块网卡一张独立的路由表，从哪块网卡出去的流量就只走它那张表的网关，互不串线；也避免多块网卡配了同一个大网段（如 `/24`）时路由互相冲突。

**对 RDMA 的影响**：

| 方面 | 影响 |
|---|---|
| **数据面**（QP 建好后的收发） | 无影响：报文由网卡硬件直接收发，不再查内核路由 |
| **GID** | 仍然由网口的 IP 生成（§2.3），VRF 不改变这一点 |
| **按 IP 寻址 / 路由解析**（`rdma_cm`、socket、`ping`） | **必须告诉内核出接口或源地址**：主表里查不到路由，不指定就 `Network is unreachable`；指定了源地址 / 出接口，内核才会去该网口所在 VRF 的表里查 |
| **下一跳** | 通常是**网关**（交换机）而不是对端主机：目的 MAC 一般是网关的 MAC，邻居表里也只有网关 |
| **普通网络工具** | `ping -I fabricN`、`ip vrf exec vrfNNN <命令>` 或 `SO_BINDTODEVICE`；默认网络（如 `eth0`）不受影响 |

**应用怎么应对**：

- **`rdma_cm`**：把端点绑定到该 VRF 内网口的 IP——server 用监听地址，client 显式给源地址（本仓库的 `--src`），否则路由解析失败；
- **手工建连（OOB）**：不靠 IP 找路由，用设备名与 GID 下标选定网卡与源 GID，GID 属于哪个网口，出接口就确定了（内核如何据此解析下一跳，本仓库**未读内核代码验证**）；
- **OOB 的 TCP** 用不在 VRF 里的网口（如 `eth0`）即可，不受影响。

**排错**：`ip -d link show type vrf`（有哪些 VRF）、`ip -6 route show table <N>`（某个 VRF 的表）、`ip -6 route get <IP> oif <网口>`（指定出接口后的选路）、`ip -6 neigh show dev <网口>`（下一跳的 MAC）。

<a id="3"></a>
## 3. 对象与队列模型

### 3.1 对象

| 对象 | 含义 |
|---|---|
| **PD**（Protection Domain） | 保护域，资源隔离与管理单元。一个 PD 可含多个 MR 和 QP；同一 PD 且权限匹配的资源之间才能安全访问 |
| **MR**（Memory Region） | 注册给网卡的一块内存，得到 `lkey`（本端用）与 `rkey`（给对端用）。注册详见 §4 |
| **QP**（Queue Pair） | 通信的基本端点，由 **SQ**（Send Queue）与 **RQ**（Receive Queue）组成 |
| **WQE / WR** | 提交给 SQ / RQ 的工作项（Work Queue Element，verbs 里叫 Work Request）。类型：SEND、WRITE、READ、Atomic |
| **CQ**（Completion Queue） | 完成队列。RNIC 完成 WQE 后写入 **CQE**（verbs 里叫 Work Completion），应用用 `ibv_poll_cq()` 取成功 / 失败信息 |
| **SRQ** | Shared Receive Queue，多个 QP 共用一个接收队列，见 §3.3 |
| **GID** | RoCE 的"地址"，形如 IPv6（§2.3） |
| **RNIC** | 支持 RDMA 的网卡：从 SQ / RQ 取 WQE，执行 DMA、封装 RDMA Packet、网络传输与远端内存访问 |

**一句话流程**：应用注册内存得到 MR → 创建 PD / QP / CQ → 向 SQ / RQ 提交 WQE → RNIC 按 WQE 与 MR 信息执行 DMA 与网络传输 → 完成后写入 CQE → 应用查询完成状态。

### 3.2 一次 RDMA WRITE 的全过程

1. 经 TCP 等带外通道交换 QP 信息，使两个 RNIC 建立可靠传输关系（§5）；
2. 交换远端 MR 的地址与 `rkey`；
3. 发送方向本地 QP 的 SQ 提交 WRITE WQE；RNIC 自动读取 WQE，经本地 DMA Read 取出数据，生成 RDMA WRITE 报文发往远端；
4. 远端 RNIC 校验 `rkey` 后直接 DMA Write 到目标内存；
5. 本端 CQ 出现 CQE，应用用 `ibv_poll_cq()` 取完成状态。

要点：

- **批量与流水**：应用可向 SQ 批量提交多个 WRITE，RNIC 异步执行；逻辑上每个 WQE 独立完成，硬件内部可流水化并行处理。
- **WQE 里是虚拟地址 + key**：RNIC 通过 MR 表把它转换成可 DMA 的内存，完成本地 DMA Read 与远端 DMA Write。
- **队列是本地的，网络上传的是报文**：SQ、RQ 都是本地 RNIC 的工作队列，SQ 中的 WQE 只被本地 RNIC 消费；网络上传输的是 RDMA Packet，不是 WQE。远端 RNIC 按 Packet 类型决定是否消耗远端 RQ（SEND 需要，WRITE / READ 不需要）。
- **大消息会被拆分**：一个 WRITE WQE 可请求几十 MB 甚至 GB 级数据，但 RNIC 内部会拆成多个 PCIe / NVLink DMA transaction（通常几百字节到几 KB），流水线式持续发送 RDMA Packet。

### 3.3 SRQ：多个 QP 共享接收队列

通常**每个 QP 都有自己的 RQ**。要接收 SEND，接收方必须提前 post 好 Receive WQE（预先挂好接收缓冲区）。连接很多时会浪费：例如 1000 个 QP、每个为应对突发至少挂 64 个 4 KiB 缓冲区，就要预留约 256 MiB，而同一时刻只有少数连接在收数据，缓冲区却被绑死在各自的 QP 上。

**SRQ 让多个 QP 共用同一个接收队列**：任何挂在 SRQ 上的 QP 收到 SEND 时，从 SRQ 取下一个空闲的 Receive WQE 使用。

```
  不用 SRQ                          用 SRQ
 QP1 ── RQ1 [缓冲 缓冲 缓冲]        QP1 ─┐
 QP2 ── RQ2 [缓冲 缓冲 缓冲]        QP2 ─┼── SRQ [缓冲 缓冲 缓冲 缓冲 …]（共用）
 QP3 ── RQ3 [缓冲 缓冲 缓冲]        QP3 ─┘
```

- **好处**：缓冲总量按所有连接合计的流量估算，不必按每个 QP 的最坏情况分别预留；谁收到包谁用，利用率高；只需往一个队列补缓冲。
- **用法**：`ibv_create_srq()` 创建；建 QP 时在 `qp_init_attr.srq` 指定，此后该 QP **没有自己的 RQ**；补缓冲用 `ibv_post_srq_recv()`，不再用 `ibv_post_recv()`。
- **识别来源**：完成项里的 `qp_num` 指明是哪个 QP 收到的；缓冲区不绑定 QP，靠 `wr_id` 识别是哪块缓冲。
- **水位告警**：可设 `srq_limit`，缓冲数降到阈值以下触发 `IBV_EVENT_SRQ_LIMIT_REACHED`，提醒应用补缓冲（`ibv_modify_srq`）。
- **被耗尽的后果**：RC 下对端收到 RNR NAK，发送方按 `rnr_retry` 重试；UC / UD 无重试机制，直接丢包（§6）。所以必须及时补充。
- **跨 QP 不保序**。
- **只影响 SEND / RECV**：WRITE、READ 不消耗远端 RQ（§7），SRQ 对它们没有意义。
- **相关概念**：**XRC（eXtended RC）** 让同一节点上的多个进程共享接收资源，把 QP 数从 O(N²) 降下来，使用 XRC SRQ。**共享 CQ**（多个 QP 的完成项放进同一个 CQ）是另一回事，很常见，与 SRQ 无关。

*本仓库的程序只用 WRITE / READ，没有用到 SRQ，也没有相关实测。*

<a id="4"></a>
## 4. 内存注册与固定页面

### 4.1 为什么要注册

网卡做 DMA 时**不认进程的虚拟地址**，只认**总线地址**（物理地址或经 IOMMU 翻译后的 IOVA）。让网卡安全地读写你的内存，要解决三个问题：

| 问题 | 为什么麻烦 | 注册怎么解决 |
|---|---|---|
| 地址翻译 | 进程用虚拟地址，网卡要总线地址，映射由内核维护 | 注册时算出每页的总线地址，写进网卡内部的翻译表（MTT） |
| 页必须固定 | 普通页可被换出、被内核迁移；DMA 期间页一动就会读写错位置 | 注册时把页**钉住**，注销前不会动 |
| 权限 | 不能让对端随便读写你的内存 | 注册时指定访问权限（本地写、远端读、远端写），发 `lkey` / `rkey`，RNIC 凭 key 校验 |

一次 `ibv_reg_mr` 做四件事：**钉页 → 取总线地址 → 建翻译表 → 发 key**。完成后数据路径上网卡凭翻译表直接 DMA，CPU 不再参与。所以**注册是一次性的重开销**：启动时注册一次、反复使用，不要每次传输都注册。

### 4.2 为什么必须固定（pin）页面

**CPU 能处理缺页，RNIC 不能**：CPU 访问被换出的页时，操作系统可把页换回后继续执行；RNIC 发起 DMA 时没有执行上下文，也不能处理中断或缺页，所以 DMA 访问的内存必须一直驻留物理内存——这是 `ibv_reg_mr()` 必须 pin 页面的根本原因。

- **"按页"的含义**：翻译表以页为粒度（默认 4 KiB，用大页可更粗）；传给 `ibv_reg_mr` 的地址与长度不必页对齐（内核向外取整、钉住整页），物理页也不必连续。
- **先触摸再注册**：Linux 物理内存是"第一次碰才分配"，而注册要钉住每一页，所以没碰过的页会在注册这一步被迫缺页分配，成本算进注册耗时。大块内存注册前先写一遍每一页，才能得到干净的注册耗时。 *（实测对照见 [c-multi-node.md §1.2](c-multi-node.md#1-2)）*
- **限制**：被钉住的页不能换出，受 `RLIMIT_MEMLOCK`（`ulimit -l`）约束，拥有 `CAP_IPC_LOCK` 的进程可绕过。
- **收尾顺序**：先 `ibv_dereg_mr` 注销，再释放内存——注销前内核还钉着这些页、网卡翻译表里还记着物理地址。

### 4.3 例外：ODP

**ODP（On-Demand Paging）** 注册时不钉页，网卡访问到没有映射的页时由内核处理缺页再通知网卡，只适用于主机内存。本集群的网卡实测支持 ODP（含隐式 ODP），但本仓库的程序没有使用，行为与性能未测。 *（见 [c-multi-node-cpu.md §3.2](c-multi-node-cpu.md#3-2)）*

### 4.4 虚拟机（SR-IOV）

在 SR-IOV 环境下，RNIC 并不直接访问虚拟机应用的虚拟地址，而是凭 `ibv_reg_mr()` 建立的 MR + DMA 映射发起 DMA，再由 IOMMU 把 DMA 地址转换到宿主机物理内存。**SR-IOV 虚拟机与物理机一样，都必须对 RDMA 注册内存 pin**：`ibv_reg_mr()` 固定 Guest 中的页面、建立 DMA 映射，底层 IOMMU 与 Hypervisor 保证 DMA 能正确访问对应的 Host 物理内存；不 pin 就无法保证 DMA 的正确性与可靠性。

<a id="5"></a>
## 5. 建立连接

### 5.1 "连接"到底指什么

**RC 连接 = 两个 RNIC 上的 QP 互相配置好对方的 `qpn` / `psn` / `gid`，并切到 RTS**，使每个 QP 能唯一识别对端 QP，并维护可靠传输状态（PSN、ACK、重传等）。连接建立后，本地 SQ 中的 WQE 生成 RDMA Packet，远端 RNIC 按 Packet 中的 QPN 把它交付给对应的远端 QP。

- **"连接"是每端本地 QP 里存的状态**，没有哪个函数是"去连对方"。
- **内存授权（`addr` + `rkey`）是另一层**，每次操作时才用，不属于"连接"。
- 一个 RNIC 可同时管理成千上万条这样的连接。

### 5.2 QP 状态机

```
RESET → INIT → RTR（需要对端 qpn / psn / gid） → RTS（需要本端 psn）
```

| 步骤 | 切换 | 需要的信息 | 关键参数 |
|---|---|---|---|
| ① | RESET → INIT | 无 | `pkey_index`、`port_num`、`qp_access_flags`（允许对端远端读写） |
| ② | INIT → RTR | **对端**的 QP 号、PSN、GID | `dest_qp_num`、`rq_psn`、`path_mtu`、`ah_attr`（RoCE 须 `is_global = 1`，带 `dgid`、`sgid_index`、`hop_limit`、`traffic_class`） |
| ③ | RTR → RTS | 本端的 PSN | `sq_psn`，以及 RC 的 `timeout`、`retry_cnt`、`rnr_retry`、`max_rd_atomic` |

RoCE 没有 InfiniBand 的 LID，必须带 GRH、用 GID 寻址。本仓库程序的具体参数见 [c-multi-node-cpu.md §2.4](c-multi-node-cpu.md#2-4)。

### 5.3 两种交换连接信息的方式

| 方式 | 做法 |
|---|---|
| **OOB（带外）** | 先经 TCP 交换 `{qpn, psn, gid, addr, rkey, len}`，再手工 `ibv_modify_qp`。perftest 默认就是这种 |
| **RDMA CM** | 用 `rdma_cm` 管理连接（用 IP 寻址），连接管理有专门的报文，省去手工 OOB |

要点：

- **OOB 上的 TCP 连接不是 RDMA 连接**，它只是临时的信令通道。OOB 只传控制信息（QP 号、GID、地址、rkey，以及"写完了 / 校验结果"之类的同步），数据走 RDMA。 *（实测 OOB 上只有约 1–2 KiB，见 [c-multi-node-cpu.md §2.2](c-multi-node-cpu.md#2-2)）*
- **RDMA CM 的连接管理报文走网卡上专门的管理 QP（GSI，即 QP1）**，数据则走之后创建的 RC QP；两者经过同一块网卡、同一条网络路径，只是用的 QP 不同。OOB 方式的建连信息则走另一张网（如 `eth0` 上的 TCP）。"CM 报文经 GSI QP 发出"是公开资料的说法；*本仓库的实测佐证：跑一次 CM 版，client 与 server 的 GSI QP 发送序号分别增加 3 和 2（两次一致），跑 OOB 版则不变（见 [py-multi-node-cpu.md §5.7](py-multi-node-cpu.md#5)）；每个增量对应哪条 CM 消息没有验证。*
- 注意区分两个"控制面"：**网络层面的**（IP / GID / 路由 / 邻居发现，来自 netdev，§2.3）是 RoCE 本身就需要的；**应用层面的 OOB** 是手工建连时应用自己加的，用 `rdma_cm` 时由 CM 报文代替。
- 本集群里 OOB 与数据甚至走不同的网口：OOB 在 `eth0`，数据在 `fabric0…`。Soft-RoCE 的例子也一样：`ib_send_bw -d rxe_eth1 <eth0 的 IP>` 用公网 `eth0` 交换连接信息，数据走 `rxe_eth1` / `eth1`。 *（见 [soft-roce.MD](soft-roce.MD)）*
- 手工建连时，连接阶段不在 fabric 上发握手包，信息填错要到第一次 WRITE / READ 才以重试耗尽之类的错误暴露（机制说法，推断）。

### 5.4 单边操作对对端"无声"

WRITE 与 READ 都不需要对端做任何事，对端的 CPU 不参与；WRITE 时对端甚至不知道数据已到——所以实际程序要另外经 OOB 通知对端去校验。发起方在 CQ 里忙轮询时 CPU 占用会很高，被动方则几乎空闲。 *（实验见 [c-multi-node-cpu.md §5.6](c-multi-node-cpu.md#5-6)）*

<a id="6"></a>
## 6. 传输类型与可靠性：RC、UC、UD

创建 QP 时要指定**传输类型**（`ibv_qp_type`），它决定 QP 是否面向连接、是否可靠、支持哪些操作。**本仓库的所有实验都用 RC**；UC、UD 没有实测，本章只做通用说明。

### 6.1 RC 的可靠性：丢包由硬件处理

RC 下发送过程中某个 Packet 丢失时，两端 RNIC 利用 **PSN** 检测，由硬件自动完成 ACK / NAK 与重传，**无需 CPU 或应用参与**。一个 WRITE Work Request 通常拆成多个 Packet，**所有数据包都按协议成功传输后**，发送端才会在 CQ 里收到成功的 CQE；超过配置的重传次数仍失败，则 CQE 返回错误状态（如 `IBV_WC_RETRY_EXC_ERR`）。整个丢包检测、重传与可靠性保证对应用透明。

传统的重传方式（go-back-N）会把丢包点之后的包全部重发，吞吐急剧下降；较新的网卡支持选择性重传。所以承载 RDMA 的网络通常要做专门的 QoS（§9）。

### 6.2 两条维度与三种类型

| | 面向连接（一个 QP 对一个对端） | 无连接（一个 QP 可对多个对端） |
|---|---|---|
| **可靠**（有 ACK 与重传） | **RC**（Reliable Connection） | RD（几乎没有实现） |
| **不可靠**（无 ACK、不重传） | **UC**（Unreliable Connection，**不可靠连接**） | **UD**（Unreliable Datagram） |

| | RC | UC | UD |
|---|---|---|---|
| `ibv_qp_type` | `IBV_QPT_RC` | `IBV_QPT_UC` | `IBV_QPT_UD` |
| 连接 | 一对一，要建连 | 一对一，要建连 | **无连接** |
| ACK / 重传 | **有**（硬件完成） | **无** | **无** |
| 保序 | 保序 | 同一连接上按序，但丢包后不恢复 | 不保证 |
| SEND / RECV | 支持 | 支持 | 支持（**仅此一种**） |
| RDMA WRITE | 支持 | **支持** | 不支持 |
| RDMA READ / Atomic | 支持 | **不支持** | 不支持 |
| 最大消息 | 可达 GB 级（受 `max_msg_sz` 限制） | 同 RC | **不超过一个 MTU** |
| 组播 | 不支持 | 不支持 | **支持** |
| QP 数量 | 每个对端一个 | 每个对端一个 | **一个 QP 服务所有对端** |

### 6.3 UC：只管发，不管有没有到

- **没有 ACK、没有重传**：发送方 RNIC 把报文发出就算完成。**发送方的 CQE 只表示"已发出"**，不表示"对端已收到"（RC 的 WRITE 完成项则意味着对端网卡已确认收到）。
- **丢包不会被发现，也不会被恢复**：接收方发现包序不连续时，**会丢弃正在接收的那条消息**（多包消息丢一个包就整条作废），不触发重传，发送方也收不到错误。
- **没有 RC 的可靠性参数**：切到 RTR / RTS 时，UC 不需要 `timeout`、`retry_cnt`、`rnr_retry`、`max_rd_atomic`、`max_dest_rd_atomic` 等；但仍要经过 INIT → RTR → RTS，仍需要对端的 QPN、PSN 与 GID。
- **只能"推"，不能"拉"**：可以 SEND 和 WRITE，**不能 READ、不能 Atomic**——这两种操作要靠对端的响应报文，没有可靠机制就无法保证响应送达。
- **应用要自己检测丢失**：WRITE 对对端本来就"无声"，丢了也无声，所以应在数据里带序号、校验和，或用 WRITE with Immediate / 另一条通道通知。

### 6.4 UD：无连接的数据报

- QP 只需切到 RTS（不需要对端 QPN）；**每次发送时**在 Work Request 里用**地址向量（AH）**指明目的地，并带对端 QPN 与 **Q_Key**。一个 QP 可给任意多个对端发送，**QP 数量不随对端数增长**。
- **消息不能超过一个 MTU**（RoCE 的 `active_mtu` 为 4096 时，载荷最大 4096 字节），大数据要应用自己分片。
- **接收缓冲区要多留 40 字节**：UD 接收时，缓冲区开头 40 字节存放 GRH（公开资料说法，RoCE 同样适用）。
- 支持**组播**；只有 SEND / RECV 一种操作。

### 6.5 为什么用不可靠传输，怎么选

| 动机 | 说明 |
|---|---|
| **QP 可扩展性** | RC 每个对端一个 QP，N 个对端就是 O(N) 个 QP；网卡缓存的 QP 上下文有限，对端很多时性能会下降。UD 一个 QP 服务所有对端 |
| **减少网卡状态与开销** | 没有 ACK 流量，不必保留已发数据等待确认，不必维护重传状态与超时定时器 |
| **应用能容忍或自己处理丢失** | 例如视频流 / 遥测；或应用在上层做可靠性 |
| **需要组播** | 只有 UD 支持 |
| **网络本身很少丢包** | 无损（PFC）网络里丢包很少，可靠性开销显得多余 |

| 需求 | 选 |
|---|---|
| 要 READ / Atomic、要可靠、数据大 | **RC**（本仓库所有实验用的） |
| 一对一，只需 SEND / WRITE，愿自己处理丢失，想去掉 ACK 与重传开销 | **UC** |
| 对端很多、消息很小、要组播，愿自己处理丢失与分片 | **UD** |
| 不确定 | **RC**：可靠、功能最全，也是 NCCL、MPI 等的默认选择 |

UC 在实际中相当少见；UD 常见于对可扩展性要求高的小消息 RPC / 事务系统（公开资料，非本仓库）。

**工具里怎么用（未在本仓库验证）**：verbs 里创建 QP 时把 `qp_init_attr.qp_type` 设为 `IBV_QPT_RC` / `UC` / `UD`；perftest 用 `-c` 选连接类型，不同工具支持的类型不同（如 READ 只能用 RC），`soft-roce` 输出里的 `Connection type : RC` 就是这一项，具体以 `ib_xxx_bw -h` 为准。UC / UD 是否像 RC 一样受 DCQCN 拥塞控制调节，**未确认**。

<a id="7"></a>
## 7. SEND / WRITE / READ 对比

| 项目 | RDMA SEND | RDMA WRITE | RDMA READ |
|---|---|---|---|
| 主要用途 | 消息传递 | 本地主动写远端内存 | 本地主动读远端内存 |
| 谁发起 / 提交位置 | 本地 QP / 本地 SQ | 同左 | 同左 |
| 是否需要远端 RQ | **需要**，远端须预先 post receive | 不需要 | 不需要 |
| 是否需要预先交换 MR 信息 | 不需要（只需接收缓冲区已挂到 RQ） | **需要**（remote_addr + rkey） | **需要**（remote_addr + rkey） |
| 本地 RNIC 动作 | 读 SEND WQE，DMA Read 本地数据，发 SEND Packet | 读 WRITE WQE，DMA Read 本地数据，发 WRITE Packet | 读 READ WQE，发 READ Request |
| 远端 RNIC 动作 | 从 RQ 取 Receive WQE，DMA Write 到接收缓冲区 | 校验 rkey，DMA Write 到指定内存 | 校验 rkey，DMA Read 指定内存，发 READ Response |
| 数据流向 | 本地内存 → 远端 RQ Buffer | 本地内存 → 远端 MR | 远端 MR → 本地内存 |
| 消耗远端 RQ | **是** | 否 | 否 |
| 远端 CPU 参与数据传输 | 否（应用需提前准备 RQ） | 否 | 否 |
| 网络上传输的内容 | SEND Packet | WRITE Packet | READ Request + READ Response |
| 完成通知 | 本地 CQ；远端通常也产生 Receive CQE | 本地 CQ；远端通常**不**产生 CQE | 本地 CQ；远端通常**不**产生 CQE |
| 支持的传输类型 | RC、UC、UD | RC、UC | 仅 RC |
| 常见应用 | MPI、RPC、消息队列 | 参数同步、Checkpoint、GPU Direct RDMA | 拉取远端数据、分布式存储 |

READ 的数据由**被读的一方**发出（READ Response），这一点在流量类别（§9）里很重要。

<a id="8"></a>
## 8. GPU 显存的 RDMA（GPUDirect RDMA）

CPU 内存的注册路径依赖"主机内存页"这个概念（内核知道怎么钉页、取物理地址）。**GPU 显存不是主机内存**，没有对应的主机页描述，内核的 `pin_user_pages` 对它不适用。只有 **NVIDIA 驱动**知道一段显存的每一页对应哪个总线地址（经 **BAR1** 暴露给 PCIe 总线），所以必须由 GPU 驱动把总线地址交给 RDMA 子系统。这就是 **GPUDirect RDMA**，有两种实现：

| | `nvidia_peermem`（legacy） | dmabuf |
|---|---|---|
| 思路 | 向 RDMA 子系统注册一个 peer memory client，由它认出 GPU 指针 | CUDA 把显存导出成 dmabuf fd，用 Linux 内核通用的 dmabuf 框架交给网卡 |
| 注册 API | 与 CPU 内存**同一个** `ibv_reg_mr` | `cuMemGetHandleForAddressRange` + `ibv_reg_dmabuf_mr` |
| 需要什么 | 宿主机加载 `nvidia_peermem`；按 NVIDIA 文档需要 MLNX_OFED | 开放内核模块、内核 ≥ 5.12、CUDA ≥ 11.7；OFED 可选 |
| 用完之后 | `ibv_dereg_mr` | `ibv_dereg_mr`，并自己 `close(fd)` |

- **数据通路相同**：两种方式下网卡最终都是经 PCIe 直接 DMA 访问 GPU 显存（通过 BAR1）。差别在**控制面**——谁把显存"交给"网卡：peermem 是厂商专用接口，dmabuf 是内核通用的标准化框架。所以 dmabuf 的收益是工程上的（更少外部依赖、更好维护、对新型分配方式更友好），不是带宽更高。
- **BAR1**：GPU 暴露给 PCIe 总线的一段**地址窗口**，不只 CPU，网卡和其他 GPU 等能发起 PCIe 访问的设备都能经它访问显存；普通 `cudaMemcpy` 走拷贝引擎 DMA，不经过 BAR1。
- **VMM（CUDA 虚拟内存管理）**：`cuMemCreate` / `cuMemAddressReserve` / `cuMemMap` / `cuMemSetAccess` 把"物理显存"和"虚拟地址"分开管理（缓冲可增长而不改指针、减少碎片）。用 VMM 分配又走 peermem 注册，分配时需设 `gpuDirectRDMACapable = 1`；dmabuf 不挑。NCCL 自 2.19 起默认用 VMM 分配，所以倾向走 dmabuf。
- **同步**：注册前对显存设 `CU_POINTER_ATTRIBUTE_SYNC_MEMOPS`；填完数据要先 `cudaDeviceSynchronize()`，再把地址和 rkey 交给对端。主机内存没有这个问题，CPU 与 DMA 的一致性由硬件和内核保证。
- **GPU 与网卡要就近**（公开资料的一般说法）：网卡经 PCIe 直接访问 GPU 的显存，两者之间的 PCIe 距离会影响 GDR 的路径与带宽。`nvidia-smi topo -m` 用几个缩写表示距离：**PIX**（最多经过一个 PCIe 交换机，最近）、**PXB**（经过多个 PCIe 交换机，但不经过 CPU）、**PHB / NODE**（经过 CPU 的 PCIe 主桥 / 同一 NUMA 节点内）、**SYS**（跨 NUMA 节点，要经过 CPU 之间的互联，最远）。距离越远，传输越可能被 CPU 根复合体或 CPU 互联（UPI / QPI）的带宽限制；所以多网卡、多 GPU 的服务器里，通常让每块 GPU 优先用与它在同一个 PCIe 交换机下的网卡。MTE 的做法是为每种内存类型生成"首选 / 备选网卡"列表（见 [mte.md §1.2](mte.md#1-2)，文档说法）。 *本集群里每块 GPU 与两块网卡同在一个 PCIe 交换机下（`PXB`），实验 2 用的就是这样的就近组合（[mte.md §4.1](mte.md#4-1)）；"不就近会差多少"本仓库没有测。*
- **产业趋势**（公开资料）：NVIDIA 官方推荐 dmabuf，把 `nvidia-peermem` 称为 legacy；NCCL 条件满足时默认走 dmabuf（`NCCL_DMABUF_ENABLE`，自 2.13 起默认 1）；Blackwell 及以后只支持开放内核模块。没有找到 NVIDIA 宣布弃用 peermem 的官方原文。

*（本仓库的实测对比——注册耗时、VMM、BAR1、两种方式的带宽——见 [c-multi-node.md §1](c-multi-node.md#1)）*

<a id="9"></a>
## 9. 流量类别与交换机队列

### 9.1 traffic_class、DSCP 与 ECN

以太网默认"尽力而为、拥塞就丢包"，而 RC 对丢包很敏感，所以承载 RDMA 的网络通常要做专门的 QoS：**给 RDMA 独立的队列、尽量不丢包、拥塞时主动降速**。

一个报文被分到队列的路径：

```
程序：QP 的 traffic_class（IP 头的 Traffic Class 字节 = DSCP 高 6 位 + ECN 低 2 位）
   ▼
网卡：按"信任模式"读 DSCP，映射到一个【优先级】（0～7）
   ▼
交换机：读报文 DSCP，映射到一个【队列】，队列决定缓冲、调度、是否开 PFC、ECN 阈值
```

- **换算**：`traffic_class = DSCP × 4 + ECN`。例如 104 = `0110 1000`，高 6 位 `011010` = **DSCP 26**（AF31），低 2 位 `00`。
- **在哪里设**：verbs 里在 QP 从 INIT 切 RTR 时写入 `ah_attr.grh.traffic_class`（`ibv_create_qp` 没有这个参数）；NCCL 对应 `NCCL_IB_TC`；MTE 对应环境变量 `MC_IB_TC`（官方文档的说法，本仓库没有设置过）；本仓库的裸 verbs 程序对应命令行参数 `-t`。
- **每端各自设置，只管自己发出的包**。因为 READ 的数据是被读方发的，所以按 RoCE 机制**推断**，READ 要**两端都设**，只设一端则 READ 数据仍按 TC 0 走。
- **两端映射必须一致**：网卡的"DSCP → 优先级"与交换机的"DSCP → 队列"必须在整条路径上一致，错一处这一段就落到默认队列。
- ECN 位在启用拥塞控制时如何设置因网卡与配置而异；如果报文不带 ECT 标记，交换机通常无法对它做 ECN 标记。

### 9.2 通用配置思路

| 配置项 | 通用做法 | 作用 |
|---|---|---|
| 分类 | 按 DSCP 分类；网卡信任 DSCP；RDMA 数据和 CNP 各给固定 DSCP | 决定进哪个队列 |
| 独立队列 | RDMA 数据一个队列、CNP 一个队列、其他走默认 | 隔离，互不影响 |
| PFC | **只**在 RDMA 数据所在优先级开 | 缓冲快满时通知上游暂停，避免丢包 |
| ECN + DCQCN | RDMA 队列设 ECN 标记阈值，网卡收到拥塞通知后降速 | 在 PFC 触发前主动限速 |
| 调度 | RDMA 队列用 ETS / WRR 保底带宽；CNP 队列用**严格优先级** | 保证保底带宽，拥塞通知不被延迟 |
| 缓冲 | 无损队列划专门缓冲池，设好 headroom 与 xon / xoff | 吸收 PFC 暂停生效前还在路上的数据 |
| MTU | 全链路一致；交换机用巨帧（RoCE MTU 4096 时常取 9216） | 避免丢弃或分片 |
| PFC 看门狗 | 开启 | 检测到暂停风暴时自动恢复 |
| 负载均衡 | 自适应路由、逐包喷洒，或让多 QP 的 UDP 源端口不同以分散 ECMP 哈希 | 避免大流集中在同一链路 |

**理想状态是 ECN 先触发、PFC 只作最后保险**，所以 ECN 阈值要低于 PFC 的 xoff。另一种思路是不开 PFC，只靠 ECN 与网卡选择性重传：避免队头阻塞、暂停风暴、死锁，代价是丢包时有性能损失，这是设计取舍。

### 9.3 常见约定与常见坑

NVIDIA（Mellanox）公开文档里常见的约定（**不是强制**，具体由网络管理员决定）：

| 流量 | 常见 DSCP | 对应优先级 | 是否开 PFC |
|---|---|---|---|
| RoCE 数据 | 26（`traffic_class = 104`） | 3 | 是 |
| CNP（拥塞通知） | 48 | 6 | 否，严格优先级 |
| 其他（TCP 等） | 0 | 0 | 否 |

| 坑 | 后果 |
|---|---|
| PFC 开得太多 | 暂停风暴、队头阻塞、死锁 |
| ECN 阈值高于 PFC 阈值 | 总是先触发 PFC，ECN 不起作用 |
| headroom 设小了 | PFC 生效前就溢出，仍然丢包 |
| DSCP→队列映射、网卡信任模式不一致 | 某段链路 RDMA 流量落进默认队列，性能不稳 |
| MTU 不匹配 | 大包被丢弃或分片 |
| 哈希极化 | 少数大流挤在同一条链路 |
| CNP 没走高优先级 | 降速太迟，拥塞扩大 |

验证思路：看网卡 `ethtool -S` 里 pause / prio / ecn / cnp / out_of_sequence / retransmit / discard 类计数器，以及交换机各队列的丢包、PFC、ECN、缓冲占用；确认 RDMA 流量进入预期队列、拥塞时 ECN 先于 PFC、没有意外丢包与重传。

*（完整术语表与 QoS 说明见 [roce-swtich.md](roce-swtich.md)；本集群上 `-t 104` 的实测影响见 [README.md](README.md#tc)。）*

<a id="10"></a>
## 10. 与单机 GPU P2P 的概念对照

GPU 之间经 NVLink 的 P2P，与 RDMA 在概念上是平行的：

| 维度 | NVLink P2P | RDMA |
|---|---|---|
| 地址空间 | 全机统一（UVA） | 节点间隔离（虚拟地址 + rkey 交换） |
| 访问原语 | 普通 load / store（地址恰好落在别家） | 显式 verb 提交（`ibv_post_send`） |
| 谁发事务 | SM 指令流本身 | HCA（CPU 只 post 描述符） |
| 完成机制 | 指令退休即完成 | CQ + 轮询 / 中断 |
| 授权建立 | `cudaDeviceEnablePeerAccess`（建页表映射） | `ibv_reg_mr`（pin + 内存窗口） |

**授权模型像 RDMA**（一侧发起、绕过对方处理器、零拷贝），**访问语义像 NUMA**（统一地址空间 + 普通 load / store）。GPU P2P 本质是"带显存的 NUMA"，NVSwitch 把 8 个 NUMA 节点缝起来。NVLS（NVSwitch 芯片内归约）在 RDMA 世界没有对应物。NCCL 把两者当平级 transport：机内 P2P（NVLink）、机间 IBVerbs（RDMA，含 GPUDirect RDMA）。 *（实验见 [c-single.md](c-single.md)）*

<a id="11"></a>
## 11. 术语速查

| 术语 | 含义 |
|---|---|
| RNIC / HCA | 支持 RDMA 的网卡 |
| PD / MR / QP / CQ / SRQ | 保护域 / 内存区域 / 队列对 / 完成队列 / 共享接收队列 |
| SQ / RQ | QP 的发送队列 / 接收队列，都是本地 RNIC 的工作队列 |
| WQE / CQE | 提交到 SQ / RQ 的工作项 / 写入 CQ 的完成项（verbs 里叫 WR / WC） |
| lkey / rkey | MR 的本地 / 远端访问凭证 |
| PSN / QPN | 包序号 / QP 号 |
| GID / GRH | RoCE 的地址 / 全局路由头 |
| RC / UC / UD | 可靠连接 / 不可靠连接 / 不可靠数据报（§6） |
| XRC | 扩展可靠连接，让多进程共享接收资源 |
| OOB | Out-Of-Band，带外：交换连接信息的 TCP 通道 |
| netdev / ib_core | Linux 的网络子系统 / RDMA 子系统（§2） |
| VRF / l3mdev | 虚拟路由转发：给网口一张独立的路由表（§2.7） |
| GSI / QP1 | 网卡上用于连接管理等管理报文的专用 QP（RDMA CM 的握手报文走它，§5.3） |
| provider | libibverbs 之下的厂商用户态驱动库（如 `libmlx5`），实现通用 verbs API（§2.6） |
| uverbs | 内核向用户态暴露 verbs 的设备节点 `/dev/infiniband/uverbsN`（§2.6） |
| MTE / pyverbs | Mooncake Transfer Engine（点对点传输库，见 [mte.md](mte.md)）/ rdma-core 的 Python 绑定（见 [py-multi-node-cpu.md](py-multi-node-cpu.md)） |
| MTT | 网卡内部的翻译表，从注册时的地址到总线地址 |
| IOVA / IOMMU | 网卡 DMA 用的地址 / 做地址翻译与隔离的硬件 |
| ODP | On-Demand Paging，按需分页，注册时不钉页 |
| BAR1 | GPU 暴露给 PCIe 总线的显存访问窗口 |
| peermem / dmabuf | GPUDirect RDMA 的两种实现 |
| VMM | CUDA 虚拟内存管理 API（`cuMem*`） |
| DSCP / ECN | IP 头 Traffic Class 的高 6 位 / 低 2 位 |
| PFC / DCQCN / CNP | 优先级流控 / 数据中心拥塞控制 / 拥塞通知报文 |
