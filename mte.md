# MTE（Mooncake Transfer Engine）

MTE 是 Mooncake 项目里的**数据传输库**：在两台机器之间，用 RDMA（或 TCP、NVLink 等）直接搬运 **CPU 内存或 GPU 显存**里的数据，零拷贝，带多网卡、拓扑感知和故障重试。本篇记录它的简介、在本集群上怎么装、它依赖哪些系统软件、两个实验（CPU 内存、跨节点 GPU 显存），以及与 pyverbs 的对照。

参考资料：[MTE Python API](https://kvcache-ai.github.io/Mooncake/api-reference/python/transfer-engine.html)（旧地址 `python-api-reference/transfer-engine.html` 会自动跳转到这里）、[MTE Architecture](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/index.html)。

**状态**：进行中。2026-11-01 在 server-0 / server-1 上完成安装与两个实验；后续实验见 [§7](#7)。

**可靠度标记**：**实测** = 在本集群跑出来的；**文档** = 来自上面两篇官方文档，本集群未验证；**推断** = 由日志、字符串或机制推导；**未测** = 没有证据。

**实验环境**：本篇所有实验都基于 [`n-server2.yaml`](n-server2.yaml) 部署的环境（与 [py-multi-node-cpu.md](py-multi-node-cpu.md) 相同）：

| 项 | 内容（来自 yaml；Pod 状态为 **实测**） |
|---|---|
| 工作负载 | StatefulSet `server`，`replicas: 2`，得到 `server-0`（client 一侧）、`server-1`（server 一侧） |
| 镜像 | `vllm/vllm-openai:cu130-nightly`（yaml 第 50 行，`imagePullPolicy: Always`；滚动标签，Pod 重建后可能拉到不同的镜像） |
| 每个 Pod 的资源 | `nvidia.com/gpu: 8`（8×B300）；`rdma/fabric0`、`fabric2`、…、`fabric14`（只有偶数号，所以 Pod 里只有偶数号的 RDMA 设备） |
| 权限 | `IPC_LOCK`（注册内存锁页）、`SYS_PTRACE` |
| 本次的 eth0 地址 | server-0 为 10.133.1.12，server-1 为 10.133.0.253（Pod 重建后可能变，脚本和 `launch.json` 里的 IP 要同步改） |

yaml 启动时用 apt 装了 `ibverbs-utils`、`infiniband-diags` 等工具（带来 `libibverbs1` 等系统库）；MTE 不在其中，是**我们另外装的**（[§2](#2)），装在容器层，Pod 重建后会丢。

<a id="toc"></a>
## 目录

- [摘要](#sum)
- [1. MTE 简介](#1)
  - [1.1 两个核心抽象](#1-1)　[1.2 比裸 verbs 多做了什么](#1-2)　[1.3 适合与不适合](#1-3)　[1.4 Python 接口速览](#1-4)　[1.5 `register_memory` 接受什么](#1-5)
- [2. 安装与对系统软件的依赖](#2)
  - [2.1 官方写法与不能照搬的原因](#2-1)　[2.2 本集群的装法](#2-2)　[2.3 验证](#2-3)　[2.4 对系统软件的依赖](#2-4)
- [3. 实验 1：CPU 内存 WRITE](#3)
  - [3.1 流程](#3-1)　[3.2 运行](#3-2)　[3.3 结果](#3-3)　[3.4 代码讲解](#3-4)　[3.5 相对原版脚本的改动](#3-5)
- [4. 实验 2：跨节点 GPU 显存 → GPU 显存](#4)
  - [4.1 GPU 与网卡要就近](#4-1)　[4.2 运行](#4-2)　[4.3 与 CPU 版的差异](#4-3)　[4.4 结果](#4-4)　[4.5 没验证的](#4-5)
- [5. 与 pyverbs 的对照](#5)
  - [5.1 与 PyTorch 集成的优势](#5-1)　[5.2 NVLink 支持情况](#5-2)　[5.3 限制](#5-3)
- [6. 没验证的汇总](#6)
- [7. 后续实验](#7)

<a id="sum"></a>
## 摘要

[↑ 返回目录](#toc)

| 问题 | 结论 | 可靠度 |
|---|---|---|
| MTE 是什么？ | 数据传输库：Segment / Buffer / BatchTransfer 三个抽象，内置拓扑感知选路、多网卡切片、按需建连、故障重试；不是集合通信库 | **文档**（[§1](#1)） |
| 官方 `pip install mooncake-transfer-engine` 在本集群能用吗？ | **不能**：镜像自带的是 CUDA 12 构建，导入报缺 `libcudart.so.12`，Pod 里只有 CUDA 13；要装 **`mooncake-transfer-engine-cuda13`** | **实测**（[§2.1](#2-1)） |
| 怎么装？ | `pip install --no-deps --target /vllm-workspace/mte13 mooncake-transfer-engine-cuda13 msgpack`（Python 3.12，两个 Pod 各一次），运行时设 `PYTHONPATH=/vllm-workspace/mte13` | **实测**（[§2.2](#2-2)） |
| 依赖哪些系统软件？ | RDMA 用户态库（`libibverbs`、`libmlx5`）、CUDA 运行时与驱动（`libcudart.so.13`、`libcuda.so.1`）、uverbs / netlink / `/sys` / `/dev/nvidia*`；glog、liburing 等 wheel 自带 | **实测**（[§2.4](#2-4)） |
| CPU 内存经 RDMA 跨 Pod 写能跑通吗？ | **能**：1 MiB，3 次全部通过，走 RDMA，GID 自动选 3，不需要 `--src`（VRF 没造成问题） | **实测**（[§3](#3)） |
| 跨节点 GPU 显存 → GPU 显存呢？ | **能**：GPU0（server-0）→ GPU7（server-1），64 MiB 与 1 GiB 都通过；`register_memory` / `transfer_sync_write` 的调用与 CPU 版完全相同，只是传显存地址 | **实测**（[§4](#4)） |
| `register_memory` 接受什么？ | **整数地址**：ctypes 用 `ctypes.addressof`、tensor 用 `data_ptr()`；直接传对象会 `TypeError` | **实测**（[§1.5](#1-5)） |
| 相对 pyverbs，与 PyTorch 集成有什么优势？ | MTE 能与 PyTorch 同进程（都在 Python 3.12），pyverbs 不能（只有 3.10 的二进制）；MTE 把建连、选网卡、GID 都藏起来了 | **实测**（[§5.1](#5-1)） |
| 支持 NVLink 吗？ | 构建里带有节点内 NVLink（`SUPPORT_INTRA_NVLINK = True`），**未运行过**；跨节点 NVLink（MNNVL）本构建不支持，本集群节点间是 RoCE | **推断**（字符串）/ **未测**（[§5.2](#5-2)） |
| `initialize` 为什么要 4.5 秒？ | 进程里没初始化 CUDA 时约 4.5 s，已用 PyTorch 初始化过则约 0.01 s：这是 MTE 首次初始化 CUDA 的耗时 | 对照**实测**，因果为**推断**（[§4.4](#4-4)） |

<a id="1"></a>
## 1. MTE 简介

[↑ 返回目录](#toc)

<a id="1-1"></a>
### 1.1 两个核心抽象（**文档**）

| 抽象 | 含义 |
|---|---|
| **Segment** | 可被远端读写的一段地址空间：RAM Segment（DRAM、VRAM）或 NVMeof Segment。每个进程启动时自动建一个以 `local_hostname` 命名的 segment（要求全局唯一），别的进程用 `openSegment(名字)` 引用 |
| **Buffer** | segment 内**真正注册**给（GPUDirect）RDMA 的连续区域。每个 buffer 有独立的权限（对应 rkey）和网卡亲和性；读写请求必须落在某个有效 buffer 内 |
| **BatchTransfer** | 一批 READ / WRITE 请求（操作类型、长度、本端地址、远端地址），异步提交，用状态接口查完成 |

传输后端由 `Transport` 类实现：TCP、RDMA、EFA、NVMeoF、NVLink、IntraNodeNvlink、HIP、SHM。引擎启动时自动发现 CPU / CUDA 与 RDMA 设备的拓扑，并据此安装后端。

<a id="1-2"></a>
### 1.2 比裸 verbs 多做了什么（**文档**）

| 我们用 pyverbs / C 手工做的 | MTE |
|---|---|
| 用 OOB 的 TCP 或 `rdma_cm` 交换 `qpn` / `gid` / `addr` / `rkey` | **元数据服务**（etcd / redis / http），或 `P2PHANDSHAKE`（点对点握手，不需要独立服务）；segment 的 buffer、rkey、设备、GID 都放在元数据里 |
| 用 `--dev` 手工选网卡 | **拓扑感知选路**：每个节点生成拓扑矩阵并广播，按内存类型把网卡分成 preferred / secondary 两个列表，正常用 preferred，故障时才用 secondary |
| 一个请求用一个 QP | 大于 64 KB 的请求切片，不同切片可走不同网卡，多卡并行 |
| 建好的连接一直占着 | 按需建连 + endpoint 池（有上限，SIEVE 淘汰），出错的连接被移除，下次传输时重建 |
| 出错就报错退出 | 某张网卡不可用时自动换一张可达的重提；也会避开出问题的 RDMA context 和 CQ |
| 手工轮询 CQ | 内部多线程 worker，每个设备实例有自己的线程和 CQ |

<a id="1-3"></a>
### 1.3 适合与不适合

- 适合：**点对点、大块、可能一对多**的数据搬运，如 KV Cache 传输（vLLM、SGLang 的 PD 分离）。
- 不是集合通信库：没有 all-reduce / all-gather 这类语义，集合通信仍用 NCCL。
- 与 pyverbs 的关系：pyverbs 是对 verbs 的一层薄绑定，展示一条 verbs 流水线（[py-multi-node-cpu.md](py-multi-node-cpu.md)）；MTE 是在这条流水线外面包了拓扑、多网卡、元数据与容错的运行时。与 PyTorch 集成时的差别见 [§5.1](#5-1)，NVLink 见 [§5.2](#5-2)。

<a id="1-4"></a>
### 1.4 Python 接口速览（**文档**）

| 类别 | 接口 |
|---|---|
| 初始化 | `initialize(local_hostname, metadata_server, protocol, device_name)`，返回 0 成功；`initialize_ext(..., metadata_type)`；`get_rpc_port()` |
| 内存注册 | `register_memory(ptr, len)` / `unregister_memory(ptr)`；批量 `batch_register_memory`；文档里标注 `register_memory` 为 experimental |
| 同步传输 | `transfer_sync_write(目标, 本端地址, 对端地址, 长度)`、`transfer_sync_read(...)`、`transfer_sync(..., opcode, notify)` |
| 异步传输 | `transfer_submit_write(...)` 返回 batch id → `transfer_check_status(id)`（1 完成、0 进行中、-1 失败、-2 超时） |
| 批量 | `batch_transfer_sync_write/read`、`batch_transfer_async_*`、`get_batch_transfer_status` |
| CUDA stream | `transfer_write_on_cuda` / `transfer_read_on_cuda` 等（传 `stream_ptr`） |
| 通知 | `TransferNotify(name, msg)` 作为 `transfer_sync` 的参数；对端用 `get_notifies()` 取 |
| 其他 | `allocate_managed_buffer`、`get_local_topology`、`send_probe`、编译能力属性 `engine.SUPPORT_CUDA` 等 |

地址一律是**整数指针**（见 [§1.5](#1-5)）；对端的 buffer 地址要**自己带外传递**。常用环境变量（**文档**）：`MC_GID_INDEX`（不设则自动选 GID）、`MC_IB_TC`（RDMA traffic class）、`MC_TE_FILTERS`（网卡白名单）、`MC_RDMA_BIND_ADDRESS`（TCP 握手与 RDMA 用不同地址时）、`MC_TRANSFER_TIMEOUT`（默认 30 秒）等，完整列表见官方架构页。

<a id="1-5"></a>
### 1.5 `register_memory` 接受什么（**实测**）

`register_memory(buffer_addr, capacity)` 要的是**整数地址**，不接受 ctypes 对象或 PyTorch tensor 本身；两者都能用，但要先取出地址再传。在 server-0 上各试一次（用 `tcp` 协议，不需要网卡，只验证接口接受什么类型）：

| 传的东西 | 结果 |
|---|---|
| ctypes 数组对象本身 | **`TypeError`**：参数类型不兼容 |
| `ctypes.addressof(arr)` | 返回 0，成功 |
| PyTorch GPU 张量对象本身 | **`TypeError`**：参数类型不兼容 |
| `tensor.data_ptr()`（GPU 张量） | 返回 0，成功 |
| `tensor.data_ptr()`（CPU 张量） | 返回 0，成功 |

```python
engine.register_memory(ctypes.addressof(arr), n)     # ctypes：先取地址
engine.register_memory(tensor.data_ptr(), n)          # tensor：data_ptr()，GPU、CPU 张量都行
```

- 官方文档的签名也是两个整数参数；文档例子里用 numpy 的 `array.ctypes.data` 取地址（这个写法**没有测**）。
- 返回 0 只说明**接口接受了**这个地址；数据能否真正传过去，CPU 内存见 [§3](#3)，GPU 显存见 [§4](#4)。
- 注册的是**地址范围**，对应的内存要在注册期间一直保持存活，传输完成、`unregister_memory` 之后才能释放；tensor 被移动或重新分配后地址就失效。这是一般的机制说法，**没有**为 MTE 单独验证（实验里的脚本都保持引用到最后才注销）。

<a id="2"></a>
## 2. 安装与对系统软件的依赖

[↑ 返回目录](#toc)

<a id="2-1"></a>
### 2.1 官方写法，以及在本集群上为什么不能照搬

官方写法：

```bash
pip install --no-cache-dir --break-system-packages mooncake-transfer-engine
```

在本集群的 Pod 里**不能照搬**（**实测**）：

- Pod 用的是 vLLM 官方镜像，里面**已经装了** `mooncake-transfer-engine 0.3.10.post2`（两个 Pod 一样）。**注意**：`0.3.10.post2` 是 Mooncake 自己的版本号，与 Python 3.10 无关；它装在 Python 3.12 的目录 `/usr/local/lib/python3.12/dist-packages` 下（`pip show` 的 `Location`）。
- 但它是 **CUDA 12 构建**，导入时报 `ImportError: libcudart.so.12: cannot open shared object file`；Pod 里只有 CUDA 13（`libcudart.so.13`）。
- 所以再执行官方命令，装到的仍是 CUDA 12 的构建，无法导入。

PyPI 上 Mooncake 按平台拆成不同的包（**实测**，查了包名列表）：

| 包名 | 用途 |
|---|---|
| `mooncake-transfer-engine` | 默认，CUDA 12 |
| `mooncake-transfer-engine-cuda13` | **CUDA 13（本集群用这个）** |
| `mooncake-transfer-engine-non-cuda` | 无 CUDA |
| `…-efa`、`…-efa-cuda13`、`…-efa-non-cuda` | AWS EFA |
| `…-rocm`、`…-musa`、`…-npu` | AMD、摩尔线程、昇腾 |

<a id="2-2"></a>
### 2.2 本集群的装法（**实测**，两个 Pod 各做一次）

```bash
for p in server-0 server-1; do
  kubectl exec $p -- bash -lc '
    pip install -q --no-deps --target /vllm-workspace/mte13 mooncake-transfer-engine-cuda13 msgpack'
done
```

- 版本：`mooncake-transfer-engine-cuda13 0.3.13.post1`，`cp312` 的 x86_64 wheel（约 93 MB，解开后 `/vllm-workspace/mte13` 约 327 MB）。Python 版本看 wheel 文件名里的 `cp312`，不是看 `0.3.13` 这个版本号。
- 用 `--target` 装到单独目录：**没有改动镜像自带的包，也没有动系统的 Python 包**；代价是运行时要设 `PYTHONPATH=/vllm-workspace/mte13`，否则会先找到那个坏掉的 CUDA 12 版本。
- 用 `--no-deps`：该包的依赖是 `aiohttp`、`requests`（Pod 里已有）和 `msgpack`（Pod 里没有，所以单独补装在同一目录）。
- 用 **`python3`（3.12）**：PyPI 有 `cp310` ～ `cp313` 的 wheel，Pod 里的 PyTorch、cupy 也都在 3.12，所以不存在 pyverbs 的版本分裂问题（[py-multi-node-cpu.md §1.5](py-multi-node-cpu.md#1)）。
- 装在容器可写层，**Pod 重建后会丢**。

<a id="2-3"></a>
### 2.3 验证（**实测**，两个 Pod 结果一致）

```bash
PYTHONPATH=/vllm-workspace/mte13 python3 -c "
from mooncake import engine
print({k: getattr(engine, k) for k in dir(engine) if k.startswith('SUPPORT')})
engine.TransferEngine()"
```

- `SUPPORT_CUDA = True`、`SUPPORT_INTRA_NVLINK = True`；`SUPPORT_EFA`、`SUPPORT_HIP`、`SUPPORT_MNNVL`、`SUPPORT_MUSA` 为 False。
- `TransferEngine()` 能创建，`ldd` 没有缺失的库（实际链接了哪些库见 [§2.4](#2-4)）。

<a id="2-4"></a>
### 2.4 对系统软件的依赖（**实测**，server-0 的 Pod 内）

方法：对装好的 `engine.so` 等文件用 `readelf -d` / `ldd` 看链接关系，再对"初始化引擎并注册 1 MiB 内存"（`protocol="rdma"`、`mlx5_0`）做一次 `strace`，看进程实际打开了哪些文件和设备；各库的来源包用 `dpkg -S` 核对。

**① Python 层**（wheel 的 `METADATA`）

| 项 | 内容 |
|---|---|
| Python 版本 | `cp312` 的二进制模块，`Requires-Python >= 3.10` |
| Python 包依赖 | `aiohttp`、`requests`（Pod 里已有）、`msgpack`（Pod 里没有，补装在 `mte13`，见 [§2.2](#2-2)） |

**② wheel 自带的库**（在 `mooncake_transfer_engine_cuda13.libs/`；`engine.so` 的 `RPATH` 是 `$ORIGIN:$ORIGIN/../mooncake_transfer_engine_cuda13.libs`，所以优先从这里找）

| 自带的库 | 说明 |
|---|---|
| `libglog`、`libgflags`、`libjsoncpp`、`libyaml-cpp`、`libxxhash`、`libzstd` | 日志、命令行参数、JSON / YAML、哈希、压缩 |
| `libasio`、`libetcd_wrapper` | 网络 I/O、etcd 客户端（用 `P2PHANDSHAKE` 时不需要 etcd 服务，但库是链接进去的） |
| `liburing` | 官方文档把系统的 `liburing2` 列为依赖，但 wheel 里已带了一份；Pod 里没装 `liburing2` 也能导入。**推断**系统的 `liburing2` 在这里是多余的，没有验证它在哪些功能下才会用 |

**③ 必须由系统提供的库**（`engine.so` 的 `NEEDED`，不在 wheel 里）

| 库 | 来自 | 说明 |
|---|---|---|
| `libibverbs.so.1` | apt `libibverbs1`（39.0-1） | verbs API |
| `libmlx5.so.1` | apt `ibverbs-providers` | mlx5 网卡的用户态驱动；**`engine.so` 直接链接它**（pyverbs 是靠 libibverbs 运行时加载 provider 插件），`store.so`、`libmooncake_pg.so` 同样如此 |
| `libnuma.so.1` | apt `libnuma1` | NUMA |
| `libcurl.so.4` | apt `libcurl4` | HTTP（元数据服务用）；它自己带出约 30 个传递依赖（openssl、krb5、ldap、gnutls 等） |
| `libnl-3` / `libnl-route-3` | apt | 由 libibverbs / libmlx5 带出 |
| `libstdc++`、`libc` 等 | 系统基础库 | |
| `librdmacm` | — | 官方依赖列表里有 `librdmacm1`，但**我们检查的 `engine.so`、`store.so`、`libmooncake_pg.so` 的 `NEEDED` 里都没有它**（**观察**；可能其他后端用到，没有查） |

**④ NVIDIA / CUDA**（wheel **不自带**，要求系统有）

| 库 | 来自 | 说明 |
|---|---|---|
| `libcudart.so.13` | `/usr/local/cuda/lib64`（镜像自带的 CUDA 13 工具包） | CUDA 运行时。**这就是为什么需要 `-cuda13` 的包**：CUDA 12 构建要找 `libcudart.so.12`，找不到（[§2.1](#2-1)） |
| `libcuda.so.1` | 宿主机驱动：以**只读挂载**注入容器的 `/usr/lib/x86_64-linux-gnu/libcuda.so.590.48.01`，不属于任何 apt 包（`mount` 里能看到） | CUDA 驱动 API |

**⑤ 内核与设备接口**（`strace` 里实际看到的，"初始化 + 注册 1 MiB"）

| 接口 | 观察到 | 用途（**推断**） |
|---|---|---|
| `/dev/infiniband/uverbs0` + `NETLINK_RDMA` 套接字 | 打开 `uverbs0`；`RDMA_VERBS_IOCTL` 共约 290 次；2 个 `NETLINK_RDMA` 套接字 | 创建 verbs 资源、查询 RDMA 设备 |
| `/sys/class/infiniband/mlx5_0/…` | 读 `device/numa_node`、`ports/1/gid_attrs/ndevs/0..3` | 网卡所在 NUMA 节点、GID 对应的网络设备（据此自动选 GID，即日志里的 `network state: with network device`） |
| `/sys/bus/pci/devices/<BDF>/numa_node` | 读了 24 次 | 各 PCI 设备（GPU、网卡）所在 NUMA 节点；**推断**用于"就近选网卡"，即 MTE 的就近可能按 NUMA 判断，而不是按 `nvidia-smi topo -m` 里的 PCIe 距离，**没有验证** |
| `/sys/devices/system/{cpu,node,memory}`、`/proc/cpuinfo`、`/proc/self/status` | 读 CPU、NUMA 节点、内存信息 | 拓扑发现 |
| `/dev/nvidiactl`、`/dev/nvidia0..7`、`/dev/nvidia-uvm`、`/proc/driver/nvidia/…` | 8 块 GPU 的设备文件各被打开约 6 次，`/proc/driver/nvidia` 下打开 52 次 | CUDA 初始化；与 [§4.4](#4-4) 里"`initialize` 前没初始化 CUDA 就要 4.5 秒"**相符**，很可能花在对 8 块 GPU 逐个初始化上，**未证明** |
| 一个 `AF_INET` `SOCK_STREAM` 套接字 | 1 个 | 握手监听端口（P2P handshake） |
| `/etc` 下 | 只打开 `ld.so.cache` 和 `localtime` | 与 pyverbs 不同：**没有**读 `/etc/libibverbs.d/*.driver`（pyverbs 的追踪里读了 15 个），原因没有查 |

**⑥ 宿主机内核模块**（只能从现象推断）

| 模块 | 依据 |
|---|---|
| `ib_uverbs`、`mlx5_ib`、`mlx5_core` | `/dev/infiniband/uverbs0` 与 `mlx5_0` 存在 |
| `nvidia`、`nvidia_uvm` | `/dev/nvidia*`、`/dev/nvidia-uvm` 存在 |
| `nvidia_peermem` | Pod 里能看到它已加载；GPU 显存的 RDMA 注册成功（[§4](#4)）。**注册走 peermem 还是 dmabuf 没有查** |

**⑦ 环境变量与网络**

- 环境变量：`MC_*` 一族（`MC_GID_INDEX`、`MC_IB_TC`、`MC_TE_FILTERS`、`MC_TRANSFER_TIMEOUT` 等），见官方架构页；本实验都没有设置，用的是默认值。`PYTHONPATH` 要包含 `mte13`（[§2.2](#2-2)）。
- 网络：握手要走一条 TCP（本实验是 `eth0`），监听端口是**随机**的（P2P 模式下由 `get_rpc_port()` 取回），所以两个 Pod 之间这些端口不能被防火墙挡住；RDMA 流量走 fabric 网卡（RoCE v2，UDP 4791）。

**小结**：对系统软件的依赖主要是三类——**RDMA 用户态库**（`libibverbs`、`libmlx5`）、**CUDA 运行时与驱动**（`libcudart.so.13` + `libcuda.so.1`，CUDA 版本必须对得上）、**内核与设备接口**（uverbs、netlink、`/sys`、`/dev/nvidia*`）；其余库（glog、liburing 等）都自带。

**限度**：这是对一次"初始化 + 注册"的追踪，不含实际传输（`transfer_*`）和 GPU 显存注册（peermem 或 dmabuf）那一步；`strace` 只能看到系统调用，看不到对 mmap 内存的读写。

<a id="3"></a>
## 3. 实验 1：CPU 内存 WRITE

[↑ 返回目录](#toc)

**目标**：server-0 把 1 MiB 数据经 RDMA 写进 server-1 的普通进程内存，并校验。

**代码**：[`mte/server_cpu.py`](mte/server_cpu.py)（server-1）、[`mte/client_cpu.py`](mte/client_cpu.py)（server-0）。基于官方 Python 例子（官方例子用 TCP 协议、同一台机器），改动见 [§3.5](#3-5)。

<a id="3-1"></a>
### 3.1 流程

（对应最初记的流程表；原表的 Sender / Receiver 已按角色名改为 Client / Server。）

| Client（server-0） | Server（server-1） |
|---|---|
| | 准备好 buffer 接收数据 |
| | 向 MTE 注册 buffer |
| | 带外监听，采用 TCP/IP |
| 带外请求，采用 TCP/IP | |
| | 接受请求，提供 buffer 的会话 ID、地址及长度 |
| 准备好发送 buffer、填充数据 | |
| 向 MTE 注册 buffer | |
| 拿到 Server 的 buffer 信息 | |
| 发送数据，需要会话 ID | |
| 经带外通知 `done` | 收到 `done`，校验内存 |
| 释放内存 | 释放内存 |

<a id="3-2"></a>
### 3.2 运行（server 先启动）

```bash
# 把脚本拷进 Pod（在 nuc 的 mte/ 目录下）
kubectl exec -i server-1 -- bash -c "cat > /vllm-workspace/server_cpu.py" < server_cpu.py
kubectl exec -i server-0 -- bash -c "cat > /vllm-workspace/client_cpu.py"  < client_cpu.py

# 终端 1：server-1（--host 填本机 eth0 的 IP）
kubectl exec -it server-1 -- bash -lc 'cd /vllm-workspace && PYTHONPATH=/vllm-workspace/mte13 python3 -u server_cpu.py --host 10.133.0.253 --dev mlx5_14'
# 终端 2：server-0（--peer 填 server 的 eth0 IP）
kubectl exec -it server-0 -- bash -lc 'cd /vllm-workspace && PYTHONPATH=/vllm-workspace/mte13 python3 -u client_cpu.py --host 10.133.1.12 --dev mlx5_0 --peer 10.133.0.253'
```

| 参数 | 含义 | 脚本 |
|---|---|---|
| `--host` | 本机 eth0 的 IP，作为 MTE 的 `local_hostname`，对端据此做握手 | 两端 |
| `--dev` | RDMA 设备名（逗号分隔）；留空由 MTE 自动发现 | 两端 |
| `--peer` | server 的 eth0 IP（OOB 取 buffer 信息） | client |
| `--protocol` | `rdma`（默认）或 `tcp` | 两端 |
| `--oob-port` | OOB 端口，默认 5555 | 两端 |

<a id="3-3"></a>
### 3.3 结果（**实测**，共 3 次，全部通过）

角色改名（receiver → server、sender → client）后又用新脚本重跑过一次，结果同样通过。下面输出里的**角色词**是改名后的写法；端口、地址等**具体数值**来自改名前的某次运行，每次运行端口都不同。

server（关键行）：

```
Server session ID: 10.133.0.253:16643
Server buffer address: 140077611503632, length: 1048576
写入前第一个字节：0
Buffer information sent to client ('10.133.1.12', 53294).
First byte in server buffer: 7
校验：1048576 字节中，值不是 7 的有 0 个 -> 通过
```

client（关键行）：

```
Server session ID: 10.133.0.253:16643
Client session ID: 10.133.1.12:16659
Transferring data to server...
Transfer successful.
```

MTE 的日志还能看到（**实测**）：

| 日志 | 含义 |
|---|---|
| `Using RDMA transport (RoCE/iWARP)` | 确实走 RDMA，不是退回 TCP |
| `Device mlx5_14 port 1 is available`、`Found 1 HCAs` | 只发现并使用 `--dev` 指定的那一块网卡 |
| `Find best gid index: 3 on mlx5_14/1 (network state: with network device)` | MTE **自动**选了 GID 下标 3，与我们手工选的一致 |
| `RDMA device: mlx5_14 … GID: fd:02:00:00:00:00:05:27:…:01` | 本端 GID 是 fabric 网口的地址 |
| `RPC using P2P handshake, listening on 10.133.0.253:16643` | 握手监听在 `--host` 的 IP 上；端口（16643）不是 `local_hostname` 里写的 12001，**推断**是 P2P 模式随机选的，由 `get_rpc_port()` 取回 |
| `transferSync, cache not found, openSegment with target 10.133.0.253:16643` | 第一次向这个对端传输时，先 `openSegment` |
| server 一侧 `Received RDMA ready ACK` | 两端的 RDMA endpoint 已就绪 |
| `Auto-discovering topology…` → `Topology discovery complete` | 约 4.5 秒（两端都是），是整条命令约 7.3 秒的主要部分；**原因见 [§4.4](#4-4)**（MTE 在进程里首次初始化 CUDA 的耗时） |

`transfer_sync_write` 本身（含按需建连）从日志时间戳看约 20 ms。**不能当性能数据**：只有 1 MiB、一次传输、没有预热，且日志时间戳只精确到毫秒级。

**VRF 没有造成问题**：握手走 `eth0`（不受 VRF 影响），RDMA 一侧由 MTE 自己按网卡和 GID 处理，不需要 `--src`（与 pyverbs 的 CM 版不同，见 [py-multi-node-cpu.md §6](py-multi-node-cpu.md#6)）。MTE 内部具体怎么连的，没有读源码。

<a id="3-4"></a>
### 3.4 代码讲解

下面讲关键片段，完整代码见脚本。

**① 初始化引擎**（两端相同）：

```python
engine = TransferEngine()
ret = engine.initialize(args.host, "P2PHANDSHAKE", args.protocol, args.dev)
session_id = f"{args.host}:{engine.get_rpc_port()}"
```

- 第一个参数 `local_hostname`：本机对外可达的地址（这里是 `eth0` 的 IP），**同时是本端 segment 的名字**，所以必须全局唯一、对端能连上。
- 第二个参数是元数据服务：`"P2PHANDSHAKE"` 表示不用独立服务，两端点对点握手；多机生产环境一般用 etcd / redis / http。
- 第四个参数 `device_name`：留空则自动发现所有网卡；本实验显式指定 `mlx5_14` / `mlx5_0`。
- `session_id = host:rpc_port` 就是**对端要用的目标名**，需要经带外告诉 client。

**② 准备并注册内存**（server）：

```python
server_buffer = (ctypes.c_uint8 * BUFFER_SIZE)()       # 1 MiB，普通 Python 进程内存
server_ptr = ctypes.addressof(server_buffer)           # 地址（整数）
engine.register_memory(server_ptr, server_len)         # 注册给 MTE，返回 0 成功
```

`register_memory` 相当于 verbs 里的 `ibv_reg_mr`（MTE 内部对每块选中的网卡注册，得到每块网卡一个 rkey，放进 segment 元数据）；这一点是**文档**的说法，没有读源码。我们的 pyverbs 脚本里要自己拿 `mr.rkey` 带外传给对端，这里不用——对端从元数据里取。

**③ 带外传递 buffer 信息**（server 监听、client 连接）：

```python
payload = {"session_id": session_id, "ptr": server_ptr, "len": server_len}
conn.sendall(json.dumps(payload).encode() + b"\n")
```

只传三样：**目标名、地址、长度**。没有 `qpn` / `gid` / `rkey`——这些由 MTE 在内部（P2P 握手）完成。这是它和我们 OOB 版 pyverbs 脚本最大的区别。

**④ 发起写**（client）：

```python
engine.register_memory(client_ptr, client_len)         # 本端缓冲也要注册（lkey）
ret = engine.transfer_sync_write(
    server_session_id,                                 # 目标 segment 名 = server 的 session_id
    client_ptr,                                        # 本端地址
    server_ptr,                                        # 对端地址
    min(client_len, server_len))                       # 长度
```

同步版：返回时数据已写完，返回 0 表示成功、负数失败。内部由 MTE 选网卡、按需建连、必要时切片。对端地址**必须落在对端注册过的 buffer 内**，否则失败（**文档**）。

**⑤ 通知与校验**：RDMA WRITE 对 server 是"无声"的（与 pyverbs 实验一样，见 [rdma.md §5.4](rdma.md#5)），所以 client 经带外发 `done`，server 收到后才去读内存校验。

<a id="3-5"></a>
### 3.5 相对原版脚本的改动

原版（官方例子的变体，`PROTOCOL = "rdma"`、设备 `mlx5_0`）原文件名是 `receive.py` / `sender.py`，现已改名为 `server_cpu.py` / `client_cpu.py`；备份在 nuc 的 `/tmp/mte_mine/*.orig`。下表里的 server / client 即原版的 receiver / sender：

| 项 | 原版 | 现在 | 原因 |
|---|---|---|---|
| IP、设备名 | 写死（别的 Pod 的 IP） | 命令行参数 `--host` / `--dev` / `--peer` | 换环境不用改代码 |
| 等传输完成 | server `input()` 等人按回车；client `sleep(10)` | server 经 OOB 等 `done`；client 不再 sleep | `kubectl exec` 没有终端，`input()` 会失败；同步写返回时数据已经写完 |
| 校验 | 只打印第一个字节 | 校验整块内存是否都是 7 | 避免只看第一个字节 |
| 返回值 | 没检查 `initialize` | 检查，失败就报错 | |
| 日志输出 | | 加了 `flush=True`，运行时用 `python3 -u` | 输出被重定向时不缓冲 |

<a id="4"></a>
## 4. 实验 2：跨节点 GPU 显存 → GPU 显存

[↑ 返回目录](#toc)

**目标**：server-0 的 GPU0 把显存里的数据经 RDMA 直接写进 server-1 的 GPU7 的显存（GPUDirect RDMA），并在 GPU 上校验。

**代码**：[`mte/server_gpu.py`](mte/server_gpu.py)（server-1）、[`mte/client_gpu.py`](mte/client_gpu.py)（server-0）。结构与实验 1 相同，只在内存的来源上不同（[§4.3](#4-3)）。

<a id="4-1"></a>
### 4.1 GPU 与网卡要就近

本集群每块 GPU 与两块网卡在同一个 PCIe 交换机下（`nvidia-smi topo -m` 里显示 `PXB`），Pod 里只有偶数号网卡可用，所以：

| GPU | 就近的网卡（Pod 里可用的） |
|---|---|
| GPU0 | `mlx5_0` |
| GPU1 | `mlx5_2` |
| GPU N | `mlx5_(2N)`，如 GPU7 ↔ `mlx5_14` |

"`mlx5_N` 就是 `nvidia-smi` 里的 `NICN`"是按顺序对应的**推断**，没有逐个核对。本实验用 GPU0 + `mlx5_0`（client）和 GPU7 + `mlx5_14`（server）。

<a id="4-2"></a>
### 4.2 运行（server 先启动）

```bash
# 把脚本拷进 Pod（同实验 1）
kubectl exec -i server-1 -- bash -c "cat > /vllm-workspace/server_gpu.py" < server_gpu.py
kubectl exec -i server-0 -- bash -c "cat > /vllm-workspace/client_gpu.py"  < client_gpu.py

# 终端 1：server-1
kubectl exec -it server-1 -- bash -lc 'cd /vllm-workspace && PYTHONPATH=/vllm-workspace/mte13 python3 -u server_gpu.py --host 10.133.0.253 --dev mlx5_14 --gpu 7'
# 终端 2：server-0
kubectl exec -it server-0 -- bash -lc 'cd /vllm-workspace && PYTHONPATH=/vllm-workspace/mte13 python3 -u client_gpu.py --host 10.133.1.12 --dev mlx5_0 --gpu 0 --peer 10.133.0.253 --repeat 3'
```

| 参数 | 含义 | 脚本 |
|---|---|---|
| `--host`、`--dev`、`--oob-port`、`--protocol` | 同实验 1；`--dev` 这里必填 | 两端 |
| `--gpu` | 使用哪块 GPU，默认 0 | 两端 |
| `--size-mib` | 显存缓冲大小，默认 64 MiB；**两端要一致**（client 取两者较小值） | 两端 |
| `--peer` | server 的 eth0 IP | client |
| `--repeat` | 重复传输次数，默认 1；第 1 次含建连开销 | client |

VS Code 里调试这两个脚本的 `launch.json` 在 [`mte/.vscode/launch.json`](mte/.vscode/launch.json)（需要在已连接 Pod 的窗口里装 Python Debugger 扩展）。

<a id="4-3"></a>
### 4.3 与 CPU 版的差异（代码）

| 项 | CPU 版（实验 1） | GPU 版 |
|---|---|---|
| 内存来源 | `ctypes` 数组，地址用 `ctypes.addressof` | `torch.zeros(size, dtype=torch.uint8, device="cuda:N")`，地址用 `tensor.data_ptr()`（**设备地址**） |
| 注册 | `engine.register_memory(ptr, len)` | **同一个调用**，只是传的是显存地址 |
| 传输 | `transfer_sync_write(目标, 本端地址, 对端地址, 长度)` | **完全相同** |
| 初始化前 | | 先 `torch.cuda.set_device(gpu)`，让之后的 CUDA 操作落在这块 GPU 上 |
| 同步 | | 分配、填充后调 `torch.cuda.synchronize()`，确保网卡去读之前数据已就绪；server 校验前同样同步 |
| 数据 | 整块填 7 | 第 i 字节 = i mod 251（位置相关，能发现错位、重复、只写一部分） |
| 校验 | CPU 上逐字节统计 | **在 GPU 上**重新生成期望模式并比较（`(buf != expect).sum()`），只把一个整数取回 CPU |

也就是说，从 CPU 换到 GPU，MTE 的调用**不用改**：它根据指针所在位置自己处理（文档说 MTE 会自动判断硬件信息选最佳传输方式）。

<a id="4-4"></a>
### 4.4 结果（**实测**）

| 大小 | 校验 | 第 1 次（含建连） | 第 2、3 次 |
|---|---|---|---|
| 64 MiB | 通过，0 个字节不一致 | 22.6 ms | 1.96 ms、1.95 ms |
| 1 GiB | 通过，0 个字节不一致 | 45.9 ms | 26.7 ms、26.6 ms |

- 写入前 server 的显存全为 0；写入后前 8 个字节是 `[0, 1, 2, 3, 4, 5, 6, 7]`，整块与期望模式逐字节一致。
- 日志里是 `Using RDMA transport (RoCE/iWARP)`，GID 自动选了下标 3，`register_memory` 对显存地址返回 0（成功）。
- 第 1 次比后面慢，是**按需建连**的开销（日志里有 `openSegment`、`Received RDMA ready ACK`）。
- **吞吐只作参考**：折算 64 MiB 约 32 GiB/s、1 GiB 约 37.5 GiB/s（约 320 Gb/s），低于单卡 400 Gb/s 线速，原因没有深究。只测了 3 次、没有预热和多轮统计，计时只是 `transfer_sync_write` 的墙钟时间，**不是基准测试**。

**初始化耗时与 CUDA 有关**（对照**实测**，因果为**推断**）：实验 1 里 `initialize` 约 4.5 s，实验 2 里只要约 7 ms。单独做了对照（server-0，`mlx5_0`，各跑 2 次）：

| 进程里 `initialize` 之前 | `initialize` 耗时 |
|---|---|
| 没有初始化 CUDA | 4.57 s、4.55 s |
| 已用 PyTorch 初始化 CUDA（`set_device` + 分配一个张量） | 0.01 s、0.01 s |

所以这 4.5 s 是 **MTE 在进程里首次初始化 CUDA 的耗时**（因果由对照结果推断，没有读 MTE 源码）。实验 2 里 PyTorch 已先初始化了 CUDA，所以这一步几乎不花时间；这部分开销没有消失，而是在 `torch.cuda.set_device` / 分配显存时已经付过了。

<a id="4-5"></a>
### 4.5 没验证的

- **是否真正走了 GPUDirect**：`register_memory` 成功、数据在 GPU 上校验正确，据此**推断**是 GPUDirect RDMA（Pod 里有 `nvidia_peermem`）；MTE 内部走 `nvidia_peermem` 还是 dmabuf，没有查。
- 只测了 GPU0 → GPU7 一对；没有测别的 GPU 与网卡组合，也没测**不就近**（如 GPU0 配 `mlx5_14`）会差多少。
- 没测 `--dev` 留空、多网卡并行、READ、异步和批量传输。
- 每次传输都是同一份数据重复写；server 只在最后校验一次。

<a id="5"></a>
## 5. 与 pyverbs 的对照

[↑ 返回目录](#toc)

| | pyverbs OOB / CM 版（[py-multi-node-cpu.md](py-multi-node-cpu.md)） | MTE 版（本篇） |
|---|---|---|
| Python 版本 | 3.10（apt 的 pyverbs 只有 3.10 的二进制） | 3.12（PyPI 有 `cp312` wheel） |
| 带外传什么 | OOB 版：`qpn`、`psn`、`gid`、`addr`、`rkey`；CM 版：`rdma_cm` 自己交换，`addr` / `rkey` 用 SEND | 目标名、地址、长度（`rkey` 等由 MTE 内部处理） |
| 选网卡 | 手工 `--dev`；CM 版靠 `--src` / `--bind` 的 IP | `--dev` 指定，或留空自动发现；GID 自动选 |
| VRF | CM 版必须 `--src` | 没有碰到问题（握手走 eth0） |
| QP 状态转换 | OOB 版手工 `modify_qp` | 不见 |
| 发起写 | `post_send` / `post_write` + 轮询 CQ | `transfer_sync_write` |
| 完成通知给对端 | 手工发 `'W'` | 本实验仍手工（OOB `done`）；MTE 另有 `TransferNotify` 机制，未测 |
| GPU | 不能直接用（[py-multi-node-cpu.md §1.6](py-multi-node-cpu.md#1)） | **可以**：同一个 `register_memory` / `transfer_sync_write`，传显存地址即可（[§4](#4)，**实测**） |
| 适合 | 看懂 verbs 流程 | 实际搬运数据 |

<a id="5-1"></a>
### 5.1 与 PyTorch 集成：MTE 相对 pyverbs 的优势

**本集群实测过的**：

| 方面 | pyverbs | MTE |
|---|---|---|
| 和 PyTorch 在同一个进程里 | **不行**：apt 的 pyverbs 只有 Python 3.10 的二进制，PyTorch、cupy、cuda-python 都在 3.12（[py-multi-node-cpu.md §1.5](py-multi-node-cpu.md#1)） | **可以**：PyPI 有 `cp312` wheel（`mooncake-transfer-engine-cuda13`），装进 3.12，与 PyTorch 同一个解释器（[§2](#2)） |
| 显存怎么交给网卡 | 接口上有 `MR(address=ptr)`，**未测**，且无法与 PyTorch 同进程 | [§4](#4)：`tensor.data_ptr()` 直接传给 `register_memory`，再 `transfer_sync_write`，跨节点 GPU0 → GPU7，校验通过 |
| 选网卡、GID | 手工：`--dev`、GID 下标 3；CM 版还要 `--src` | `--dev` 指定，GID 自动选了 3；VRF 没造成问题（握手走 `eth0`） |
| 建连 | 手工 `modify_qp`，或用 `rdma_cm` | 全部在 MTE 内部完成，调用方只传"目标名、地址、长度" |
| 代码量 | OOB 版约 200 行，要处理 QP、PSN、rkey、轮询 CQ | 核心只有 `initialize` / `register_memory` / `transfer_sync_write` 几行 |

**官方文档里写了、本集群没测过的**（**文档**）：

| 能力 | 文档的说法 |
|---|---|
| 挂在 CUDA stream 上 | `transfer_write_on_cuda` / `transfer_read_on_cuda` 及批量版，`stream_ptr` 可用 PyTorch 的 `stream.cuda_stream`，让传输与 GPU 计算按 stream 排队；文档警告：回调里出错会直接 `_exit(1)` 终止进程 |
| 多网卡并行 | 大于 64 KB 的请求自动切片，不同切片可走不同网卡；按内存类型就近选网卡 |
| 故障处理 | 网卡不可用时换一张重提，自动重建连接 |
| 异步与批量 | 一次提交多个不连续的区间，异步查状态，适合 KV Cache 这类"很多小块"的传输 |
| 通知 | `TransferNotify` 让对端知道某次传输已完成，不用自己写带外 `done` |

<a id="5-2"></a>
### 5.2 NVLink 支持情况

"支持 GPU 与 GPU"要分三种情况看：

| 场景 | MTE | 状态 |
|---|---|---|
| **跨节点** GPU 显存 → GPU 显存（RDMA） | 支持 | **实测**（[§4](#4)） |
| **同一台机器内** GPU ↔ GPU（NVLink） | 构建里带有 `IntraNodeNvlinkTransport`，模块属性 `SUPPORT_INTRA_NVLINK = True` | **未测**；见下 |
| **跨节点** NVLink（MNNVL） | 本构建**不支持**：`SUPPORT_MNNVL = False` | 见下 |

依据与限度：

- 前两行里的"构建里带有"是**检查已安装的 `engine.so` 里的字符串**得到的，没有运行过。看到的内容：`IntraNodeNvlinkTransport` 的实现（用到 `cudaIpcGetMemHandle` / `cudaIpcOpenMemHandle` 和 `cudaMemcpyBatchAsync`）；日志字符串 `Using Intra-Node NVLink transport (MC_INTRANODE_NVLINK set)`，据此**推断**要设环境变量 `MC_INTRANODE_NVLINK` 才启用；另外还有 `MC_INTRA_NVLINK`、`MC_USE_NVLINK_IPC`、`MC_FORCE_MNNVL`，它们各自的含义没有查。
- 跨节点 NVLink：字符串里有 `Protocol 'nvlink' requires -DUSE_MNNVL=ON`，说明这个 wheel 没有按该选项编译。另外本集群两个节点之间是 RoCE（以太网），不是 NVLink 域，所以即使换构建，在这套集群上也用不上（对拓扑的**推断**）。
- 本集群节点内 8 块 GPU 之间是 NVLink（`nvidia-smi topo -m` 显示 `NV18`），所以节点内 NVLink 在硬件上是有的。
- 节点内 NVLink 实际能不能用、协议名怎么写、速度如何，都**没有验证**；官方文档对 Python 的 `protocol` 参数只给了 `rdma`、`tcp` 这样的例子。

<a id="5-3"></a>
### 5.3 需要说明的限制

- **MTE 不是集合通信库**：没有 all-reduce / all-gather。训练中的集合通信仍用 PyTorch 自带的 NCCL；MTE 解决的是点对点、大块、可能一对多的搬运（如 KV Cache）。两者是互补，不是替代。
- **pyverbs 的优势仍然存在**：它直接暴露 verbs，适合学习、验证环境、排查问题；MTE 把细节都藏起来了，出问题时更难看清内部做了什么（比如 QP 怎么建，我们只能从日志推断）。
- **安装有坑**：官方 `pip install` 在本集群装出来的是 CUDA 12 构建，导不进来，要用 `-cuda13` 的包（[§2.1](#2-1)）。
- **没有和 NCCL 做过对比**：实验 2 只测了一对 GPU，所以"MTE 比 NCCL 点对点快还是慢"**没有结论**。

<a id="6"></a>
## 6. 没验证的汇总

[↑ 返回目录](#toc)

**传输能力**

- 只测了 **WRITE**（CPU 内存与 GPU 显存各一组）：READ、多块网卡并行、批量传输、异步传输、`TransferNotify` 都没有测。
- `--dev` 留空（自动发现全部网卡）时的行为没有测；这个 Pod 里只有偶数号设备（见 [py-multi-node-cpu.md §9](py-multi-node-cpu.md#9) 第 6 条）。
- `transfer_write_on_cuda` / `transfer_read_on_cuda`（挂在 PyTorch 的 CUDA stream 上）没有测，文档里"出错会 `_exit(1)`"的警告也没有验证（[§5.1](#5-1)）。
- 没有与 NCCL 的点对点传输做对比，所以不能说 MTE 比 NCCL 快或慢。

**GPU 与 NVLink**

- GPU 显存走 `nvidia_peermem` 还是 dmabuf，文档没写，需要看源码或实测（[§4.5](#4-5)）。
- 节点内 NVLink（`IntraNodeNvlinkTransport`）没有运行过：是否要设 `MC_INTRANODE_NVLINK`、协议名怎么写、速度如何都未知；跨节点 NVLink（MNNVL）在本构建里不支持（[§5.2](#5-2)）。

**健壮性**

- 3 次运行都通过，但每次都是新启动的 server；没有测同一个 server 多次传输、对端断开、传输超时（`MC_TRANSFER_TIMEOUT`）等错误路径。
- `register_memory` 只验证了"接口接受什么类型"（[§1.5](#1-5)）；numpy 的 `.ctypes.data`、内存生命周期（张量被释放或移动后继续传输会怎样）都没有测。

**版本与依赖**

- 镜像自带 0.3.10.post2，这里装的是 0.3.13.post1，官方文档对应较新的版本；两个版本的行为差异没有对比。
- 依赖追踪（[§2.4](#2-4)）只覆盖"初始化 + 注册 1 MiB"，没有追踪实际传输，也没有追踪 GPU 显存注册；"就近选网卡按 NUMA 判断"、"4.5 秒花在对 8 块 GPU 的初始化上"都是推断；系统的 `liburing2` 是否多余、`librdmacm` 为什么没被链接，没有验证。

<a id="7"></a>
## 7. 后续实验

[↑ 返回目录](#toc)

| 编号 | 实验 | 状态 |
|---|---|---|
| 1 | CPU 内存 WRITE（[§3](#3)） | **已完成** |
| 2 | 跨节点 GPU 显存 → GPU 显存 WRITE（[§4](#4)） | **已完成** |
| 3 | …… | 待定 |

（后续实验补在这里，并在 [§5](#5) 对照表里补充对应列。）
