# RDMA / RoCE 实验笔记

[English](README.md) | **中文**

在 DigitalOcean DOKS（DigitalOcean Kubernetes Service，DigitalOcean 的 Kubernetes 托管服务）GPU 节点上，学习并实测 **RDMA（Remote Direct Memory Access，远程直接内存访问）经 RoCE v2（RDMA over Converged Ethernet version 2，融合以太网上的 RDMA 第 2 版）** 的工作方式：从原理，到裸 C / Python 程序，到 MTE（Mooncake Transfer Engine，Mooncake 传输引擎），再到交换机上的 QoS（Quality of Service，服务质量）与拥塞控制。

仓库里大部分是**中文文档**，加上配套的示例代码。文档里的每个结论都标了可靠度（**实测** / **推断** / **未验证** 等），读的时候请留意这些标记。

## 从哪里开始读

| 你想…… | 读 |
|---|---|
| 先搞懂 RDMA 是什么、为什么一块网卡有两套接口 | [rdma.md](rdma.md) |
| 搞懂 RoCE v2 网络怎么做拥塞控制（PFC、ECN、DCQCN） | [qos.md](qos.md)，开头有「三分钟读懂」 |
| 看交换机一侧要配什么、怎么核对 | [roce-swtich.md](roce-swtich.md)（文件名有拼写错误，保持原样） |
| 看两台机器经 RDMA 读写主机内存的实测 | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| 看两台机器经 RDMA 读写 GPU 显存的实测 | [c-multi-node.md](c-multi-node.md)（先读上一篇） |
| 用 Python 做同样的事 | [py-multi-node-cpu.md](py-multi-node-cpu.md) |
| 用 MTE 做数据传输 | [mte.md](mte.md) |
| 单机内 GPU 之间的 NVLink / P2P | [c-single.md](c-single.md) |
| 在本地用软件模拟 RoCE（无需网卡） | [soft-roce.MD](soft-roce.MD)、[soft-roce-local.MD](soft-roce-local.MD) |

建议的阅读顺序：`rdma.md` → `c-multi-node-cpu.md` → `c-multi-node.md` → `qos.md`。

## 文档一览

**原理与网络**

| 文档 | 内容 |
|---|---|
| [rdma.md](rdma.md) | RDMA 原理：对象、一次操作怎么走、为什么要注册内存、传输类型、GPUDirect RDMA |
| [qos.md](qos.md) | RoCE v2 拥塞控制与 QoS：两道防线（ECN 加 DCQCN 提前降速，PFC 紧急暂停）、网卡与交换机分工、incast、自适应路由 |
| [roce-swtich.md](roce-swtich.md) | 交换机侧 QoS 配置清单与验证，含向网络管理员核实的 11 项清单 |
| [understanding_RoCE.MD](understanding_RoCE.MD) | DOKS 工作节点上 RoCE 网络的介绍（英文） |
| [soft-roce.MD](soft-roce.MD)、[soft-roce-local.MD](soft-roce-local.MD) | Soft-RoCE（RXE）的搭建，用于学习和测试 |

**实验**

| 文档 | 内容 | 配套代码 |
|---|---|---|
| [c-single.md](c-single.md) | 单机：NVLink 与 P2P，kernel 直接读写对端显存，CUDA IPC | `p2p/0_to_7.cu`、`remote_access.cu`、`ipc_tcp.cu` |
| [c-multi-node-cpu.md](c-multi-node-cpu.md) | 多机主机内存：RDMA WRITE / READ、带宽、`-t 104` 的影响 | `p2p/server-cpu.c`、`client-cpu.c` |
| [c-multi-node.md](c-multi-node.md) | 多机 GPU 显存：GPUDirect RDMA，`nvidia_peermem` 与 dmabuf 两种实现 | `p2p/server-gpu.cu`、`client-gpu.cu` |
| [py-multi-node-cpu.md](py-multi-node-cpu.md) | pyverbs：OOB 版与 `rdma_cm` 版 | `pyverbs-example/` |
| [mte.md](mte.md) | MTE：安装、依赖、CPU 内存与跨节点 GPU 显存传输 | `mte/` |

