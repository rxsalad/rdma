# pyverbs 实验：用 Python 经 RDMA 读写对端 CPU 内存

两台机器（server-0、server-1）上各跑一个**很短的 Python 脚本**，让 server-0 经 **RDMA（RoCE v2）** 直接读写 server-1 的**普通进程内存**：先 RDMA WRITE 一段数据写进去，再 RDMA READ 一段数据读回来，server 的 CPU 全程不参与数据搬运。

共两组案例，做的事完全相同，区别只在**怎么建连、怎么交换连接信息**：

| 案例 | 脚本（`pyverbs-example/`） | 建连方式 |
|---|---|---|
| **OOB 版** | `server_oob_cpu.py` / `client_oob_cpu.py` | 先经 `eth0` 上的 TCP（OOB，带外）交换连接信息，再**手工** `modify_qp` |
| **CM 版** | `server_cm_cpu.py` / `client_cm_cpu.py` | 交给 **`rdma_cm`**：用 IP 寻址，自动解析路由、建 QP、切状态 |

这是 [c-multi-node-cpu.md](c-multi-node-cpu.md)（纯 C 版）的 **Python 简化版**：协议、网卡都相同，只是换成 `pyverbs`（rdma-core 的 Python 绑定），并去掉带宽测试与诊断选项，只保留"建连 → WRITE → READ → 校验"这条主线，适合第一次看 verbs 流程。RDMA 的通用原理见 [rdma.md](rdma.md)。

**状态**：定稿。2026-11-01 在 server-0 / server-1 上实测通过；每个结论的可靠程度见文末附录。

**阅读指南**

| 你想…… | 读 |
|---|---|
| 先看结论 | 摘要 |
| 了解 pyverbs 是什么、靠哪些系统组件运行、系统调用层面发生了什么 | §1 |
| 准备环境、安装、拷贝脚本 | §2 |
| 比较两种建连方式 | §3 |
| 跑 OOB 版 / 跑 CM 版并看代码 | §4 / §5 |
| 搞清为什么 CM 版必须给 `--src` | §6 |
| 看重复性、`hop_limit`、接口差异 | §7 |
| 与原有示例、C 版的区别 | §8 |
| 排错 | §9 |
| 查某个结论有多可靠 | 附录 |

## 目录

