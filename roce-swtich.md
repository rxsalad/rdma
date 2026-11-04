# RoCE v2 交换机侧 QoS 配置清单与验证

RoCE v2（RDMA over Converged Ethernet version 2，融合以太网上的远程直接内存访问第 2 版）把 RDMA（Remote Direct Memory Access，远程直接内存访问）放在以太网上跑。以太网默认"尽力而为、拥塞就丢包"，而 RDMA 对丢包很敏感，所以承载 RDMA 的交换机要做专门的 QoS（Quality of Service，服务质量）配置。

**本篇的定位**：**交换机一侧**要配什么、怎么核对、怎么验证。机制原理（为什么要两道防线、PFC 和 ECN 怎么工作、网卡做什么）见 [qos.md](qos.md)，本篇不重复。

**状态与可靠度（请先读）**

- 本篇的配置思路与数值示例来自**公开资料与业界通用做法**，**不是**本集群交换机的实际配置——我**没有看过**本集群交换机的配置，也**没有**在交换机上验证过任何一项。
- 具体数值（缓冲大小、阈值、带宽份额）依赖交换机型号、网卡型号与网络规模，**必须以厂商指南为准**。
- 与本集群实验的关系见[第 7 节](#7)，那里把"实测到的"和"没有证据的"分开写。
- 标记：**厂商文档** = NVIDIA 等厂商公开文档；**通用做法** = 业界常见做法；**实测** = 在本集群看到的；**推断** = 由机制推导；**未验证** = 没有证据。

**英文缩写**：每个缩写第一次出现时都给出英文全称和中文，集中列在[第 8 节](#8)。

<a id="toc"></a>
## 目录

- [三分钟读懂](#sum)
- [1. 交换机要做的事（全景）](#1)
- [2. 一个报文是怎么被分到队列的](#2)
- [3. 逐项配置说明](#3)
- [4. 常见约定与厂商示例](#4)
- [5. 常见的坑](#5)
- [6. 怎么核对与验证](#6)
- [7. 与本集群实验的关系](#7)
- [8. 缩写表](#8)
- [附录. 状态汇总](#A)

<a id="sum"></a>
## 三分钟读懂

[↑ 返回目录](#toc)

**交换机要做的核心事情**：让 RDMA 流量走专用队列，并在这个队列上同时开两道防线。

1. **分类**：信任 DSCP（Differentiated Services Code Point，差分服务代码点），把 RDMA 数据（常用 DSCP 26）和拥塞通知包（CNP，Congestion Notification Packet，常用 DSCP 48）分到各自的队列；
2. **PFC**（Priority-based Flow Control，基于优先级的流量控制）：**只**在 RDMA 数据所在的那一个优先级上开；
3. **ECN**（Explicit Congestion Notification，显式拥塞通知）：在 RDMA 队列上设标记阈值，**低于** PFC 的触发阈值，这样先降速、后暂停；
4. **调度**：CNP 队列走严格优先级，RDMA 队列保证一个带宽份额；
5. **缓冲**：给无损队列划专门的缓冲池，设好预留空间（headroom）和暂停阈值；
6. **保护**：开 PFC 看门狗，MTU（Maximum Transmission Unit，最大传输单元）全链路一致，广播与组播不进无损队列。

NVIDIA Cumulus Linux 一条命令 `nv set qos roce` 就按推荐值配好（[§4](#4)）。

**本集群**：测试程序已经用 `-t 104`（DSCP 26）打了标记，但**交换机上怎么配的，未知**；[§6.1](#6) 列了一份要向网络管理员核实的清单。

<a id="1"></a>
## 1. 交换机要做的事（全景）

[↑ 返回目录](#toc)

| 配置项 | 通用做法 | 作用 | 原理见 |
|---|---|---|---|
| **分类** | 信任 DSCP；给 RDMA 数据和 CNP 各分配固定的 DSCP，映射到各自的优先级 | 决定报文进入哪个队列 | [qos.md §3](qos.md#3) |
| **独立队列** | RDMA 数据一个队列；CNP 一个队列；TCP 及其他流量用默认队列 | 隔离，互不影响 | [qos.md §3.3](qos.md#3) |
| **PFC** | **只**在 RDMA 数据所在的优先级上开，CNP 与其他优先级不开 | 缓冲快满时让上游暂停，避免丢包 | [qos.md §5](qos.md#5) |
| **ECN 标记** | 在 RDMA 队列上设标记阈值（Kmin / Kmax / Pmax），按队列深度标记拥塞 | 在 PFC 触发之前就让网卡降速 | [qos.md §4](qos.md#4) |
| **调度** | RDMA 队列用 ETS / DWRR 保证带宽份额；CNP 队列用**严格优先级** | 保证 RDMA 有保底带宽，且拥塞通知不被延迟 | [qos.md §3.4](qos.md#3) |
| **缓冲** | 给无损队列划专门的缓冲池，设好 headroom 与 xon / xoff | 吸收 PFC 暂停生效前还在路上的数据 | [qos.md §6](qos.md#6) |
| **MTU** | 全链路一致；交换机用巨帧 | 避免丢弃或分片 | 本篇 §3 |
| **PFC 看门狗** | 开启 | 检测到暂停风暴时自动恢复 | [qos.md §5.3](qos.md#5) |
| **负载均衡** | 自适应路由，或逐包喷洒，或让多个 QP 的 UDP 源端口不同以分散 ECMP 哈希 | 避免大流集中在同一条链路 | [qos.md §9](qos.md#9) |

**PFC 与 ECN 的配合**：理想状态是 **ECN 先触发、PFC 作为最后的保险**，所以 ECN 的标记阈值要**低于** PFC 的 xoff 阈值，否则总是先触发 PFC，而 PFC 容易造成队头阻塞，甚至死锁。

**另一种思路**：有些网络**不开 PFC**，只靠 ECN 和网卡的选择性重传。优点是避免 PFC 的风险（队头阻塞、暂停风暴、死锁），代价是丢包时有性能损失。这是设计取舍，不是对错（见 [qos.md §5.4](qos.md#5)）。

<a id="2"></a>
## 2. 一个报文是怎么被分到队列的

[↑ 返回目录](#toc)

```
程序：QP（Queue Pair，队列对）的 traffic_class（来自 -t 或 NCCL_IB_TC）
   │   这是 IP 头里的一个字节 = DSCP（高 6 位）+ ECN（低 2 位）
   ▼
网卡：按"信任模式"读 DSCP，映射到一个【优先级】（0～7）
   ▼
交换机：读报文的 DSCP，映射到一个【队列】，该队列决定缓冲、调度、是否开 PFC、ECN 阈值
```

**例：`-t 104`**。104 的二进制是 `0110 1000`：高 6 位 `011010` = **DSCP 26**（也叫 AF31，Assured Forwarding 31，确保转发 31），低 2 位 `00` = ECN 位为 0。换算公式：`traffic_class = DSCP × 4 + ECN`（为什么是乘 4，见 [qos.md §3.2](qos.md#3)）。

- `-t 104` 就是"DSCP 26、ECN 位为 0"；`-t 0` 是 DSCP 0（默认类别）。
- **ECN 位的处理**（**未验证**）：ECN 位为 0（Not-ECT，不支持 ECN 的传输），交换机通常不会给这样的报文打标记。网卡启用拥塞控制时会不会自己把这两位改成 ECT（ECN-Capable Transport，支持 ECN 的传输），因网卡与配置而异，**本篇没有验证**，需要对照网卡与交换机的实际配置确认。

**两端必须一致**：网卡的"DSCP → 优先级"映射、交换机的"DSCP → 队列"映射，必须在整条路径的**每一跳**上一致，错一处，这一段就落到默认队列。

<a id="3"></a>
## 3. 逐项配置说明

[↑ 返回目录](#toc)

### 3.1 分类与队列

- **信任模式**：交换机端口设为信任 DSCP（也可同时信任 PCP，Priority Code Point，优先级代码点；RoCE v2 里主流用 DSCP，见 [qos.md §3.1](qos.md#3)）。
- **DSCP → 优先级 → 队列**：两步映射。NVIDIA Cumulus Linux 默认示例（**厂商文档**，5.6）：DSCP 每 8 个一组映射到一个优先级（24–31 → 3，48–55 → 6）；优先级 3 → 队列 3（RoCE），优先级 6 → 队列 6（CNP），其余 → 队列 0。
- 注意：交换机里的 traffic class（流量类别）指**队列**，与 IP 头里的 Traffic Class 字节**不是一回事**。

### 3.2 PFC

- **只**在 RDMA 数据所在的一个优先级上开（常见优先级 3），CNP 所在优先级不开。
- 两个阈值：入口队列占用达到 **xoff** 时发暂停；降回 **xon** 时恢复。
- **headroom**（预留空间）：发出暂停之后，到上游真正停下之前，仍会有数据到达；必须预留，否则仍会溢出丢包。它与**线缆长度、链路速率、MTU** 有关，**用厂商工具计算，不要手算**。

### 3.3 ECN 标记

- 在 RDMA 队列上设三个参数：**Kmin**（低于它不标记）、**Kmax**（高于它全部标记）、**Pmax**（两者之间的最大标记概率）。
- 阈值可按**绝对字节数**，也可按缓冲的**百分比**。
- NVIDIA Cumulus 默认示例（**厂商文档**，5.6）：对队列 3 启用 ECN，绝对阈值，最小约 150 000 字节，最大约 1 500 000 字节，概率 100。**数值依赖型号和链路速率，不能照搬**。
- **必须低于 PFC 的 xoff**（见 [§1](#1)）。

### 3.4 调度

- **CNP 队列**：严格优先级（SP，Strict Priority），并且**不被 PFC 暂停**，拥塞通知才不会被拖慢。
- **RDMA 队列**：ETS（Enhanced Transmission Selection，增强传输选择）或 DWRR（Deficit Weighted Round Robin，差额加权轮询）保证一个带宽份额。Cumulus 默认示例：CNP 严格优先级；RoCE 队列 DWRR 50%；其余 DWRR 50%。

### 3.5 缓冲与 MTU

- **缓冲**：无损队列使用专门的缓冲池，设好 headroom 与 xon / xoff。
- **MTU**：全链路一致；交换机的 MTU 必须大于 RoCE 的 MTU 加上头开销（RoCE MTU 为 4096 时，交换机常取 9216）。

### 3.6 保护与负载均衡

- **PFC 看门狗**（watchdog）：开启；检测到暂停风暴时自动恢复，防止整个网络被卡住。
- **广播与组播**不进无损队列；无损报文不要泛洪。
- **负载均衡**：RoCE v2 用 UDP（User Datagram Protocol，用户数据报协议）封装，目的端口固定 4791，源端口按 QP 变化，所以**多个 QP 的源端口不同，可以让 ECMP（Equal-Cost Multi-Path，等价多路径）把它们分散到不同路径**。大流少时哈希容易不均，可用自适应路由或逐包喷洒（[qos.md §9](qos.md#9)）。

<a id="4"></a>
## 4. 常见约定与厂商示例

[↑ 返回目录](#toc)

### 4.1 DSCP 约定

NVIDIA（Mellanox）公开文档里常见的一套约定（**厂商文档**）：

| 流量 | 常见 DSCP | 对应优先级 | 是否开 PFC |
|---|---|---|---|
| **RoCE 数据** | 26（`traffic_class = 104`） | 3 | 是 |
| **CNP** | 48 | 6 | 否，用严格优先级 |
| 其他（TCP 等） | 0 | 0 | 否 |

- 这只是**常见约定**，不是强制；具体用哪个 DSCP、映射到哪个队列由网络管理员决定。
- 本集群实验用的 `-t 104`（NCCL 的 `NCCL_IB_TC=104`）与这套约定的 RoCE 数据 DSCP 吻合，但这**不代表**本集群的交换机一定是按这套约定配置的。

### 4.2 Cumulus Linux：一条命令配好

NVIDIA Cumulus Linux 的 NVUE（NVIDIA User Experience，NVIDIA 的命令行配置接口）（**厂商文档**，5.6）：

```
nv set qos roce                  # 无损模式（默认）：PFC + ECN
nv set qos roce mode lossy       # 有损模式：只用 ECN，不开 PFC
nv config apply
```

无损模式按上面的默认值一次配好分类、队列、PFC、ECN、调度。有损模式下 RoCE 优先级不进无损缓冲池，丢包靠 RDMA 传输层自带的重传恢复。

其他品牌的交换机需要手工配上面 §3 的各项，要点相同。

<a id="5"></a>
## 5. 常见的坑

[↑ 返回目录](#toc)

| 坑 | 后果 | 说明 |
|---|---|---|
| **PFC 开得太多** | 暂停风暴、队头阻塞、死锁 | 只在 RDMA 那一个优先级上开，并开启 PFC 看门狗 |
| **ECN 阈值高于 PFC 阈值** | 总是先触发 PFC，ECN 起不到作用 | ECN 的 Kmin / Kmax 要低于 PFC 的 xoff |
| **headroom 设小了** | PFC 暂停还没生效就已溢出，仍然丢包 | headroom 要覆盖链路时延、对端响应时间和一个最大帧 |
| **映射不一致** | 某一段链路上 RDMA 流量落进默认队列，性能不稳 | DSCP → 队列的映射、网卡信任模式要全网一致 |
| **MTU 不匹配** | 大包被丢弃或分片 | 交换机 MTU 要大于 RoCE MTU 加头开销 |
| **哈希极化** | 多条大流挤在同一条链路 | 用自适应路由、逐包喷洒，或让五元组更分散 |
| **CNP 没有走高优先级** | 降速太迟，拥塞扩大 | CNP 要放在严格优先级队列里，且不开 PFC |
| **报文不带 ECT** | 交换机不会打 ECN 标记，第一道防线失效 | 确认网卡发出的包 ECN 位为 ECT（[§2](#2)） |
| **网卡信任 PCP 而不是 DSCP** | 网卡侧把流量归到默认优先级，网卡的 PFC 与调度保护不到它（交换机仍可能按 DSCP 分类） | 在宿主机上用 `mlnx_qos -i <网口>` 看信任模式（[qos.md §3.1](qos.md#3)） |

<a id="6"></a>
## 6. 怎么核对与验证

[↑ 返回目录](#toc)

### 6.1 向网络管理员核实的清单

本集群没有交换机的访问权限，所以最直接的办法是请管理员（或有权限的人）逐项确认。**这份清单也是回答[第 7 节](#7)里那些"未知"所需要的信息**。

| # | 要确认的问题 | 期望的答案 | 为什么 |
|---|---|---|---|
| 1 | DSCP 26 和 DSCP 0 各映射到哪个优先级、哪个队列？ | 26 → RDMA 专用队列；0 → 默认队列 | 判断 `-t 104` 和 `-t 0` 是否走不同队列 |
| 2 | PFC 在哪些优先级上开启？ | 只有 RDMA 那一个 | 开多了有风险 |
| 3 | RDMA 队列上有没有启用 ECN？Kmin / Kmax / Pmax 是多少？是否低于 PFC 的 xoff？ | 启用，且低于 xoff | 否则第一道防线失效 |
| 4 | CNP（DSCP 48）的队列是不是严格优先级、不开 PFC？ | 是 | 保证拥塞通知不被拖慢 |
| 5 | 各队列的调度方式与权重？有没有整形或限速（maxrate）？ | RDMA 有保底份额，没有意外的限速 | 排除"被限速"的可能 |
| 6 | 无损缓冲池和 headroom 是怎么设的？ | 按链路长度与速率算过 | headroom 小了会丢包 |
| 7 | 交换机 MTU 是多少？ | 9216 或至少大于 RoCE MTU 加头开销 | 避免丢弃 |
| 8 | PFC 看门狗开了吗？ | 开 | 防暂停风暴 |
| 9 | 有没有启用自适应路由或逐包喷洒？ECMP 哈希用哪些字段？ | 视需要 | 决定大流是否均匀 |
| 10 | 叶交换机和脊交换机的配置是否**每一跳一致**？ | 一致 | 错一跳就落到默认队列 |
| 11 | 能不能导出各队列的丢包、PFC 暂停帧、ECN 标记、缓冲占用计数？ | 能 | 验证时需要 |

### 6.2 主机侧（网卡）验证

用 `ethtool -S <网口>` 和 `rdma statistic show link <设备>/<端口>`，**测试前后各取一次，看差值，不要看绝对值**。本集群 Pod 里实际有的计数器见 [qos.md §12.2](qos.md#12)，常用的有：

| 想确认 | 看什么 |
|---|---|
| 流量进了预期优先级 | `rx_prio3_bytes`、`tx_prio3_bytes`（N 取 0–7 各一套） |
| 交换机在打 ECN 标记 | `rx_prio3_marked`、`rx_ecn_mark` |
| PFC 暂停帧 | `rx_pause_ctrl_phy`、`tx_pause_ctrl_phy`（只有这两个，没有按优先级的，也没有暂停时长） |
| 丢弃 | `rx_prio3_discards`、`rx_discards_phy`、`rx_out_of_buffer` |
| 重传与超时 | `rdma statistic` 里的 `out_of_sequence`、`packet_seq_err`、`local_ack_timeout_err` |

### 6.3 交换机侧验证

各队列的丢包、PFC 暂停帧收发、ECN 标记、缓冲占用的计数。具体命令因厂商和型号而异，本篇不展开。

### 6.4 验证的基本思路

1. 跑一个已知流量的测试，同时记录网卡与交换机的计数；
2. 确认 RDMA 流量确实进入了预期的队列（该队列的计数增长）；
3. 确认拥塞时 **ECN 标记先于 PFC 暂停**出现；
4. 确认没有意外的丢包与重传。

**健康的样子**：有 ECN 标记，PFC 暂停很少，没有丢弃。

<a id="7"></a>
## 7. 与本集群实验的关系

[↑ 返回目录](#toc)

**已知（来自实验，见 [c-multi-node-cpu.md](c-multi-node-cpu.md)，实测）**：同一对网卡，主机内存的 RDMA：

| | READ 带宽 | 第一次 READ |
|---|---|---|
| `-t 0` | 约 24.6 GB/s，会掉到 17 GB/s | 12–103 ms |
| `-t 104` | 43.8 GB/s，恒定 | 1.55 ms |

WRITE 不受 `-t` 影响。在深度 1 与深度 8 的两次运行里，我检查过 `ethtool -S` 中与拥塞、重传、丢弃相关的计数器，**没有任何增量**。

**不知道**：

- 本集群交换机上 DSCP 0 与 DSCP 26 各自映射到哪个队列；
- 该队列有没有 PFC、ECN、整形、带宽限制；
- 实验时 fabric 的真实负载。

**一个未证实的假设**：`-t 104` 进入了为 RDMA 调好的队列，`-t 0` 落在默认队列，二者的缓冲、ECN 阈值、调度不同，所以表现不同。**这只是假设**，并且有几条线索对它不利，需要对照交换机配置才能判断：

- 网卡侧没有看到丢包、重传、拥塞类计数器的增长；
- 把 CPU 版与 GPU（Graphics Processing Unit，图形处理器）显存版交替各跑 5 次，CPU 版在 `-t 0` 下每次都是约 24.6 GB/s，GPU 版每次都是 30.0 GB/s——所以不像是"某个时刻网络忙"，也不像是 `-t 0` 被一个固定的速率上限卡住（否则 GPU 版在 `-t 0` 下不可能超过它）。

**由这些现象能推出的一点**（**推断**，未验证）：`-t 0` 与 `-t 104` 表现不同，说明 **DSCP 在路径上的某处被识别并影响了处理**。这个"某处"可能是网卡，也可能是交换机，具体是哪里、怎么影响，未知。注意：DSCP 是写在 IP 头里发出去的，无论网卡信任什么，交换机都读得到，所以这个差异**不能**说明网卡的信任模式是什么。

要查清原因，需要的信息就是[§6.1](#6)的清单：这两个 DSCP 对应的队列配置，以及各队列的计数器。

<a id="8"></a>
## 8. 缩写表

[↑ 返回目录](#toc)

概念性的术语（headroom、xoff / xon、队头阻塞、哈希极化等）见 [qos.md §14](qos.md#14)。

| 缩写 | 英文全称 | 中文 |
|---|---|---|
| AF31 | Assured Forwarding 31 | 确保转发 31（DSCP 26 的名称） |
| CNP | Congestion Notification Packet | 拥塞通知包 |
| DCQCN | Data Center Quantized Congestion Notification | 数据中心量化拥塞通知（网卡里的降速算法） |
| DSCP | Differentiated Services Code Point | 差分服务代码点 |
| DWRR | Deficit Weighted Round Robin | 差额加权轮询 |
| ECMP | Equal-Cost Multi-Path | 等价多路径 |
| ECN | Explicit Congestion Notification | 显式拥塞通知 |
| ECT | ECN-Capable Transport | 支持 ECN 的传输 |
| ETS | Enhanced Transmission Selection | 增强传输选择（IEEE 802.1Qaz） |
| GPU | Graphics Processing Unit | 图形处理器 |
| MTU | Maximum Transmission Unit | 最大传输单元 |
| NCCL | NVIDIA Collective Communications Library | NVIDIA 集合通信库 |
| NVUE | NVIDIA User Experience | NVIDIA 的命令行配置接口 |
| PCP | Priority Code Point | 优先级代码点（VLAN 标签里的 3 位） |
| PFC | Priority-based Flow Control | 基于优先级的流量控制（IEEE 802.1Qbb） |
| QoS | Quality of Service | 服务质量 |
| QP | Queue Pair | 队列对（RDMA 的一条连接） |
| RDMA | Remote Direct Memory Access | 远程直接内存访问 |
| RoCE | RDMA over Converged Ethernet | 融合以太网上的 RDMA |
| SP | Strict Priority | 严格优先级 |
| TCP | Transmission Control Protocol | 传输控制协议 |
| UDP | User Datagram Protocol | 用户数据报协议 |
| VLAN | Virtual Local Area Network | 虚拟局域网 |

<a id="A"></a>
## 附录. 状态汇总

[↑ 返回目录](#toc)

| 结论 | 状态 |
|---|---|
| `traffic_class = DSCP × 4 + ECN`；`-t 104` = DSCP 26、ECN 位 0 | **换算**（按字段定义），未抓包验证 |
| 第 1、3、5 节的配置思路（独立队列、只在 RDMA 优先级开 PFC、ECN 先于 PFC、CNP 严格优先级等） | **通用做法 / 公开资料**；未在本集群交换机上验证 |
| 第 4 节的 DSCP 26 / 48 约定；Cumulus 的默认值与 `nv set qos roce` | **厂商文档**（Cumulus 5.6）；本集群交换机是否这样配置**未知** |
| ECN 位在 `-t 104` 下如何被网卡设置、交换机能否据此标记 | **未验证** |
| 网卡的信任模式是 DSCP 还是 PCP | **未知**（Pod 里读不到） |
| 本集群 `-t 104` 下主机内存 READ 稳定在 43.8 GB/s，`-t 0` 约 24.6 GB/s 且不稳 | **实测**（见 [c-multi-node-cpu.md](c-multi-node-cpu.md)） |
| 实验中网卡的拥塞、重传、丢弃类计数器没有增量 | **实测** |
| `-t 0` 与 `-t 104` 的差异说明 DSCP 在路径上某处被识别 | **推断**，未验证 |
| TC 影响 READ 的原因是队列配置不同 | **假设**，未证实 |
| 本集群交换机的 QoS 配置、实验时的网络负载 | **未知**，需交换机侧信息（[§6.1](#6) 清单） |

---

*本篇为通用说明，不含本集群交换机的实际配置；初版 2026-11-01，2026-11-03 重写。*