**工具链**

| 文档 | 内容 |
|---|---|
| [c-build.md](c-build.md) | 一个 `.cu` 怎么变成只依赖驱动的可执行文件 |
| [cuda-toolchain.md](cuda-toolchain.md) | PTX、cubin、两层 JIT 与缓存 |
| [debug.md](debug.md) | gdb、cuda-gdb 与 VS Code，在 Pod 里调试 |

## 实验环境

- **节点**：DOKS 工作节点，每台 8 张 NVIDIA B300 GPU，8 块支持 RoCE v2 的网卡（Linux 里是 `fabric0`、`fabric2`、…，对应 RDMA 设备 `mlx5_0`、`mlx5_2`、…）；单机内 GPU 经 NVSwitch 全互联。
- **Pod**：用 [`n-server2.yaml`](n-server2.yaml) 部署一个两副本的 StatefulSet，得到 `server-0` 与 `server-1`，各申请 8 个 GPU 和各 fabric 网卡的 RDMA 资源；镜像为 `vllm/vllm-openai:cu130-nightly`（CUDA 13.0）。
- **yaml 的区别**：[`n-server.yaml`](n-server.yaml) 两副本，不带调试工具；[`n-server1.yaml`](n-server1.yaml) 单副本，多装了 gdb、cuda-gdb 并加了 `SYS_PTRACE` 权限，用于单机调试（[debug.md](debug.md)）；[`n-server2.yaml`](n-server2.yaml) 与 `n-server1.yaml` 只差副本数（两副本），多机实验用它。
- **操作入口**：所有 `kubectl` 命令都在跳板机 `nuc` 上执行（`ssh nuc`）。
- **网络**：每块 fabric 网卡挂在独立的 VRF（Virtual Routing and Forwarding，虚拟路由转发）上；两端经 `eth0` 上的 TCP 交换连接信息（OOB，Out-Of-Band，带外），数据走 RoCE v2 的 RDMA。

## 关键结论速览

以下均为本集群**实测**，细节、条件和局限见各文档的「摘要」与附录。

| 问题 | 结论 | 出处 |
|---|---|---|
| 主机内存的 RDMA 稳态带宽 | 约 **44 GB/s**（网卡标称 400 Gb/s 的 88%），单 QP（Queue Pair，队列对）、单条 64 MiB 消息 | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| GPU 显存的 RDMA 带宽 | WRITE 约 **29.8 GB/s**，READ 约 **30.0 GB/s**，只有标称的 60%；不是网络上限，同一对网卡上主机内存能到 44 GB/s | [c-multi-node.md](c-multi-node.md) |
| 单机 NVLink | 779.4 GB/s，约是 RDMA 的 26 倍（测量方法不同，只看量级） | [c-single.md](c-single.md) |
| 对端 CPU 参与吗 | 不参与：被读写时 server CPU 约 1%，client 约 98%（忙轮询） | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| GPU RDMA 的两种实现 | `nvidia_peermem` 与 dmabuf **带宽无差别**，注册开销不同（128 MiB：dmabuf 约 0.23 ms，peermem 约 2.1 ms） | [c-multi-node.md](c-multi-node.md) |
| 短测试可靠吗 | **不可靠**：WRITE 从约 22 GB/s 起步逐渐爬升，`-n 100` 只有 22.1，`-n 6000` 才平均 43.3 | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| MTE 怎么装 | 官方 `pip install mooncake-transfer-engine` 用不了（CUDA 12 构建），要装 `mooncake-transfer-engine-cuda13` | [mte.md](mte.md) |
| pyverbs | apt 的 pyverbs 只有 Python 3.10 的二进制，PyTorch 在 3.12，两者不能同进程 | [py-multi-node-cpu.md](py-multi-node-cpu.md) |

<a id="tc"></a>
### 一个重要的坑：必须设 `-t 104`（流量类别）