- [摘要](#sum)
- [1. pyverbs 简介与系统依赖](#1)
- [2. 环境与准备](#2)
- [3. OOB 与 CM：两种建连方式](#3)
- [4. OOB 版](#4)
- [5. CM 版](#5)
- [6. VRF：fabric 网口各自独立的路由表](#6)
- [7. 实测与接口差异](#7)
- [8. 与原有 pyverbs 示例、与 C 版的差异](#8)
- [9. 坑](#9)
- [附录. 验证状态汇总](#A)

<a id="sum"></a>
## 摘要

| 问题 | 结论 | 可靠度 |
|---|---|---|
| 两个版本都能在 server-0 / server-1 上跑吗？ | **能**。装好 `python3-pyverbs` 后用 `python3.10` 运行；OOB 版 5 次（含 1 次 client 端 `hop_limit` 用默认值的对照）、CM 版 4 次，运行全部成功、退出码 0 | **实测** |
| 数据对吗？ | WRITE：server 在自己的内存里看到 client 写入的 40 字节；READ：client 读回 server 预置的 53 字节，内容都与预期一致 | **实测** |
| pyverbs 是什么、靠什么运行？ | rdma-core 的 Python 绑定（Cython 编译的 C 扩展），本身不实现 RDMA；全部靠系统的 `libibverbs` / `librdmacm` / provider 插件，再经 `/dev/infiniband/uverbsN` 进入宿主机内核驱动 | **实测**（§1） |
| 在 `n-server2.yaml` 的 Pod 上要准备什么？ | 只需 `apt-get install python3-pyverbs`（39.0-1，依赖由 apt 解决）并拷入脚本，其余由 yaml 提供（§2.2）；运行必须用 `python3.10`：默认 `python3`（3.12）顶层 `import pyverbs` 能成功，`from pyverbs.device import Context` 才报错 | **实测** |
| 数据面会进内核吗？ | **不会**：发起 WRITE / READ 与轮询完成项期间，`RDMA_VERBS_IOCTL` 为 0 次 | **实测**（`strace`，§1.5） |
| CM 版的建连与数据走同一条路径吗？ | 同一块网卡、同一条 fabric 路径，但**用的 QP 不同**：建连报文走 GSI QP（QP1），数据走 RC QP；OOB 版则是建连走 `eth0` 的 TCP | **实测**（§5.7） |
| CM 版为什么要用 fabric 网口的 IP？ | `rdma_cm` 靠 IP 找 RDMA 设备；填 `eth0` 的 IPv4 报 `Errno: 19, No such device` | **实测** |
| CM 版 client 为什么必须给 `--src`？ | 每块 fabric 网卡挂在独立的 **VRF** 上，主路由表里没有 fabric 网段的路由；不给报 `Errno: 101, Network is unreachable` | **实测**（§6） |
| `hop_limit` 一定要设 64 吗？ | **不用**：只把 client 一端改回 pyverbs 默认值（1），仍然 WRITE / READ 成功 | **实测**；双端都用默认值未测，原因未查明 |

<a id="1"></a>
## 1. pyverbs 简介与系统依赖

### 1.1 pyverbs 是什么

**pyverbs 是 rdma-core 项目自带的 Python 绑定**：用 Cython 把 `libibverbs`（verbs API）和 `librdmacm`（RDMA CM）的 C 接口包装成 Python 类。它**不自己实现任何 RDMA 功能**——每个调用最终都是对系统 C 库的调用。

- **来源**：Debian / Ubuntu 的源码包是 `rdma-core`，`python3-pyverbs` 是它拆出来的二进制包（本环境 `python3-pyverbs 39.0-1`）。
- **用途**：rdma-core 自己用它写测试；适合做原型、教学、快速验证环境。
- **性能边界**：热路径在 C 库里，但每个操作都由 Python 逐个发起，完成项靠 Python 忙轮询——所以**适合验证流程，不适合测带宽**。
- **对象一一对应**：

| pyverbs | 对应的 C 对象 | 本实验里的用途 |
|---|---|---|
| `Context` | `ibv_context` | 打开 RDMA 设备（如 `mlx5_0`）、查端口与 GID |
| `PD` / `CQ` | `ibv_pd` / `ibv_cq` | 保护域 / 完成队列 |
| `MR` | `ibv_mr` | **一步完成"分配内存 + 注册"**，得到 `buf`（地址）、`lkey`、`rkey` |
| `QP` / `QPInitAttr` / `QPAttr` / `QPCap` | `ibv_qp` 及其属性 | 创建队列对、手工切状态（OOB 版） |
| `SGE` / `SendWR` / `RecvWR` | `ibv_sge` / `ibv_send_wr` / `ibv_recv_wr` | 描述一次发送 / 接收（OOB 版） |
| `GID` / `GlobalRoute` / `AHAttr` | `ibv_gid` / `ibv_global_route` / `ibv_ah_attr` | RoCE 的地址向量（OOB 版） |
| `CMID` / `AddrInfo` / `ConnParam` | `rdma_cm_id` / `rdma_addrinfo` / `rdma_conn_param` | 用 `rdma_cm` 建连并收发（CM 版） |

### 1.2 依赖层次

```
你的脚本（python3.10）
  └ pyverbs：/usr/lib/python3/dist-packages/pyverbs/*.cpython-310-….so    ← Cython 编译的 C 扩展
      ├ libibverbs.so.1（39.0）─ 加载 provider 插件 ─→ libmlx5-rdmav34.so（mlx5 网卡的用户态驱动）
      ├ librdmacm.so.1（39.0）                       ← 仅 CM 版用到
      └ libnl-3 / libnl-route-3
            ↓  open / ioctl / mmap
  /dev/infiniband/uverbs0、rdma_cm                   ← 容器与宿主机的边界
            ↓
  宿主机内核：ib_uverbs → mlx5_ib → 网卡（PCI 驱动 mlx5_core，固件 40.47.2526）
```

容器里只有**用户态**部分（pyverbs、libibverbs、librdmacm、libmlx5）和设备节点；内核驱动与固件在宿主机，由设备节点隔开。所以容器里不需要装任何驱动，但 `/dev/infiniband` 必须被挂进来。

### 1.3 本环境里查到的静态依赖信息（**实测**，server-0 的 Pod 内）

运行时才发生的事（provider 加载、选驱动等）放在 §1.5。

| 项 | 实测结果 |
|---|---|
| 包依赖 | `python3-pyverbs` 声明依赖 `libibverbs1 (>= 36)`、`librdmacm1 (>= 31)`、`ibverbs-providers (>= 37)`、`rdma-core (>= 21)`、`libpython3.10`，并限定 `python3 (<< 3.11)` |
| 包的内容 | 一批 `*.cpython-310-x86_64-linux-gnu.so`：`device`、`pd`、`mr`、`cq`、`qp`、`wr`、`addr`、`cmid`、`enums`、`cm_enums` 等；另有 `providers/mlx5/`（`mlx5dv` 等 mlx5 专有接口）与 `providers/efa/`。本篇不用 provider 子包 |
| 链接关系（`ldd`） | `device`、`cmid` 链接 `librdmacm`、`libibverbs`、`libnl-3`、`libnl-route-3`；`qp` 只链接 `libibverbs` 与 libnl。libibverbs、libmlx5 自己也依赖 libnl（用途见 §1.5） |
| 版本 | `libibverbs1`、`librdmacm1`、`ibverbs-providers`、`rdma-core`、`python3-pyverbs` 全部是 **39.0-1**，同源同版本 |
| 设备节点 | `/dev/infiniband/uverbs0`（char 231,192，权限 `crw-rw-rw-`）、`/dev/infiniband/rdma_cm`（char 10,262） |
| 内核侧 | `uverbs` ABI 版本 6；`mlx5_0` 的 PCI 驱动是 `mlx5_core`（设备 `0000:86:00.0`），固件 `40.47.2526`；`node_type` 为 CA |

### 1.4 运行时进程里有什么（**实测**，`/proc/self/maps` 与 `/proc/self/fd`）

| 时刻 | 进程里有什么 |
|---|---|
| 仅 `import` pyverbs | 加载了 pyverbs 的各个 `.so` 以及 `libibverbs`、`librdmacm`、`libnl`；**没有**打开任何设备文件，**没有**加载 provider |
| 打开设备 `Context(name="mlx5_0")` 之后 | provider 插件被加载；进程持有 `/dev/infiniband/uverbs0` 的 fd（`cmd_fd = 3`）与一个 `anon_inode:[infinibandevent]`（异步事件）；对 `uverbs0` 有 **3 个内存映射** |
| 再创建 PD / CQ / MR / QP 之后 | 内存映射增加到 **4 个**（2 可写、2 只读） |
| CM 版创建 `CMID` 之后 | 额外打开 `/dev/infiniband/rdma_cm` |

映射的来源与用途见 §1.5（阶段 3 与阶段 4）。

### 1.5 pyverbs 如何使用这些系统文件（`strace` 追踪，**实测**）

pyverbs 自己不做 RDMA 的事，只是 C 扩展模块；所有事情靠下面这几类系统资源完成。追踪方法：在 server-0 的 Pod 里装 `strace`（只装在容器层），分别追踪"import → 打开设备 → 创建 PD / CQ / MR / QP"和真实的 `client_oob_cpu.py`。下文的"行号"是 `strace` 日志中的先后顺序。

**阶段 1：动态链接（Python 导入 `pyverbs.device` 时）**

1. Python 打开 `/usr/lib/python3/dist-packages/pyverbs/device.cpython-310-….so`；
2. 这个 `.so` 里只写着 `NEEDED: libibverbs.so.1` 等，**没有 RPATH**，所以交给系统的动态链接器 `ld.so`；
3. `ld.so` 读 `/etc/ld.so.cache`，并按搜索顺序查找：先试 `/usr/local/cuda/lib64/libibverbs.so.1`（`ENOENT`，这个目录**推测**来自 `LD_LIBRARY_PATH`），再命中 `/lib/x86_64-linux-gnu/libibverbs.so.1`；`librdmacm`、`libnl-3`、`libnl-route-3` 同理（行 810–839）；
4. 此时只是把库映射进进程，**还没碰任何设备**。

**阶段 2：libibverbs 发现设备（`Context(name="mlx5_0")` 时）**

| 顺序（行号） | 动作 | 用到的系统文件 |
|---|---|---|
| 1247–1263 | 建 **`NETLINK_RDMA` 套接字**，发一个请求、连续收到十几条回复。请求的类型值是 `0x1401`，按公开资料对应 RDMA nldev 的设备查询（**推断**）——即向内核询问"有哪些 RDMA 设备" | 内核 RDMA 子系统（经 netlink，用到 libnl） |
| 1277–1334 | 从 `uverbs19` 倒数到 `uverbs0`，对每个节点做 `stat`：存在的（偶数号）返回设备号，不存在的 `ENOENT` | `/dev/infiniband/uverbsN` |
| 1336–1435 | 读 `/etc/libibverbs.d/` 下 15 个 `*.driver` 文件；`mlx5.driver` 的内容是 `driver mlx5` | `/etc/libibverbs.d/*.driver` |
| 1436–1554 | 把 `/usr/lib/x86_64-linux-gnu/libibverbs/` 下的 provider 插件**逐个加载**（行 1526 是 `libmlx5-rdmav34.so`，它是指向 `libmlx5.so.1.22.39.0` 的符号链接） | provider 插件 `.so` |

注意：整个追踪里**读 `/sys` 的次数是 0**。这个版本的 libibverbs 靠 netlink 加 `stat` 设备节点发现设备，不读 sysfs。

**阶段 3：打开设备，与内核交互**

| 行号 | 动作 | 说明 |
|---|---|---|
| 1572 | `open("/dev/infiniband/uverbs0", O_RDWR)` → fd 3 | 整个运行只打开一次 |
| 1575–1577 | 第一个 `RDMA_VERBS_IOCTL` 返回 `ENOSPC`，第二个成功 | 之后所有命令都走这个 ioctl，整个运行共 **22 次**；**没有**对该 fd 的 `write()`（0 次），即用新式 ioctl 接口，不是旧式 `write` 命令接口 |
| 1578–1579 | `mmap(4096, PROT_READ, MAP_SHARED, fd, 偏移 0x500000 / 0x700000)` | 两个只读页 |
| 约 1967 | `mmap(4096, PROT_WRITE, 偏移 0x900000)` | 可写页，在打开设备的过程中映射 |
| 约 2004 | 创建 PD / CQ / MR / QP 的几个 ioctl 之后，再一个 `mmap(PROT_WRITE, 0x901000)` | 第二个可写页 |

映射数与 §1.4 一致（打开后 3 个：1 个可写、2 个只读；创建之后 4 个）。行 1579–1960 之间约 380 行是 mlx5 provider 的初始化，本篇**没有逐行细看**。

**阶段 4：数据面——没有系统调用**

对真实的 `client_oob_cpu.py` 追踪，数 WRITE 与 READ 期间的系统调用：

| 窗口 | 系统调用 | `RDMA_VERBS_IOCTL` 次数 |
|---|---|---|
| QP 就绪 → WRITE 完成（`post_send` + 轮询 CQ） | 3 次 `write`（都是 `print` 到标准输出）、1 次 `pselect6`（脚本里 `time.sleep(0.0005)`） | **0** |
| WRITE 完成 → READ 完成（含 OOB 通知，再 READ） | OOB 的 `sendto` / `recvfrom` / `poll`、1 次 `pselect6` | **0** |

即发起 WRITE / READ 和取完成项这两步**都没有进内核**。**推断**：`post_send` 靠上面映射的可写页（网卡门铃寄存器页）通知网卡，`poll` 靠读 CQ 所在的内存；映射的存在与"数据面零 ioctl"是实测的，页面的具体用途是通用机制说法。

**汇总：pyverbs 用到哪些系统文件**

| 系统文件 | 由谁使用 | 作用 |
|---|---|---|
| `/etc/ld.so.cache`、`/lib/x86_64-linux-gnu/libibverbs.so.1` 等 | 动态链接器 `ld.so` | 找到并加载 libibverbs、librdmacm、libnl |
| `NETLINK_RDMA` 套接字 | libibverbs（经 libnl） | 向内核查询设备列表 |
| `/etc/libibverbs.d/*.driver` | libibverbs | 知道有哪些驱动 |
| `/usr/lib/x86_64-linux-gnu/libibverbs/*.so` | libibverbs（`dlopen`） | 各厂商网卡的用户态驱动，mlx5 就在其中 |
| `/dev/infiniband/uverbsN` | libibverbs + provider | 控制面：ioctl 创建 PD / CQ / MR / QP；mmap 门铃页等 |
| `/dev/infiniband/rdma_cm` | librdmacm（仅 CM 版） | 连接管理 |

**限度**：`strace` 只能看到系统调用，看不到对 mmap 内存的读写；这次追踪只覆盖 OOB 版，CM 版额外走 `rdma_cm`，没有追踪。`strace` 装在容器层，Pod 重建会丢。

### 1.6 几个要点

1. **Python 版本是硬约束**：apt 的 pyverbs 只有 `cpython-310` 的二进制模块。默认 `python3` 是 3.12（`sys.path` 里包含 `/usr/lib/python3/dist-packages`）：顶层 `import pyverbs` 会**成功**（`__init__.py` 是纯 Python），容易误以为装好了；`from pyverbs.device import Context` 才报 `No module named 'pyverbs.device'`。所以必须用 **`python3.10`**。
2. **同源同版本**：pyverbs、libibverbs、librdmacm、provider 全是 rdma-core 39.0。仓库里原有示例是在 Ubuntu 24.04（rdma-core 更新）上写的，接口有差异，见 §7.3。
3. **依赖全在容器层**：`apt-get install` 装在容器可写层，Pod 重建后会丢。
4. **版本配合**：宿主机内核 6.12 与容器里的 rdma-core 39 能配合工作（本实验已跑通）。

<a id="2"></a>
## 2. 环境与准备

### 2.1 环境（本次实测）

| | server-0（client） | server-1（server） |
|---|---|---|
| RDMA 设备 ↔ 网口 | `mlx5_0` ↔ `fabric0`（`rdma link` 可见） | `mlx5_14` ↔ `fabric14` |
| `eth0`（Pod 网络，OOB 版用） | 10.133.1.12 | 10.133.0.253 |
| fabric 网口 IPv6（CM 版用） | `fd02:0:400:52e::1/64` | `fd02:0:0:527::1/64` |
| 本端 GID[3] | `fd02:0000:0400:052e:0000:0000:0000:0001` | `fd02:0000:0000:0527:0000:0000:0000:0001` |
| 端口 | ACTIVE（state 4）、`active_mtu` = 5（即 4096）、link layer = 2（Ethernet） | 同左 |

GID 表（`mlx5_0` 实测）：下标 0 / 1 是 `fe80::`（链路本地，不能跨机），下标 2 / 3 是 `fd02:…`；`query_gid_type` 显示 0 / 2 为 RoCE v1，**1 / 3 为 RoCE v2**，所以用 **GID 下标 3**。GID 由网口的 IP 生成，因此 `fd02:…` 与 fabric 网口的地址是同一个值（[rdma.md §2.3](rdma.md#2)）。

另外，每块 fabric 网卡都挂在独立的 VRF 上，会影响 CM 版的用法，见 [§6](#6)。

### 2.2 安装 pyverbs

**结论：在 `n-server2.yaml` 起好的 Pod 上，只需再装 `python3-pyverbs`（依赖由 apt 自动解决），并把脚本拷进 Pod；其余不用额外准备。**

| 项 | 来源 |
|---|---|
| `libibverbs1`、`librdmacm1`、`ibverbs-providers`（含 `libmlx5`） | yaml 启动时装的 `ibverbs-utils`、`infiniband-diags` 已带上；pyverbs 只带 Cython 模块，不自带 `libibverbs.so.1`（[§1](#1)） |
| `uverbs` 设备、`IPC_LOCK` 能力（注册内存锁页用） | yaml 的 `rdma/fabric…` 资源与 `securityContext` |
| `python3-pyverbs` | **要自己装**（Pod 镜像里没有：`import pyverbs` 报 `ModuleNotFoundError`，两个 Pod 一样） |

两个 Pod 各装一次（**实测**）：

```bash
kubectl exec server-0 -- bash -lc 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pyverbs'
kubectl exec server-1 -- bash -lc 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pyverbs'
```

- 运行时必须用 `python3.10`（§1.6）。
- 装在容器可写层，**Pod 重建后要重装**；想固化可把 `python3-pyverbs` 加进 yaml 的 `apt install` 行（**未测**：未重建 Pod 验证）。
- 其余前提（宿主机 mlx5 驱动、可锁定内存、RoCE v2 GID、网络互通）与主机内存 C 版相同，见 [c-multi-node-cpu.md §1.3](c-multi-node-cpu.md#1-3)。**不需要** OFED、CUDA。

### 2.3 拷贝脚本

在 nuc 上，从仓库的 `pyverbs-example/` 拷进 Pod（用 stdin 重定向，不依赖目标目录）：

```bash
cd pyverbs-example
for f in client_oob_cpu client_cm_cpu; do kubectl exec -i server-0 -- bash -c "cat > /vllm-workspace/$f.py" < $f.py; done
for f in server_oob_cpu server_cm_cpu; do kubectl exec -i server-1 -- bash -c "cat > /vllm-workspace/$f.py" < $f.py; done
```

四个脚本各自独立（小工具有重复，为的是每个文件能单独读懂）。行数（含大量中文注释：每一步做什么、为什么、对应的 RDMA 概念都写在代码旁）：

| | server | client |
|---|---|---|
| OOB 版 | 204 行 | 227 行 |
| CM 版 | 165 行 | 182 行 |

<a id="3"></a>
## 3. OOB 与 CM：两种建连方式

| | OOB 版（§4） | CM 版（§5） |
|---|---|---|
| 建连 | 自己建 PD / CQ / QP，**手工** `modify_qp`：INIT → RTR → RTS | `rdma_cm` 代劳：解析地址与路由、建 QP、切状态 |
| 寻址 | 手工选 `--dev`（设备名）与 `--gid-index` | 给 **IP 地址**，`rdma_cm` 据此找到 RDMA 设备与 GID |
| 交换连接信息 | 额外的 TCP（OOB）：`qpn` / `psn` / `gid` / `addr` / `rkey` | `rdma_cm` 自己交换 `qpn` / `psn` / `gid`；`addr` / `rkey` 用连接后的一条 **RDMA SEND** 传 |
| 控制信息（"写完了"/"结束"） | OOB 上的 1 字节 | RDMA SEND / RECV |
| 用到的地址 | server 的 `eth0` IP（TCP）+ GID 下标 | server 与本端的 **fabric 网口 IP** |
| 建连报文走哪里 | `eth0` 上的 TCP（**另一张网**） | fabric 上网卡的 **GSI QP（QP1）**（**同一块网卡、同一条路径**），见 §5.7 |
| 需要 `eth0` | 是 | 否 |
| `traffic_class` | `--tc`，默认 104 | **没有设置**（等于 0），见 §5.6 |
| 对 VRF 的敏感度 | 不依赖 IP 路由，不受影响 | **`--src` 必填**（§6） |

OOB 版里，OOB 只传连接信息和 1 字节同步，数据走 RDMA；`eth0` 上的 TCP 不是 RDMA 连接，只是临时的信令通道。**两个版本都一样：WRITE 对 server 是"无声"的**——server 不知道数据已到，所以都要另外通知它去看（见 [rdma.md §5.4](rdma.md#5)）。

两个版本共用同一个内存布局（server / client 各注册一块内存，各取一半）：

| | 前半（`[0, 2048)`） | 后半（`[2048, 4096)`） |
|---|---|---|
| **server** | 留给 client **写入**，初始为 0 | 预置数据 `Hello from server CPU memory (read me with RDMA READ)`，供 client **读取** |
| **client** | 要**写给对端**的 `Hello from client, written by RDMA WRITE` | **读回来**的落点 |

（CM 版里 server 的两半是两块独立的 MR：`mr_w` 允许远端写、`mr_r` 允许远端读。）

<a id="4"></a>
## 4. OOB 版

### 4.1 时间线

```
  server-1（被动方）                                   server-0（主动方）
  ① 注册 CPU 内存、建 QP（INIT）                       ① 注册 CPU 内存、建 QP（INIT）
  ② eth0 上 listen、accept          ◄── TCP connect ──  ② 连接 server 的 eth0 IP
  ③ 收 client 信息、回自己的 {qpn, psn, gid, addr, rkey, msg_len}
  ④ QP: INIT → RTR → RTS                               ④ QP: INIT → RTR → RTS
  ⑤ 等 OOB 命令                                        ⑤ RDMA WRITE：本端 [0,..) → 对端 [0,..)
                                    ◄──── 'W' ─────────  ⑥ 经 OOB 通知"写完了"
  ⑦ 读自己的内存 [0,2048)，打印 ───── 'K' ─────────►
                                                        ⑧ RDMA READ：对端 [2048,..) → 本端 [2048,..)，打印
                                    ◄──── 'Q' ─────────  ⑨ 结束
```

### 4.2 运行（server 先启动）

```bash
kubectl get pod server-1 -o jsonpath='{.status.podIP}'       # 本次为 10.133.0.253

# 终端 1：server-1
kubectl exec -it server-1 -- bash -lc 'cd /vllm-workspace && python3.10 -u server_oob_cpu.py --dev mlx5_14'
# 终端 2：server-0。--server 填 server-1 的 eth0 IP
kubectl exec -it server-0 -- bash -lc 'cd /vllm-workspace && python3.10 -u client_oob_cpu.py --server 10.133.0.253 --dev mlx5_0'
```

`-u` 让 Python 不缓冲输出；把输出重定向到文件时不加它，日志要等进程结束才出现（§9）。server 处理完一个 client 就退出。

| 参数 | 含义 | server 默认 | client 默认 |
|---|---|---|---|
| `--dev` | RDMA 设备名 | `mlx5_14` | `mlx5_0` |
| `--gid-index` | RoCE v2 的非链路本地 GID 下标 | 3 | 3 |
| `--tc` | traffic_class（104 = DSCP 26） | 104 | 104 |
| `--port` | OOB 端口 | 18515 | 18515 |
| `--bind` | OOB 监听地址 | `0.0.0.0` | — |
| `--server` | server 的 OOB IP | — | **必填** |

### 4.3 实测输出

**client**（server-0）：

```
设备 mlx5_0，active_mtu=5，本端 GID[3] = fd02:0000:0400:052e:0000:0000:0000:0001
对端：QPN=3256 GID=fd02:0000:0000:0527:0000:0000:0000:0001 addr=0x55773fbf0000 rkey=135680
QP 已就绪（RTS）
RDMA WRITE 完成：写入 40 字节 -> b'Hello from client, written by RDMA WRITE'
RDMA READ  完成：读回 53 字节 -> b'Hello from server CPU memory (read me with RDMA READ)'
client 结束
```

**server**（server-1）：

```
设备 mlx5_14，active_mtu=5，本端 GID[3] = fd02:0000:0000:0527:0000:0000:0000:0001
已注册 CPU 内存：addr=0x55773fbf0000 rkey=135680 len=4096
OOB 监听 0.0.0.0:18515，等待 client ...
OOB 连接来自 ('10.133.1.12', 50224)
QP 已就绪（RTS）。之后 server 不再参与数据传输，只等 client 的通知。
[server] client 的 RDMA WRITE 之后，我的内存 [0,2048) 里是： b'Hello from client, written by RDMA WRITE'
server 结束
```

server 打印的 `addr` / `rkey` 正是 client 在"对端"一行里拿到的值，印证 OOB 交换无误；`OOB 连接来自 10.133.1.12` 即 server-0 的 `eth0`。

### 4.4 代码讲解

下面只讲与 RDMA 相关的关键片段，细节直接读脚本里的注释。

**① 注册 CPU 内存，并允许远端读写**（server）：

```python
acc = (ibv_access_flags.IBV_ACCESS_LOCAL_WRITE
       | ibv_access_flags.IBV_ACCESS_REMOTE_WRITE
       | ibv_access_flags.IBV_ACCESS_REMOTE_READ)
mr = MR(pd, BUF_SIZE, acc)          # 分配 4096 字节并注册，得到 mr.buf（地址）与 mr.rkey
```

`MR(...)` 一步完成"分配内存 + `ibv_reg_mr`"。`mr.buf` 是这块内存的虚拟地址（整数，要用 `ctypes` 读写），`mr.rkey` 是给对端用的凭证，这两个值经 OOB 发给 client。client 的 MR 只开 `LOCAL_WRITE`（READ 回来的数据要落进本端内存）。

**② 让 QP 接受对端的读写**（server）：

```python
qp_attr = QPAttr(port_num=1)
qp_attr.qp_access_flags = REMOTE_WRITE | REMOTE_READ   # 在 INIT 阶段设置
qp = QP(pd, init, qp_attr)                             # 创建后处于 INIT
```

MR 的权限和 **QP 的权限都要开**，对端的 WRITE / READ 才会被接受。

**③ OOB 交换 + QP 状态转换**（两端相同）：

```python
a.qp_state = IBV_QPS_RTR;  a.path_mtu = port.active_mtu     # 取端口的 active_mtu（4096）
a.dest_qp_num = theirs["qp_num"];  a.rq_psn = theirs["psn"]
a.ah_attr = AHAttr(port_num=1, is_global=1,                  # RoCE 必须带 GRH
                   gr=GlobalRoute(dgid=GID(theirs["gid"]), sgid_index=3,
                                  hop_limit=64, traffic_class=104), dlid=0)
qp.to_rtr(a)
a.qp_state = IBV_QPS_RTS;  a.sq_psn = psn                    # 再切 RTS
qp.to_rts(a)
```

与 [rdma.md §5.2](rdma.md#5) 的状态机一一对应：INIT → RTR 需要**对端**的 QPN / PSN / GID，RTR → RTS 需要本端 PSN。

**④ 发起单边操作**（client）：

```python
sge = SGE(local_addr, length, lkey)
wr = SendWR(wr_id=1, opcode=ibv_wr_opcode.IBV_WR_RDMA_WRITE,   # READ 则换成 IBV_WR_RDMA_READ
            num_sge=1, sg=[sge], send_flags=ibv_send_flags.IBV_SEND_SIGNALED)
wr.set_wr_rdma(rkey, remote_addr)      # 单边操作：对端地址与 rkey
qp.post_send(wr)
n, wcs = cq.poll(1)                    # 忙轮询 CQ，wcs[0].status == 0 表示成功
```

`set_wr_rdma(rkey, remote_addr)` 是 WRITE / READ 与 SEND 的区别所在：必须带上对端的**地址与 rkey**；对端不需要 post 任何接收（单边，见 [rdma.md §7](rdma.md#7)）。

<a id="5"></a>
## 5. CM 版

概念见 [rdma.md §5.3](rdma.md#5)。

### 5.1 地址：要用 fabric 网口的 IP，不是 eth0

`rdma_cm` 靠 **IP 地址**找 RDMA 设备：把这个 IP 对应到某个 netdev，再找到绑定在该 netdev 上的 RDMA 设备（这正是 [rdma.md §2.3](rdma.md#2) 里"RDMA 设备依附于 netdev"的体现）。所以必须用 **fabric 网口**的地址：

| | server-0（client） | server-1（server） |
|---|---|---|
| 使用的 RDMA 设备 ↔ 网口 | `mlx5_0` ↔ `fabric0` | `mlx5_14` ↔ `fabric14` |
| 该网口的 IPv6 地址（ULA） | `fd02:0:400:52e::1/64` | `fd02:0:0:527::1/64` |
| `eth0`（Pod 网络，**不是** RDMA 网口） | 10.133.1.12 | 10.133.0.253 |

- 各 fabric 网口的地址形如 `fd02:0:<rail>:<节点标识>::1/64`；例如 server-0 的 `fabric2` 是 `fd02:0:500:52e::1`，server-1 的 `fabric2` 是 `fd02:0:500:527::1`（**实测**，`ip -br -6 addr`）。
- **server** 用 `--bind` 指定监听地址，就等于选定了用哪块网卡（绑 `fd02:0:0:527::1` → `mlx5_14`）。
- **client** 用 `--src` 指定本端源地址（选定 `mlx5_0`），`--server` 指定对端的 fabric 地址。**`--src` 在本集群上必填**，原因见 §6。
- **填 `eth0` 的 IPv4 会失败**：`--server 10.133.0.253` 报 `PyverbsRDMAError: Failed to create CM ID. Errno: 19, No such device`——`eth0` 上没有 RDMA 设备（**实测**）。

### 5.2 时间线

```
  server-1（被动方）                                  server-0（主动方）
  ① 创建监听端点（--bind），listen
                                                      ① 创建端点（--src → --server），rdma_cm 解析地址、路由、建 QP
  ② get_request()：取到连接请求，得到一条新连接
  ③ 注册两块 CPU 内存：mr_w（远端可写）、mr_r（远端可读）
  ④ 先挂好 2 条接收缓冲（收 'W'、'Q'）                ② 先挂好 2 条接收缓冲（收内存信息、收 'K'）
  ⑤ accept()                         ◄── connect() ──  ③ connect()，阻塞到连接建立
  ⑥ SEND：{mr_w.addr/rkey, mr_r.addr/rkey, len} ────►  ④ 收到 server 的内存信息
                                                      ⑤ RDMA WRITE：本端 → 对端 mr_w
                                    ◄──── SEND 'W' ──  ⑥ 通知"写完了"
  ⑦ 读自己的 mr_w，打印；SEND 'K' ───────────────────►
                                                      ⑦ RDMA READ：对端 mr_r → 本端
                                    ◄──── SEND 'Q' ──  ⑧ 结束
```

**接收缓冲必须在对端 SEND 之前挂好**——所以 server 在 `accept()` 之前、client 在 `connect()` 之前就先 `post_recv`。

### 5.3 运行（server 先启动）

```bash
# 终端 1：server-1（--bind 填它自己的 fabric14 地址）
kubectl exec -it server-1 -- bash -lc 'cd /vllm-workspace && python3.10 -u server_cm_cpu.py --bind fd02:0:0:527::1'
# 终端 2：server-0（--src 填本端 fabric0 地址，--server 填对端 fabric14 地址）
kubectl exec -it server-0 -- bash -lc 'cd /vllm-workspace && python3.10 -u client_cm_cpu.py --src fd02:0:400:52e::1 --server fd02:0:0:527::1'
```

| 参数 | 含义 | server | client |
|---|---|---|---|
| `--bind` | 监听的 fabric 网口 IP | **必填** | — |
| `--server` | server 的 fabric 网口 IP | — | **必填** |
| `--src` | 本端 fabric 网口 IP | — | **本集群必填**（VRF，§6） |
| `--port` | CM 端口（字符串） | 18515 | 18515 |

不需要 `--dev` 和 `--gid-index`：设备与 GID 由 `rdma_cm` 根据 IP 决定。

### 5.4 实测输出

**client**：

```
连接 [fd02:0:0:527::1]:18515 ...
连接已建立，QPN = 2688
对端内存：mr_w addr=0x5573063f3000 rkey=135680；mr_r addr=0x5573063f4000 rkey=136192
RDMA WRITE 完成：写入 40 字节 -> b'Hello from client, written by RDMA WRITE'
RDMA READ  完成：读回 53 字节 -> b'Hello from server CPU memory (read me with RDMA READ)'
client 结束
```

**server**：

```
CM 监听 fd02:0:0:527::1:18515，等待 client ...
收到连接请求，QPN = 3261
已注册 CPU 内存：mr_w addr=0x5573063f3000 rkey=135680；mr_r addr=0x5573063f4000 rkey=136192
连接已接受
已把内存的 addr / rkey 发给 client。之后 server 不参与数据传输。
[server] client 的 RDMA WRITE 之后，我的内存 mr_w 里是： b'Hello from client, written by RDMA WRITE'
server 结束
```

client 拿到的"对端内存"正是 server 注册时打印的 `addr` / `rkey`，说明经 SEND 传递的信息无误。

### 5.5 代码讲解（只讲与 OOB 版不同的部分）

**① 创建端点：`rdma_cm` 一步完成设备查找、路由解析与 QP 创建**

```python
# client
addr = AddrInfo(src=args.src, dst=args.server, dst_service=args.port,
                port_space=rdma_port_space.RDMA_PS_TCP)       # TCP 端口空间 = RC 语义
cm = CMID(creator=addr, qp_init_attr=QPInitAttr(cap=cap))     # 内部建好 PD / CQ / QP
# server
addr = AddrInfo(src=args.bind, src_service=args.port,
                port_space=rdma_port_space.RDMA_PS_TCP, flags=RAI_PASSIVE)   # RAI_PASSIVE = 我是 server
listener = CMID(creator=addr, qp_init_attr=QPInitAttr(cap=cap)); listener.listen()
conn = listener.get_request()                                 # 每个连接请求得到一个新的 CMID
```

`RDMA_PS_TCP` 是 `rdma_cm` 的"端口空间"，对应 RC 连接，与 TCP 协议无关，只是借用了名字。client 的 `CMID(...)` 在失败时（填了 `eth0` 的 IP、或没给 `--src`）会抛 `PyverbsRDMAError`，脚本捕获后打印提示再原样抛出。

**② 注册内存：`reg_write` / `reg_read` / `reg_msgs`**

```python
mr_w = conn.reg_write(SIZE)    # 允许远端写（client 的 RDMA WRITE 写这里）
mr_r = conn.reg_read(SIZE)     # 允许远端读（client 的 RDMA READ 读这里）
buf  = conn.reg_msgs(64)       # 收发 SEND/RECV 消息用，也可作 RDMA 的本地缓冲
```

OOB 版是一块 MR 同时开读写权限；CM 版里 `reg_write` / `reg_read` 各给一块 MR，所以 server 有**两组** `addr` / `rkey` 要告诉 client。CM 版里**没有**像 OOB 版那样手工设 `qp_access_flags`，WRITE 与 READ 照样成功（**实测**）；`rdma_cm` 内部如何设置 QP 的访问权限，本篇没有查。

**③ 先挂接收缓冲，再 `accept()` / `connect()`**

```python
conn.post_recv(w_buf, 16); conn.post_recv(q_buf, 16)    # server：accept 之前
conn.accept()
# client：
cm.post_recv(info, 64);   cm.post_recv(ack, 16)         # connect 之前
cm.connect()
```

对端一连上就可能发 SEND；缓冲没挂好，对端的 SEND 可能遇到接收方未就绪（RNR）而重试或失败（机制说法，本篇没有故意不挂来验证）。完成项**按挂的顺序返回**，所以 client 的第 1 个接收完成项是 server 的内存信息、第 2 个是确认 `'K'`。

**④ 单边操作：`post_write` / `post_read`**

```python
cm.post_write(src_mr, len(CLIENT_MSG), addr_w, rkey_w, flags=ibv_send_flags.IBV_SEND_SIGNALED)
cm.post_read (dst_mr, msg_len,         addr_r, rkey_r, flags=ibv_send_flags.IBV_SEND_SIGNALED)
wc = cm.get_send_comp()        # 返回 wc，没有完成项时返回 None；status == 0 表示成功
```

比 OOB 版的 `SendWR` + `set_wr_rdma` 更短：地址、rkey、长度直接作参数。脚本里显式带了 `IBV_SEND_SIGNALED`；不带是否也会产生完成项，本篇**没有试**。

**⑤ 没有 `modify_qp`，也没有 TCP**：`connect()` / `accept()` 返回时 QP 已经在 RTS，状态转换由 `rdma_cm` 完成。

### 5.6 CM 版特有的实测

**连接实际落在哪块网卡**：让 client 保持连接，在两个 Pod 里用 `rdma res show` 查看（**实测**）：

```
server-0: link mlx5_0/1  ... qp-type RC state CONNECT ps TCP
          src-addr fd02:0:400:52e::1:56933  dst-addr fd02:0:0:527::1:18515
server-1: link mlx5_14/1 ... state LISTEN  src-addr fd02:0:0:527::1:18515
          link mlx5_14/1 ... qp-type RC state CONNECT  dst-addr fd02:0:400:52e::1:56933
```

client 用 `mlx5_0`、server 用 `mlx5_14`，QP 类型 RC——与 `--src` / `--bind` 选定的网口一致，说明 `rdma_cm` 确实是"按 IP 找设备"。

**`traffic_class` 没有设置**：OOB 版用 `--tc 104`，CM 版里没有设置（即 TC 0）。原因：`rdma_create_ep` 在创建端点时就解析了地址与路由，而 TOS 需要在路由解析之前设置；本篇没有试用 `set_option`，或拆成"先建 id、再设 TOS、再解析路由"的流程。对本实验传输的几十字节没有影响，也**没有**用 CM 版去复测"TC 影响 READ"（见 [c-multi-node-cpu.md §5.4](c-multi-node-cpu.md#5-4)，那是 64 MiB 级的 READ 带宽现象）。

### 5.7 建连与数据：同一条路径，能分清吗（**实测**）

CM 版里，"先建连接、后传数据"是不是走同一条路径？能不能分清？做法：每块网卡都有一个管理用的 **GSI QP（QP1）**，用 `rdma res show qp` 记录它的发送序号 `sq-psn`，分别在跑 OOB 版和 CM 版前后各取一次（`mlx5_0` 是 client 用的网卡，`mlx5_14` 是 server 用的）：

| 操作 | server-0 `mlx5_0` 的 GSI `sq-psn` | server-1 `mlx5_14` 的 GSI `sq-psn` |
|---|---|---|
| 初始 | 21 | 14 |
| 跑一次 **OOB 版** | 21（**不变**） | 14（**不变**） |
| 跑一次 **CM 版** | 24（**+3**） | 16（**+2**） |
| 再跑一次 CM 版 | 27（**+3**） | 18（**+2**） |

两次 CM 版的增量完全一致；OOB 版没有任何增量。据此分三层看：

| 层 | 结论 |
|---|---|
| **代码层** | **能分清**：建连是 `CMID(...)` / `connect()` / `accept()`；交换内存信息是一条 `post_send`；数据是 `post_write()` / `post_read()`；协调消息（`'W'` `'K'` `'Q'`）是 `post_send` / `post_recv` |
| **网络路径层** | **同一条路径，但用的 QP 不同**：建连和数据都走同一块网卡、同一个端口、同一对 fabric IP（经 VRF 与网关，§6）；建连报文走 **GSI QP（QP1）**（上表），数据走建好的 **RC QP**（`rdma res show` 里是另一个 QP 号，如 `lqpn 2690`）。OOB 版则是建连走 `eth0` 的 TCP（另一张网），GSI 不变 |
| **连接建好之后** | **不分开**：本篇 CM 版脚本里的协调消息（内存信息、`'W'` `'K'` `'Q'`）用 RDMA SEND / RECV 发，与 WRITE / READ **在同一个 RC QP 上**，只能靠操作类型（SEND 还是 WRITE / READ）和程序含义区分。OOB 版则是协调消息在 TCP、数据在 RDMA，界线更清楚 |

**没验证的**：
- client 每次 +3、server 每次 +2，**推断**对应 CM 握手里的几类消息（client 发 REQ、RTU 与断开请求，server 发 REP 与断开应答），但每个增量对应哪条消息，没有抓包验证（Pod 里没有能抓 RDMA 报文的工具）；
- "RoCE v2 上 `rdma_cm` 的报文经 GSI QP 发出"是公开资料的说法，上表只证明了"CM 版有 GSI 流量、OOB 版没有"。

<a id="6"></a>
## 6. VRF：fabric 网口各自独立的路由表

在本集群上 `client_cm_cpu.py` **必须**给 `--src`，不给就 `Errno: 101, Network is unreachable`。原因是每块 fabric 网卡都挂在自己独立的 **VRF** 上。VRF 的通用说明见 [rdma.md §2.7](rdma.md#2)。

### 6.1 现状（**实测**，server-0 的 Pod 内）

```
fabric0  master vrf100   vrf table 100        fd02:0:400:52e::/64（网段）
fabric2  master vrf102   vrf table 102        fd02:0:500:52e::/64（网段）
fabric4  master vrf104   vrf table 104        fd02:0:600:52e::/64（网段）
fabric6  master vrf106   vrf table 106        fd02:0:700:52e::/64（网段）
fabric8  master vrf108   vrf table 108        fd02:0:300:52e::/64（网段）
fabric10 master vrf110   vrf table 110        fd02:0:200:52e::/64（网段）
fabric12 master vrf112   vrf table 112        fd02:0:100:52e::/64（网段）
fabric14 master vrf114   vrf table 114        fd02:0:0:52e::/64（网段）
```

- **每个 VRF 的路由表只有两条**（以 `fabric0` / 表 100 为例）：`fd02:0:400:52e::/64 dev fabric0`（本网口所在 /64 的直连路由）；`fd02::/24 via fd02:0:400:52e::2 dev fabric0 onlink`（其余 `fd02::/24` 一律经网关 `…::2`，即交换机）。
- `ip rule` 里有 `1000: from all lookup [l3mdev-table]`：流量属于某个 VRF 时，改去该 VRF 的路由表查。
- **主路由表里没有任何 `fd02::` 路由**，所以 `ip -6 route` 只能看到 `eth0` 上的 `fe80::/64`。
- `eth0`（Pod 网络）在主表里，**不受 VRF 影响**。
- NAD 里只写了 `ip-preserving-host-device` 与设备名，没有 VRF 配置；VRF 是谁创建的，本篇**没有查**。
- `fabric0` 的 netdev MTU 是 **4200**，大于 RoCE 的 `active_mtu`（4096）。按 IPv6 40 + UDP 8 + BTH 12 + 载荷 4096 + ICRC 4 估算约 4160，小于 4200（**估算**，非实测）。

### 6.2 实验（**实测**）

| 操作 | 结果 |
|---|---|
| `ip -6 route get fd02:0:0:527::1`（不指定出接口） | `Network is unreachable` |
| 同上，加 `oif fabric0` 或 `vrf vrf100` | `via fd02:0:400:52e::2 dev fabric0 table 100 … src fd02:0:400:52e::1` |
| `ping6 fd02:0:0:527::1` | `Network is unreachable` |
| `ping6 -I fabric0 fd02:0:0:527::1` | 2/2 成功，约 0.14 ms |
| CM client 不给 `--src` | `Failed to create CM ID. Errno: 101, Network is unreachable` |
| CM client 给 `--src fd02:0:400:52e::1` | 成功 |
| 邻居表（server-0 `fabric0`，运行过连接之后） | 只有网关 `fd02:0:400:52e::2`（标记 router）与其链路本地地址；**没有对端主机**。server-1 的 `fabric14` 同样只有网关 `fd02:0:0:527::2` |

### 6.3 对 RDMA 的影响

| 方面 | 影响 |
|---|---|
| **数据面**（QP 建好之后的收发） | **无影响**：报文由网卡直接收发，不再查内核路由 |
| **按 IP 找设备 / 解析路由**（`rdma_cm`、普通 socket、`ping`） | **必须告诉内核出接口 / 源地址**：主表里没有 fabric 路由，不指定就 `Network is unreachable`。`--src` / `--bind` 正是起这个作用——绑定到 fabric 网口的 IP，内核就在该网口所在 VRF 的表里查 |
| **RoCE 报文的下一跳** | 到对端是**经网关的 L3 路由**；邻居表里只有网关，据此**推断**报文的目的 MAC 是网关的 MAC（未抓包） |
| **多块网卡之间** | 每块网卡一张独立的路由表，从 `fabricN` 出去的流量只走它自己那张表——相当于 rail 隔离（动机是**推测**） |
| **普通网络工具** | `ping`、`curl`、`ssh` 等要用 `-I fabricN` 或 `ip vrf exec vrfNNN …`；Pod 的默认网络（`eth0`）不受影响 |
| **OOB 版** | OOB 的 TCP 走 `eth0`，不经过 VRF；RDMA 一侧不靠 IP 找路由，而靠 `--gid-index`（源 GID 属于 `fabric0`）加上 `AHAttr` 里的 `dgid`，在主表无路由的情况下仍然建连成功。**推断**是内核按源 GID 所属网口解析下一跳，**没有读内核代码验证** |

### 6.4 代码里怎么处理

| | 做法 |
|---|---|
| **OOB 版** | 不给 IP 路由；`--dev` 选设备、`--gid-index 3` 选源 GID、`--tc` 与 `hop_limit=64` 写进地址向量。OOB 的 TCP 用 `eth0`。**不需要**为 VRF 做额外处理 |
| **CM 版 server** | `--bind <fabric 网口 IP>`：监听在 fabric 网口的地址上，天然落在对应的 VRF 里 |
| **CM 版 client** | `--src <本端 fabric 网口 IP>`：**必填**。脚本捕获 `PyverbsRDMAError`，打印"创建 CM 端点失败"和提示（要用 fabric 网口 IP、`--src` 必填、用 `ip -br -6 addr` 与 `rdma link` 查看），再原样抛出；`--src` 的 `help` 与脚本开头注释也都说明了 VRF |

### 6.5 排错命令

```bash
ip -d link show type vrf                      # 有哪些 VRF、各用哪张路由表
ip -d link show fabric0 | grep -E "master|vrf_slave"   # 网口属于哪个 VRF
ip -6 route show table 100                    # 某个 VRF 的路由表
ip -6 route get <对端 IP> oif fabric0         # 指定出接口后的选路结果
ping6 -I fabric0 <对端 IP>                    # 经指定网口 ping（VRF 下必须）
ip -6 neigh show dev fabric0                  # 邻居表：下一跳（这里是网关）的 MAC
rdma link                                     # RDMA 设备 ↔ 网口 对应关系
rdma res show cm_id                           # 当前 CM 连接用的设备与地址
```

<a id="7"></a>
## 7. 实测与接口差异

### 7.1 正确性与重复性

每次运行都重新启动 server，全部成功：client 退出码 0，server 在内存里看到 client 写入的内容，client 读回的内容与 server 预置的一致。

| 版本 | 运行次数 | 计时的几次，整条 client 命令耗时 |
|---|---|---|
| OOB 版 | 5 次：首次运行（未计时）+ 3 次计时重复 + 1 次 client 端 `hop_limit` 用默认值的对照（§7.2） | 约 0.90、0.88、0.89 s；对照那次约 0.88 s |
| CM 版 | 4 次：首次运行（未计时）+ 3 次计时重复 | 约 0.90、0.93、0.90 s |

"耗时"是从 nuc 发起 `kubectl exec` 到 client 退出的整条命令，含 `kubectl` 与 Python 启动、导入 pyverbs、建连、一次 WRITE、一次 READ，**不是** RDMA 操作本身的延迟，不能当性能数据。运行结束后 server 进程已自行退出，没有残留。给脚本加详细注释之后，两个版本各重新跑过一次，结果不变。

### 7.2 `hop_limit`：默认值（1）在这里也能用

RoCE v2 的地址向量里有 `hop_limit`（IP 头的 TTL）。OOB 版显式设 64（与 C 版一致）。pyverbs 的 `GlobalRoute` **默认 `hop_limit = 1`**（**实测**：`GlobalRoute(dgid=…, sgid_index=3).hop_limit` 返回 1）。

对照实验：只把 **client 一端**的 `hop_limit=64` 去掉（用默认的 1），server 一端仍是 64。**结果：WRITE 与 READ 仍然全部成功**（§7.1 里 OOB 版的那次对照运行）。所以：

- **在本集群上，"必须设 `hop_limit=64`"不成立**——至少 client → server 方向用 1 没有问题；
- **原因未查明**：§6 的路由表显示，到对端走的是经网关的 L3 路由（`fd02::/24 via …::2`），所以"rail 内二层互通"这一解释**不成立**；是否因为网关 / 交换机不递减该字段，**没有验证**；
- **未测**：**两端都用默认值**的情形（READ 的数据由 server 端 QP 发出，server 方向的 `hop_limit` 在本实验里仍是 64）；
- 脚本里仍显式设 64：对不同网络环境更稳妥。

### 7.3 为了跑通，踩到的接口差异（**实测**，rdma-core 39 / pyverbs 39.0-1）

| 现象 | 原因 / 处理 |
|---|---|
| `from pyverbs.device import Context` 报 `No module named 'pyverbs.device'` | 用了默认 `python3`（3.12），只有 3.10 的二进制模块；改用 `python3.10`（§1.6） |
| `from pyverbs.libibverbs_enums import …` 报 `ModuleNotFoundError` | 这个版本的 enum 在 **`pyverbs.enums`** |
| `from pyverbs.librdmacm_enums import …` 报 `ModuleNotFoundError` | CM 相关枚举（`rdma_port_space`、`RAI_PASSIVE` 等）在 **`pyverbs.cm_enums`** |
| `GID("fd02:0:400:52e::1")` 报 `Invalid GID value` | GID 字符串必须写成**完整 8 段**，不能用 `::` 缩写；脚本用 `str(gid)` 传递，天然是完整形式（`AddrInfo` 的 IP 参数则可以用缩写） |
| `AHAttr(..., grh=gr)` 报 `unexpected keyword argument 'grh'` | 参数名是 **`gr=`**（文档字符串里写的 `grh` 与实际不符） |
| 原示例用 `client.dev_name` / `client.port_num` | 本版 `CMID` **没有**这两个属性（`hasattr` 为 False），只有 `qpn`；要看用的哪块网卡，用 `rdma res show` |
| `CMID.pd` 拿不到设备名 | `PD` 对象没有 `ctx` / `context` 属性 |
| `rdma_cm` 的 TOS 选项 | 本版 `cm_enums` 只暴露了 `RDMA_OPTION_ID` 与 `RDMA_OPTION_ID_ACK_TIMEOUT`，**没有** TOS 常量 |
| `inspect.signature` 对 pyverbs 的类报 `no signature found` | 它们是 Cython 编译的，用 `help()` / `__init__.__doc__` 看参数 |

<a id="8"></a>
## 8. 与原有 pyverbs 示例、与 C 版的差异

### 8.1 与仓库里原有的 `client_oob.py` / `server_oob.py` / `client_cm.py` / `server_cm.py`

原脚本（见 [soft-roce-local.MD](soft-roce-local.MD)）在 Soft-RoCE 环境、较新的 rdma-core 上写成，用的是 SEND / RECV。本篇脚本的改动：

| 项 | 原脚本 | 本篇脚本 | 原因 |
|---|---|---|---|
| enum 导入 | `libibverbs_enums` / `librdmacm_enums` | `pyverbs.enums` / `pyverbs.cm_enums` | 这个版本里原模块不存在（§7.3） |
| 设备 / GID 下标 | `rxe_eth1` / 1 | `mlx5_*` / 3 | 本集群的设备名；`gid[1]` 是 `fe80::` 链路本地，不能跨机 |
| 地址 | 写死的 IP | `--server` / `--bind` / `--src` 参数 | 不同环境的 IP 不同；CM 版还要用 fabric 网口的 IP |
| 操作 | SEND / RECV | **RDMA WRITE + READ** | 本篇要演示"访问对端内存" |
| QP 权限（OOB 版） | 默认 | 开 `REMOTE_WRITE` / `REMOTE_READ` | 单边操作需要 |
| `path_mtu`（OOB 版） | 固定 1024 | 取端口 `active_mtu`（4096） | 与网卡能力一致 |
| `traffic_class` / `hop_limit`（OOB 版） | 未设置 | 显式设置（104 / 64） | 与 C 版一致；见 §7.2 |
| CM 的 `dev_name` / `port_num` | 使用 | 不使用 | 本版没有这两个属性 |

### 8.2 与 C 版（[c-multi-node-cpu.md](c-multi-node-cpu.md)）

| | pyverbs 版（本篇） | C 版 |
|---|---|---|
| 依赖 | `python3-pyverbs`（再往下是 libibverbs 等） | `libibverbs` |
| 内存 | `MR(pd, size, access)` 一步分配并注册 | `posix_memalign` + `ibv_reg_mr`，注册前先写满每页 |
| 内容 | 40–53 字节的字符串，演示用 | 64 MiB 区域，`unsigned` 模式，带校验与带宽测试 |
| 带宽测试 / 诊断选项 | **无** | 有（`-n` `-w` `-o` `-W`） |
| 建连 | OOB + 手工 `modify_qp`（OOB 版）；`rdma_cm`（CM 版） | OOB + 手工 `modify_qp` |
| OOB 监听地址 | `0.0.0.0` | 只绑 `eth0` 的 IPv4 |
| 适用 | 看懂流程、快速验证环境 | 量带宽、诊断 |

**注意**：Python 版**不能用来测带宽**——只传几十字节，且每次运行都包含 Python 启动、`kubectl exec`、建连等开销。

<a id="9"></a>
## 9. 坑

**通用**

| # | 坑 / 注意 | 说明 |
|---|---|---|
| 1 | 默认 `python3`（3.12）导入不了 pyverbs 的子模块 | 顶层 `import pyverbs` 会成功，容易误以为装好了；`from pyverbs.device import …` 才失败。要用 `python3.10`（§1.6） |
| 2 | Pod 重建后 pyverbs 与脚本都没了 | 装在容器层；重建后重装并重新拷贝（§2） |
| 3 | 输出重定向到文件时日志是空的 | **实测遇到**：Python 在输出不是终端时会缓冲，server 的"监听"行要等进程结束才出现，脚本里轮询日志会白等到超时。运行时加 **`python3.10 -u`** |
| 4 | server 要先启动 | server 一次性：处理完一个 client 就退出；client 没有重试，连不上就报错 |
| 5 | `kubectl exec` 起后台进程会卡住 | 把 `kubectl exec … python3.10 server_*.py` 整条命令放进 nuc 的后台，再轮询日志（同 [c-multi-node-cpu.md §6](c-multi-node-cpu.md#6) 坑 8）；清理残留进程时**不要用 `pkill -f`**，它会匹配到远端 shell 自己的命令行，可以用 Pod 里的 `timeout` 兜底 |
| 6 | 只能用偶数号网卡 | 本环境 yaml 只申请了 `rdma/fabric0/2/…/14`，Pod 里只有偶数号 `uverbs`（见 [c-multi-node-cpu.md §1.2](c-multi-node-cpu.md#1-2)） |
| 7 | WRITE 对 server 是"无声"的 | server 不知道数据已到，必须另外通知它去看（OOB 的 `'W'` / CM 的 SEND `'W'`） |
| 8 | `wr_id` 只是个标签 | 原样出现在完成项里，用来辨认是哪一次操作 |

**OOB 版**

| # | 坑 / 注意 | 说明 |
|---|---|---|
| 9 | GID 下标要选 RoCE v2 的非链路本地项 | 本集群是 3；用 `ctx.query_gid(1, i)` 与 `query_gid_type(1, i)` 查（类型 1 才是 RoCE v2，`fe80::` 不能跨机） |
| 10 | GID 字符串要完整 8 段 | 不能写 `::` 缩写（§7.3） |
| 11 | QP 和 MR 的权限都要开 | server 的 QP 要 `REMOTE_WRITE` / `REMOTE_READ`，MR 也要，对端的 WRITE / READ 才被接受 |
| 12 | `--server` 填 server 的 `eth0` IP | OOB 的 TCP 走 `eth0`，不是 fabric 地址 |

**CM 版**

| # | 坑 / 注意 | 说明 |
|---|---|---|
| 13 | 地址要用 fabric 网口的 IP | 填 `eth0` 的 IPv4 报 `Errno: 19, No such device`（**实测**）。用 `ip -br -6 addr` 看 `fabricN` 的地址，`rdma link` 看 `mlx5_N ↔ fabricN` |
| 14 | client 一定要给 `--src` | fabric 网口挂在 VRF 上，不给会 `Errno: 101, Network is unreachable`（**实测**，§6）；普通 `ping6` 同理，要 `ping6 -I fabric0 …` |
| 15 | 先挂接收缓冲，再 `accept()` / `connect()` | 否则对端的第一条 SEND 没处可收 |
| 16 | 接收完成项按挂的顺序返回 | 脚本靠顺序区分"内存信息"与"确认"；改了挂的顺序就要同步改 |

<a id="A"></a>
## 附录. 验证状态汇总

**实测** = 在 server-0 / server-1 上跑出来的；**推断** = 由机制或公开资料推导；**未测 / 未查明** = 没有证据，或有现象没有原因。

**运行与正确性**

| 结论 | 状态 |
|---|---|
| OOB 版（5 次，含 1 次 `hop_limit` 对照）与 CM 版（4 次）在 server-0 / server-1 上运行全部成功、退出码 0 | **实测** |
| WRITE：server 看到 client 写入的 40 字节；READ：client 读回 server 预置的 53 字节 | **实测** |
| 为脚本加详细注释后，两个版本各重新跑过一次，结果不变；注释前后代码逻辑一致（AST 对比，仅 `help` 文字、一处未用 import、一处元组赋值写法不同） | **实测** |
| 单边操作期间 server 的 CPU 不参与 | **推断**（单边操作机制）；本篇未测 CPU 占用（主机内存 C 版测过，约 1%） |
| 输出被重定向时 Python 缓冲导致日志延迟出现，`-u` 可避免 | **实测** |

**pyverbs 与系统依赖**

| 结论 | 状态 |
|---|---|
| `python3-pyverbs 39.0-1` 的依赖声明、文件内容、`ldd` 链接关系；`libibverbs1` / `librdmacm1` / `ibverbs-providers` / `rdma-core` 均为 39.0-1 | **实测** |
| 默认 `python3`（3.12）只能导入顶层包、导入不了 `pyverbs.device` 等子模块；`python3.10` 可用 | **实测** |
| 打开设备前后进程里的库、fd、对 `uverbs0` 的内存映射（3 个 → 4 个）；`libmlx5-rdmav34.so` 是指向 `libmlx5.so.1.22.39.0` 的符号链接；`/etc/libibverbs.d` 有 15 个文件、`mlx5.driver` = `driver mlx5` | **实测** |
| CM 版额外打开 `/dev/infiniband/rdma_cm`；`uverbs` ABI 版本 6、PCI 驱动 `mlx5_core`、固件 40.47.2526 | **实测** |
| libibverbs 初始化时建 `NETLINK_RDMA` 套接字并收发；随后对 `/dev/infiniband/uverbs19…0` 逐个 `stat`；读 `/etc/libibverbs.d/` 的 15 个 `.driver` 文件并加载 provider 插件；整个追踪里读 `/sys` 的次数为 0 | **实测**（`strace`） |
| 打开 `uverbs0` 一次；之后 `RDMA_VERBS_IOCTL` 共 22 次、对该 fd 的 `write()` 为 0 次；`mmap` 共 4 个 4096 字节页（偏移 0x500000 / 0x700000 只读，0x900000 / 0x901000 可写） | **实测**（`strace`） |
| WRITE / READ 的发起与完成轮询期间，没有 `RDMA_VERBS_IOCTL`（数据面不进内核） | **实测**（真实 `client_oob_cpu.py` 的 `strace`） |
| 对 `uverbs0` 的可写映射是网卡门铃页，`post_send` 经它通知网卡、`poll` 读 CQ 内存，使热路径不进内核 | **推断**（通用机制） |
| netlink 请求类型 `0x1401` 对应 RDMA nldev 的设备查询 | **推断**（按公开资料的常量） |
| `ld.so` 先试 `/usr/local/cuda/lib64`（`ENOENT`）再命中 `/lib/x86_64-linux-gnu`，原因是 `LD_LIBRARY_PATH` | **推断**（搜索顺序实测，原因未核对环境变量） |
| mlx5 provider 初始化期间（约 380 行系统调用）具体做了什么；CM 版的系统调用序列 | **未查 / 未追踪** |

**接口差异**

| 结论 | 状态 |
|---|---|
| `libibverbs_enums` / `librdmacm_enums` 不存在，枚举在 `pyverbs.enums` / `pyverbs.cm_enums`；`AHAttr` 的参数是 `gr=`；GID 字符串须完整 8 段；`CMID` 无 `dev_name` / `port_num`；`cm_enums` 无 TOS 常量 | **实测** |
| `GlobalRoute` 默认 `hop_limit = 1` | **实测** |

**地址、设备、建连路径与 VRF**

| 结论 | 状态 |
|---|---|
| GID 表：下标 0/1 为 `fe80::`、2/3 为 `fd02:…`，类型 1/3 为 RoCE v2；端口 `active_mtu` = 5（4096） | **实测** |
| fabric 网口的 IPv6 地址：server-0 `fabric0` = `fd02:0:400:52e::1`、server-1 `fabric14` = `fd02:0:0:527::1`；`rdma link` 显示 `mlx5_0 ↔ fabric0` | **实测** |
| CM 版实际使用的设备：client `mlx5_0/1`、server `mlx5_14/1`，QP 类型 RC（`rdma res show`） | **实测** |
| CM 版填 `eth0` 的 IPv4 报 `Errno: 19, No such device`；不给 `--src` 报 `Errno: 101, Network is unreachable`，给了则成功 | **实测** |
| `rdma_cm` 依据 IP 找 RDMA 设备（IP → netdev → RDMA 设备） | **推断**（机制；与 `rdma res show` 的结果一致） |
| 每跑一次 CM 版，client 的 GSI QP `sq-psn` +3、server 的 +2（两次一致）；跑 OOB 版则不变 | **实测** |
| 上述增量对应 CM 握手的 REQ / REP / RTU / 断开等消息；`rdma_cm` 报文经 GSI QP 发出 | **推断** / 公开资料（未抓包） |
| CM 版建连后的协调消息与数据在同一个 RC QP 上（脚本设计，SEND / RECV 与 WRITE / READ 共用） | 代码事实 |
| server-0 上 `fabricN` 各属于一个 VRF（`fabric0` → `vrf100` → 表 100 … `fabric14` → `vrf114` → 表 114）；每张表只有本网口 /64 与 `fd02::/24 via …::2 onlink`；主表无 `fd02::` 路由 | **实测**（server-0） |
| `ip -6 route get` 不指定出接口 → `Network is unreachable`；指定 `oif fabric0` 或 `vrf vrf100` → 走网关；`ping6` 须 `-I fabric0` | **实测** |
| 邻居表里只有网关、没有对端主机；因此 RoCE 报文的目的 MAC 应是网关的 MAC | 邻居表**实测**；"目的 MAC 是网关"为**推断**（未抓包） |
| OOB 版靠 GID 下标建连，在主表无路由的情况下仍成功，推断是内核按 GID 所属网口解析 | **推断**（现象实测，机制未查内核代码） |

**`hop_limit`、未查明与未测**

| 项目 | 状态 |
|---|---|
| 仅 client 一端用默认 `hop_limit`（1）时 WRITE / READ 仍成功 | **实测** |
| 为什么默认 `hop_limit = 1` 仍然可用；两端都用默认值的情形 | **未查明** / **未测** |
| VRF 由谁创建；server-1 上是否同样的 VRF 布局（仅看到 `fabric14` 的网关邻居）；NCCL 在 VRF 下的行为 | **未查 / 未测** |
| CM 版 `traffic_class`（默认 0）；是否能用 `set_option` 设 TOS；不带 `IBV_SEND_SIGNALED` 是否仍有完成项 | **未测** |
| 非默认 `traffic_class`（如 0）下的 OOB 版行为；SEND / RECV 之外的 CM 事件处理；多 QP；错误路径（权限不足、rkey 错误）；带宽与延迟 | **未测** |

---

*环境：server-0 与 server-1（StatefulSet `server` 的两个 Pod，`n-server2.yaml`，各 8×B300，RoCE v2 / 400 Gb/s，Ubuntu 22.04，rdma-core 39，python3-pyverbs 39.0-1，Python 3.10）；实验日期 2026-11-01；代码：`pyverbs-example/server_oob_cpu.py`、`client_oob_cpu.py`、`server_cm_cpu.py`、`client_cm_cpu.py`，均含中文注释。*