**本集群上，运行 NCCL 必须设置 `NCCL_IB_TC=104`；本仓库的裸 verbs 程序对应的开关是 `-t 104`，两端都要设。** 104 = DSCP（Differentiated Services Code Point，差分服务代码点）26 乘以 4（ECN 位为 0），详见 [qos.md §3.2](qos.md#3)。

同一对网卡上，主机内存的 RDMA：

| | READ 带宽 | 第一次 READ |
|---|---|---|
| `-t 0`（默认） | 约 24.6 GB/s，会掉到 17 GB/s | 12–103 ms |
| `-t 104` | 43.8 GB/s，恒定 | 1.55 ms |

- WRITE 不受影响；GPU 显存版在测量范围内也不敏感（`-t 0` 与 `-t 104` 数字相同）。
- **原因没有查明**。"`-t 104` 进了为 RDMA 调好的队列"只是假设，并且有线索对它不利（网卡侧没有拥塞、重传、丢弃计数器的增量）。要查清楚，需要交换机一侧的配置，见 [roce-swtich.md §6.1](roce-swtich.md#6) 的核对清单。
- pyverbs 的 `rdma_cm` 版没有设置 `traffic_class`（等于 0），MTE 的 `MC_IB_TC` 默认 -1，所以这两条路径**不能代表** `-t 104` 的表现。

## 上手：复现主机内存实验

下面是最短路径，完整步骤、参数与避坑见 [c-multi-node-cpu.md §5、§6](c-multi-node-cpu.md)。

```bash
# 1. 在 nuc 上部署两个 Pod（得到 server-0、server-1）
kubectl apply -f n-server2.yaml

# 2. 把代码拷进两个 Pod，在 Pod 里编译（纯 C，只依赖 libibverbs）
gcc -O2 -Wall -Wextra p2p/server-cpu.c -o server-cpu -libverbs
gcc -O2 -Wall -Wextra p2p/client-cpu.c -o client-cpu -libverbs

# 3. server-1 上先启动被动方（两端都要 -t 104）
./server-cpu -t 104

# 4. server-0 上运行主动方；-a 是 server-1 的 eth0 IP
./client-cpu -a <server-1 的 eth0 IP> -t 104

# 测稳态带宽：迭代数要大
./client-cpu -a <IP> -t 104 -n 6000 -w 8
```

常见的坑：server 要先启动；两端的 `-s` 必须一致；只能用偶数号网卡；短测试会低估 WRITE。其余见 c-multi-node-cpu.md 的速查表。

## 目录结构

```
.
├── README.md  README-CN.md
├── rdma.md  qos.md  roce-swtich.md  understanding_RoCE.MD   原理与网络
├── c-single.md  c-multi-node-cpu.md  c-multi-node.md        C / CUDA 实验
├── py-multi-node-cpu.md  mte.md                             Python / MTE 实验
├── c-build.md  cuda-toolchain.md  debug.md                  工具链与调试
├── soft-roce.MD  soft-roce-local.MD                         软件模拟 RoCE
├── n-server.yaml  n-server1.yaml  n-server2.yaml           Pod 部署
├── p2p/               C / CUDA 源码：单机 P2P、多机 CPU 与 GPU 版、注册开销测试
├── pyverbs-example/   pyverbs 脚本：OOB 版与 CM（Connection Manager，连接管理器）版
└── mte/               MTE 的 Python 脚本：CPU 与 GPU 版
```

## 约定

- **可靠度标记**：**实测**（在本集群跑出来的）、**推断**（由机制或现象推导）、**文档**（来自官方文档，未在本集群验证）、**未验证** / **未测**（没有证据）。每篇文档的附录里有逐条的状态汇总。
- **英文缩写**：`qos.md` 与 `roce-swtich.md` 每个缩写第一次出现时都给出了英文全称和中文，并有集中的术语表；其他较早的文档可能没有统一这样处理。
- 文档里的"实测"数据来自 2026-10 至 2026-11 在上述环境中的运行，**不同型号的网卡、交换机和网络规模结果会不同**。
- **交换机的实际配置没有访问权限**，所以关于交换机的内容都是通用做法，不是本集群的真实配置。

## 许可

MIT，见 [LICENSE](LICENSE)。
