# RoCE v2 网络的拥塞控制与 QoS

RoCE v2（RDMA over Converged Ethernet version 2，融合以太网上的远程直接内存访问第 2 版）把 RDMA（Remote Direct Memory Access，远程直接内存访问）放在以太网上跑。以太网默认遇到拥塞就丢包，而 RDMA 对丢包很敏感。所以承载 RDMA 的网络要靠一套机制来：**把流量分清楚、排对队、拥塞时提前降速、实在来不及就紧急暂停、让多条路径用得均匀**。

本篇讲的是 QoS（Quality of Service，服务质量）里与拥塞有关的部分，回答三个问题：**这套机制怎么工作、网卡和交换机各做什么、怎么配和怎么验证**。仓库里的相关文章：[rdma.md §9](rdma.md#9) 是一页纸入门；[roce-swtich.md](roce-swtich.md) 偏交换机配置清单与验证。

**读法**：只想快速了解，读[「三分钟读懂」](#sum)；想动手配置，读[第 11 节](#11)；论文里的数值与公式放在[附录 A](#A)。

**英文缩写**：每个缩写第一次出现时都给出英文全称和中文，全部缩写集中列在[第 14 节术语表](#14)。

**可靠度标记**：**论文 / 标准** = 来自文末列出的论文、电气电子工程师学会（IEEE，Institute of Electrical and Electronics Engineers）标准或请求评论文档（RFC，Request for Comments）；**厂商文档** = NVIDIA 等厂商公开文档（具体数值依赖型号和版本）；**第三方** = 博客、新闻稿，只作参考；**实测** = 在本集群 Pod 里看到的；**推断** = 由机制推导；**凭记忆** = 我记得但没有核对原文；**未验证** = 没有证据。**本篇所有机制说明都没有在本集群的交换机上验证过**（没有交换机配置的访问权限）。

<a id="toc"></a>
## 目录

- [三分钟读懂](#sum)
- [1. 问题与两道防线](#1)
- [2. 一次拥塞怎么被处理，谁做什么](#2)
- [3. 流量怎么分类和排队](#3)
- [4. 第一道防线：ECN 与 DCQCN（提前降速）](#4)
- [5. 第二道防线：PFC（紧急暂停）](#5)
- [6. 两道防线怎么配合](#6)
- [7. 通知包（CNP）与暂停帧（PFC）的区别](#7)
- [8. 多打一（incast）](#8)
- [9. 多路径：ECMP 与自适应路由](#9)
- [10. 其他拥塞控制算法](#10)
- [11. 实施步骤](#11)
- [12. 监控与排错](#12)
- [13. 与本集群的关系](#13)
- [14. 术语表](#14)
- [15. 参考资料](#15)
- [附录 A. 论文里的数值与公式](#A)
- [附录 B. 状态汇总](#B)

<a id="sum"></a>
## 三分钟读懂

[↑ 返回目录](#toc)

**问题**：RDMA 不能容忍丢包，以太网拥塞时却会丢包。

**解法：两道防线，一前一后**

| | 第一道：提前降速 | 第二道：紧急暂停 |
|---|---|---|
| 机制 | ECN（Explicit Congestion Notification，显式拥塞通知）加 DCQCN（Data Center Quantized Congestion Notification，数据中心量化拥塞通知） | PFC（Priority-based Flow Control，基于优先级的流量控制） |
| 什么时候出手 | 队列**刚变长** | 缓冲**快满了** |
| 做法 | 交换机给包盖"堵车戳"，接收方网卡发通知包（CNP，Congestion Notification Packet，拥塞通知包），发送方网卡降速 | 交换机让**上一级设备**停发 |
| 角色 | 主力 | 最后的保险 |

两道缺一不可：只有降速，网卡全速起步，来不及时会丢包；只有暂停，暂停会扩散，拖慢无关流量，严重时卡死。

**谁做什么**：**降速完全在网卡里**（发送方网卡降速，接收方网卡发通知包）；交换机在降速机制里只负责**按阈值盖戳**；暂停由交换机或网卡发出，对端响应。

**配置的核心**：让 RDMA 流量带固定的 DSCP（Differentiated Services Code Point，差分服务代码点）标记，走专用队列；网卡和交换机都信任 DSCP；**只在 RDMA 这一个优先级上开 PFC**；ECN 阈值低于 PFC 触发阈值。

**多打一（incast）**：多个发送方同时向同一个接收方发数据（比如多个 Prefill 向一个 Decode 传 KV cache），拥塞点在接收方一侧，降速来不及，更依赖 PFC。

**本集群**：测试程序已用 `-t 104`（DSCP 26）打标记；主机侧和交换机侧的 QoS 配置**读不到**，未知；Pod 里能用网卡计数器做验证。

<a id="1"></a>
## 1. 问题与两道防线

[↑ 返回目录](#toc)

### 1.1 为什么 RDMA 怕丢包

| 原因 | 说明 | 来源 |
|---|---|---|
| **可靠连接（RC，Reliable Connection）假定网络不因拥塞丢包** | RDMA 传输协议按"网络无损"设计；其他原因的丢包（校验错误、交换机缺陷）仍会发生 | 论文 |
| **丢包的恢复代价很大，甚至活锁** | 微软的论文：网卡早期实现 **go-back-0**（丢一个包就从消息第 0 个包重发）。交换机按 1/256（0.4%）的规则丢包，4 MB 的消息拆成约 4000 个包，**应用层吞吐降为 0**——链路满速、应用无进展，即"活锁"（livelock）。改成 **go-back-N**（从丢失的那个包开始重发）后没再出现 | 论文 |
| **RDMA 流量是少数几条大流** | AI（Artificial Intelligence，人工智能）训练等场景里流数少、带宽大，容易在某条链路集中，造成突发拥塞 | 论文 / 通用 |
| **与 TCP 混用会互相影响** | TCP（Transmission Control Protocol，传输控制协议）靠丢包感知拥塞，RDMA 靠流控与 ECN；放在同一个队列，谁的行为都不可预期 | 通用 |
| **拥塞通知不能被拖慢** | 通知发送方降速的报文延迟大了，降速就太晚 | 论文 |

### 1.2 两道防线

- **第一道（提前降速）**：队列刚开始变长，就让发送方自己降一点速度。温和，不影响别人。
- **第二道（紧急暂停）**：降速来不及，缓冲眼看要满，就让上一级设备**直接停发**。粗暴，但立刻见效、保证不丢包。

比喻：高速公路收费站。第一道像导航提醒"前面堵了"，司机自己松油门，但司机反应需要时间；第二道像交警直接拦车，立刻见效，但会连带堵住后面的车。

| | 第一道：提前降速（ECN + DCQCN） | 第二道：紧急暂停（PFC） |
|---|---|---|
| **什么时候出手** | 队列**刚变长**就触发 | 缓冲**快满了**才触发 |
| **谁来做** | 交换机盖戳；接收方网卡通知发送方网卡；发送方网卡降速。管**整条路径** | 交换机通知它的**上一级设备**。只管**一段链路** |
| **效果** | 发送方**降低速度**，仍在发 | 上一级**完全停发**，数据暂时停在原地 |
| **优点** | 温和、精确，不连累别的流量 | 快、有保证，一定不丢包 |
| **缺点** | 要经过一个来回才起作用，可能来不及 | 粗暴，会波及无关流量；严重时整个网络卡死 |

**理想状态**：第一道先起作用，第二道只当最后的保险；第一道做得越早，第二道越少触发。

<a id="2"></a>
## 2. 一次拥塞怎么被处理，谁做什么

[↑ 返回目录](#toc)

### 2.1 处理流程

```
1. 发送方网卡发出的包，IP（Internet Protocol，网际协议）头里带着"这是 RDMA 流量"的标记（DSCP）
2. 交换机队列变长 → 在包上盖"堵车了"的戳（ECN 标记，即 CE，Congestion Experienced，经历拥塞）
3. 接收方网卡看到戳 → 给发送方发一个"通知包"（CNP）
4. 发送方网卡收到通知包 → 降低该连接的发送速度（DCQCN 算法）
5. 如果降速来不及、缓冲眼看要满 → 第二道防线：交换机让上一级暂停（PFC）
```

**ECN 的通知是绕一圈的**：交换机只能改正在转发的数据包，不会直接联系发送方；第一个知道堵车的是**接收方网卡**，再由它发通知包回发送方。

```
发送方 ──数据包──> 交换机（盖戳）──数据包（带戳）──> 接收方
   ^                                                  │
   └──────────────── 通知包（CNP）─────────────────────┘
```

这多出来的一个来回，就是降速有延迟、有时来不及的原因，也是需要第二道防线兜底的原因。暂停是交换机直接通知上一级，不用绕圈，所以快。

### 2.2 网卡做的事

| 角色 | 做什么 |
|---|---|
| **发送方网卡** | ① 给发出的包**打标记**（IP 头里的 DSCP，约定用 26，即 `traffic_class = 104`）<br>② 收到 CNP 后**降低该连接的发送速度**，之后按定时器慢慢恢复（DCQCN 在网卡里运行）<br>③ 收到暂停消息后**停发**对应优先级的流量 |
| **接收方网卡** | ④ 发现收到的包上有"堵车戳"，就**生成并发送 CNP**，并让 CNP 走高优先级 |
| **两端网卡** | ⑤ 配置"按标记区分流量"和"只对 RDMA 类开启暂停"（`mlnx_qos`）<br>⑥ 接收太慢时（比如网卡里的 MTT，Memory Translation Table，内存地址翻译表缓存不命中），**网卡自己也会向交换机发暂停消息**（[§5.3](#5)） |

**降速由网卡的硬件和固件直接完成，不经过 CPU（Central Processing Unit，中央处理器）和操作系统**，应用感知不到；CPU 只参与**配置**。原因是降速要在微秒级响应，经 CPU 来不及。（由 DCQCN 的设计**推断**；论文说它"已在 Mellanox 网卡中实现"，具体是固件还是硬件逻辑**未验证**。）

### 2.3 交换机做的事

| 做什么 | 说明 |
|---|---|
| ① **按标记把包分到不同队列** | RDMA 数据一个队列，CNP 一个队列，其他流量走默认队列 |
| ② **盖"堵车戳"（ECN 标记）** | 队列超过阈值就盖戳，而不是丢包。**这是交换机在降速机制里唯一要做的事** |
| ③ **发暂停消息（PFC）** | 缓冲快满时，通知上一级设备暂停 |
| ④ **预留缓冲** | 为"暂停消息发出后、上游真正停下前"还会到达的数据预留空间（headroom，预留空间），不预留仍会丢包 |
| ⑤ **调度** | 给 CNP 开优先通行的队列，不能被暂停 |
| ⑥ **保护机制** | PFC 看门狗（watchdog，暂停风暴保护）；广播和组播不进无损队列 |
| ⑦ **多路径** | 把不同的流分散到不同链路（哈希、自适应路由，[§9](#9)） |

### 2.4 要点

- **降速完全在网卡里**：交换机只盖戳，不知道也不管发送方降不降速。
- **交换机必须支持两个功能**：按阈值打 ECN 标记；PFC 暂停。DCQCN 论文强调它**只要求交换机支持 RED（Random Early Detection，随机早期检测）和 ECN 标记**，其余都在网卡里完成。
- **暂停是一端发出、对端响应**：双方都要开启，并且只对同一个优先级开启。
- **标记要全程一致**：网卡打的标记，路径上每一台交换机都要用同样的规则识别，错一台，那一段就落到默认队列，保护失效。

### 2.5 网卡自己决定发包，为什么还会拥塞

一个常见的疑问：RDMA 网卡自己决定什么时候发，是不是就不存在"发包拥塞"？答案：**网卡本地不会因为发太快而丢包，但拥塞仍然存在，只是不发生在发送网卡里**。（由 RDMA 的工作方式**推断**，未在本集群验证。）

- **网卡本地为什么不丢包**：要发的数据以 WQE（Work Queue Element，工作队列元素）的形式排在主机内存里，网卡按自己的节奏取出来发。同一个端口上有多个 QP（Queue Pair，队列对，RDMA 的一条连接）时，网卡在它们之间做本地调度，发不完的留在内存里，不会被丢掉。这是无损的本地反压。
- **拥塞发生在别处**：

| 位置 | 说明 |
|---|---|
| **交换机的出口队列** | 多条流汇入同一个出口（incast），网卡发得再有节奏，到了交换机照样堆积。这是主要的拥塞点 |
| **接收方的入向链路和网卡** | 多打一时汇聚点在接收端；接收方网卡处理变慢也会引发拥塞和暂停 |
| **发送方自己的端口** | 多个 QP 抢同一个端口的带宽，但这只是本地排队，没有丢包问题 |

- **发送网卡掌握发送节奏，恰好是拥塞控制能起作用的前提**：DCQCN 的降速就是网卡把某个 QP 的发送节奏放慢；PFC 暂停传到网卡时，网卡也是停止发送，数据留在主机内存里。

一句话：**发包由网卡掌控，所以"刹车"装在发送网卡上；但拥塞发生在网络中间和接收端，网卡看不到，要靠 ECN 和 CNP（或 PFC）告诉它。**

<a id="3"></a>
## 3. 流量怎么分类和排队

[↑ 返回目录](#toc)

两道防线都建立在一个前提上：**RDMA 流量被识别出来，进了专用队列**。

### 3.1 两个分类字段：PCP 与 DSCP

| | PCP（Priority Code Point，优先级代码点） | DSCP（差分服务代码点） |
|---|---|---|
| 位置 | 二层，802.1Q VLAN（Virtual Local Area Network，虚拟局域网）标签里的 3 位 | 三层，IP 头 Traffic Class（流量类别）字节的高 6 位 |
| 取值 | 0–7 | 0–63 |
| 能否跨子网 | **不能保证**：三层网络里没有标准办法在跨子网时保留 VLAN 的 PCP | **能**：DSCP 随 IP 报文走 |
| 另一个问题 | 与 VLAN ID 绑在同一个标签里，网口要配成 trunk 模式，会影响 PXE（Preboot Execution Environment，预启动执行环境）启动等 | 数据报文不再需要 VLAN 标签 |

微软在 2016 年的论文里因为这两个问题，推动了**基于 DSCP 的 PFC**：优先级从 VLAN 标签挪到 DSCP，PFC 暂停帧本身的格式不变（它本来就不带 VLAN 标签）。（**论文**）

**PCP 与 PFC、与 RoCE v2 的关系**

- **PCP 不是 PFC 专用的**：它是通用的二层优先级字段，所有流量的分类、排队、调度都能用，和 PFC 无关。
- PFC 的"优先级"也是 0–7 共 8 个，因为 PFC 标准（IEEE 802.1Qbb）是在 802.1Q 的 8 级优先级基础上设计的。早期做法是：数据帧的 PCP 值决定它属于哪个优先级，暂停帧里写"暂停优先级 3"，上游就停发 PCP = 3 的帧。所以 PCP 是 PFC **最早的分类依据**，但只是分类依据：这个信息也可以从 DSCP 来。
- **PCP 对 RoCE v2 有用，但作用有限**：只在同一个二层网络、带 VLAN 标签、两端都信任 PCP 时可用；RoCE v2 本来是为了能路由，PCP 跨子网保留不了，叶脊网络里用它做端到端优先级不可靠。主流做法是 DSCP。
- **对本集群的提醒**（**推断**，未验证）：我们的脚本 `-t 104` 写的是 IP 头里的 DSCP 26，**不涉及 PCP**。如果网卡实际信任的是 PCP、而数据帧又没有 VLAN 标签，网卡侧就不会把流量归到预期的优先级，网卡一侧的 PFC 与调度保护不到它（交换机仍可能按 DSCP 分类）。可以用 `mlnx_qos -i <网口>` 看 `Priority trust state` 核实，或用[§12.2](#12)的按优先级计数器做旁证。

### 3.2 traffic_class：一个字节，两个字段

IP 头里有一个 **8 位（一个字节）** 的 Traffic Class 字段（IPv4 里叫 ToS，Type of Service，服务类型），被切成两段：

```
        一个字节（8 位）
 ┌───────────────────┬───────┐
 │   DSCP（高 6 位）   │ ECN   │
 │   这是哪一类流量     │（低 2 位）│
 │   用于分队列、做 PFC │ 拥塞标记 │
 └───────────────────┴───────┘
```

程序里设的 `traffic_class`（比如 `-t 104`）写的是**这一整个字节**，不是只写 DSCP。

**为什么是 `DSCP × 4 + ECN`**：DSCP 在高 6 位，放进字节要**左移 2 位**，左移 2 位就是乘以 4；低 2 位留给 ECN，所以再加上 ECN 的值。反过来：DSCP = traffic_class ÷ 4 取整，ECN = 余数（0–3）。

| 设的值 | 二进制 | 怎么算 | DSCP | ECN |
|---|---|---|---|---|
| **104** | `0110 1000` | 26 × 4 + 0 | **26**（AF31，Assured Forwarding 31，确保转发 31；RoCE 数据常用） | 0（Not-ECT，不支持 ECN） |
| 106 | `0110 1010` | 26 × 4 + 2 | 26 | 2（ECT(0)，ECN-Capable Transport，支持 ECN） |
| 107 | `0110 1011` | 26 × 4 + 3 | 26 | 3（CE，已遇到拥塞） |
| **192** | `1100 0000` | 48 × 4 + 0 | **48**（CNP 常用） | 0 |
| 0 | `0000 0000` | 0 × 4 + 0 | 0（默认，走默认队列） | 0 |

**104 和 106 的 DSCP 都是 26，进同一个队列**；区别只在 ECN 位，决定交换机能不能盖"堵车戳"。

**需要留意**（**未验证**）：`-t 104` 的 ECN 位是 0（Not-ECT），按规则 Not-ECT 的包交换机通常不会标记；但网卡启用拥塞控制时会不会自己把这 2 位改成 ECT，因网卡与配置而异，**本篇没有验证**。如果测试里完全看不到 ECN 标记（`rx_prio3_marked`、`rx_ecn_mark` 不涨），除了交换机没配 ECN，这也是要排查的方向。

**在哪里设**：verbs 里在 QP 切到 RTR（Ready To Receive，准备接收）状态时写入 `ah_attr.grh.traffic_class`（[rdma.md §9.1](rdma.md#9)）；NCCL（NVIDIA Collective Communications Library，NVIDIA 集合通信库）对应 `NCCL_IB_TC`，MTE（Mooncake Transfer Engine，Mooncake 传输引擎）对应 `MC_IB_TC`（官方文档的说法，本仓库没有设置过）。**每一端只管自己发出去的包**，所以 RDMA READ 的数据（由被读方发出）要靠被读方那端的设置。

### 3.3 信任模式与映射链

```
报文的 DSCP（或 PCP）
   │  信任模式（trust mode）：网卡 / 交换机以哪个字段为准
   ▼
switch priority（交换机优先级，0–7）      ← PFC 就是按它区分的
   │  priority → traffic class 映射
   ▼
traffic class（队列）                     ← 缓冲、调度、ECN 阈值按它配置
```

- **映射必须整条路径一致**：网卡的"DSCP → 优先级"、交换机的"DSCP → 优先级 → 队列"在每一跳都要对得上。
- 注意：交换机里的 traffic class 指**队列**，与 IP 头的 Traffic Class 字节**不是一回事**。
- NVIDIA 交换机的默认示例（**厂商文档**，Cumulus Linux 5.6）：port 信任 PCP 与 DSCP；DSCP 每 8 个一组映射到一个 switch priority；**switch priority 3 → traffic class 3（RoCE），switch priority 6 → traffic class 6（CNP），其余 → traffic class 0**。所以 DSCP 26 落到 priority 3，DSCP 48 落到 priority 6。

### 3.4 队列与调度

| 调度方式 | 含义 | 常用于 |
|---|---|---|
| **SP**（Strict Priority，严格优先级） | 高优先级队列只要有包就先发 | 要求低时延的 CNP |
| **WRR**（Weighted Round Robin，加权轮询）/ **DWRR**（Deficit Weighted Round Robin，差额加权轮询） | 按权重分配带宽 | RDMA 数据与其他流量按比例共享 |
| **ETS**（Enhanced Transmission Selection，增强传输选择，IEEE 802.1Qaz） | 按权重给不同流量类别分配带宽，空闲带宽可被别的类别借用；最多 8 个类别；标准**不规定**具体调度算法 | 通用的带宽保证（**标准**） |
| **整形 / 限速（maxrate）** | 限制某个队列或端口的最大速率 | 避免某类流量吃光链路 |

- **DCBX**（Data Center Bridging Exchange，数据中心桥接交换协议，也在 802.1Qaz）：基于 **LLDP**（Link Layer Discovery Protocol，链路层发现协议，IEEE 802.1AB）的协商协议，用来发现对端的 DCB（Data Center Bridging，数据中心桥接）能力、检测配置不一致、下发配置。RoCE 常见做法是在 LLDP 里带一个 **Application TLV**（TLV，Type-Length-Value，类型-长度-值），声明"UDP（User Datagram Protocol，用户数据报协议）目的端口 4791 → 优先级 3"。（**标准**）
- NVIDIA 交换机的默认 ETS 示例（**厂商文档**）：traffic class 6（CNP）严格优先级；traffic class 3（RoCE）DWRR 50%；其余 DWRR 50%。
- 原则：**CNP 要走严格优先级、且不被 PFC 暂停**，拥塞通知才不会被拖慢。

<a id="4"></a>
## 4. 第一道防线：ECN 与 DCQCN（提前降速）

[↑ 返回目录](#toc)

### 4.1 ECN 的编码（RFC 3168，**标准**）

IP 头 Traffic Class 的低 2 位是 ECN 字段：

| 值 | 名称 | 含义 |
|---|---|---|
| `00` | Not-ECT（Not ECN-Capable Transport，不支持 ECN 的传输） | 该报文的传输层不支持 ECN |
| `10` | ECT(0)（ECN-Capable Transport，支持 ECN 的传输） | 支持 ECN（发送方标记） |
| `01` | ECT(1) | 支持 ECN（另一种标记） |
| `11` | **CE**（经历拥塞） | 途中遇到拥塞（被交换机改写） |

发送方把报文标成 ECT；交换机在拥塞时把 ECT 改成 CE，**而不是丢包**；接收方看到 CE，就把拥塞信息反馈给发送方。**Not-ECT 的报文交换机通常不会标记**。

### 4.2 交换机怎么标记：RED / WRED

交换机在**出口队列**上按队列深度概率性标记（**论文**）：

```
标记概率 p
  Pmax ┤                     ╱─────── 1（超过 Kmax 全部标记）
       │                  ╱
       │               ╱   在 Kmin 与 Kmax 之间，p 从 0 线性升到 Pmax
     0 ┼────────────╱
       └──────────┬──────────┬────── 出口队列深度
                 Kmin       Kmax
```

- 队列 ≤ **Kmin**：不标记；Kmin 到 **Kmax** 之间：以 0 到 **Pmax** 的概率标记，随深度线性增大；> Kmax：全部标记。设 Kmin = Kmax、Pmax = 1，就是 DCTCP（Data Center TCP，数据中心 TCP）的"超过阈值就全标"。
- 论文在 40 Gbps 网络上用的是 **Kmin = 5 KB，Kmax = 200 KB，Pmax = 1%**，**不是通用数值**。
- NVIDIA 交换机默认示例（**厂商文档**，Cumulus Linux 5.6）：对 RoCE 的 traffic class 3 启用 ECN，绝对阈值，最小约 150 000 字节、最大约 1 500 000 字节、概率 100。与论文差别很大，因为链路速率、缓冲和型号都不同。
- 阈值可按**绝对字节数**，也可按缓冲的**百分比**配置。

### 4.3 DCQCN：网卡里的降速算法

DCQCN 是 RoCE v2 上最常见的降速算法，由微软与 Mellanox 提出（SIGCOMM，ACM Special Interest Group on Data Communication，ACM 数据通信特别兴趣组年会，2015，**论文**），已在 Mellanox 网卡里实现。

**三个角色**

| 角色 | 谁 | 做什么 |
|---|---|---|
| **CP**（Congestion Point，拥塞点） | 交换机 | 出口队列超过阈值时给报文标 CE |
| **NP**（Notification Point，通知点） | 接收端网卡 | 收到 CE 报文后，给发送方发 CNP |
| **RP**（Reaction Point，反应点） | 发送端网卡 | 收到 CNP 后降低该连接（QP）的发送速率，之后慢慢恢复 |

**降速与恢复**（公式与参数见[附录 A](#A)）

- **降速**：每收到一个 CNP，当前速度乘以 (1 − α/2)，并记住降速前的速度作为恢复目标。α 是网卡对"堵得多严重"的估计：CNP 来得越密，α 越高，降得越狠；一段时间没收到，α 逐渐衰减。
- **恢复**分三个阶段：快速恢复（连续几次向降速前的速度走一半）；加性增（之后每次增加一个固定小量）；超级增（快速提速）。
- **通知节流**：接收方对每条连接，收到带戳的包就立刻发一个 CNP，之后至多每 50 µs 发一个。

**特点**

| 特点 | 说明 |
|---|---|
| **几乎全在网卡里** | 交换机只需支持 RED / ECN 标记，所以能在现成的以太网交换机上部署 |
| **对每条连接单独降速** | 对象是单个 QP，不是整个端口，别的连接不受影响 |
| **没有慢启动，从线速开始** | 和 TCP 最大的区别。网络不堵、流又小时，一开始就跑满，对短流最划算 |
| **所以不能取代 PFC** | 全速起步意味着第一波冲击它自己挡不住，没有 PFC 就会丢包 |

**缺点**：参数多，不同网络要重新调（HPCC（High Precision Congestion Control，高精度拥塞控制）论文摘要性资料提到约 15 个，**第三方摘要**，没核对原文）；只有"超过阈值"一位信息，不知道超过多少，这是后来 HPCC 等算法想改进的地方（[§10](#10)）。

**名字里的 "Quantized"（量化）怎么理解**

"量化"的本意是把连续的拥塞程度压成有限的几档数值再反馈，这个词从前身 **QCN**（Quantized Congestion Notification，量化拥塞通知，IEEE 802.1Qau）继承而来：

| | QCN | DCQCN |
|---|---|---|
| 反馈内容 | 交换机算出"队列比目标长多少、涨得多快"，**量化成几个比特**（我记得是 6 位）发回发送方，带有程度信息 | **只有 ECN 一位**：盖了戳，或没盖 |
| 工作层次 | 二层，不能跨 IP 子网 | 三层，用 IP 头里的 ECN 和 CNP，能跨子网 |
| 降速算法 | 降速、快速恢复、加性增 | **借用了 QCN 的这一套** |
| 对交换机的要求 | 要实现 QCN 的反馈计算 | 只要支持现成的 ECN 标记 |

所以 Quantized 是**出身**：继承的是 QCN 的降速算法，真正的反馈只有一位。程度是**发送方网卡自己估计**的——用 α 把一位信号在时间上累积成连续的"拥塞程度"。（QCN 的"6 位反馈""只能在二层"**凭记忆**，未核对标准原文。）

### 4.4 常见的"没生效"原因

| 现象 | 可能原因 |
|---|---|
| 没有任何 CNP | 报文不带 ECT，交换机不会标记；或该优先级上交换机没配置 ECN；或 CNP 被丢 / 没走高优先级 |
| 有 CNP 但降速不明显 | 阈值设得太高（缓冲先满、PFC 先触发） |
| 开了 PFC 之后 DCQCN 不工作 | 有用户在 NVIDIA 论坛报告"启用 PFC 后 CNP 不再产生"，他怀疑问题在交换机一侧（**第三方**，一个用户的经验，不是已验证的结论） |

<a id="5"></a>
## 5. 第二道防线：PFC（紧急暂停）

[↑ 返回目录](#toc)

### 5.1 PFC 是什么（IEEE 802.1Qbb，**标准**）

- 以太网传统的暂停（802.3x）作用于整个端口：暂停所有流量。**PFC 是它的"按优先级"版本**：只暂停某一个优先级，其他优先级照常发送。
- 机制：交换机某个入口队列（对应一个优先级）占用超过 **xoff** 时，向**上游**发一个 **PAUSE 帧**（暂停帧，携带"暂停哪些优先级、多久"），上游收到后停发该优先级；队列降回 **xon** 时恢复。
- 目的：**消除因拥塞造成的丢包**。所以无损队列通常只在 RDMA 数据所在的那一个优先级上开 PFC。
- 每个端口有 8 个**优先级组**（PG，Priority Group），每个可配置为无损（lossless）或有损（lossy）。（**论文**）

### 5.2 "上一级"是谁，暂停怎么往回传

"上一级"可以是**交换机，也可以是网卡**：靠近发送方的那一跳，上一级是**发送方网卡**；多级网络里，上一级是**另一台交换机**；方向也可以反过来，网卡接收太慢时，网卡会向交换机发暂停。

**暂停帧本身只管一段链路**，但**效果**会一级一级往回扩散，靠的是缓冲涨起来：

1. 交换机 A 缓冲快满，向上一级 B 发暂停；
2. B 停发，数据堆在 B 的缓冲里；
3. B 的缓冲涨到阈值，B 再向它的上一级 C 发**一个新的**暂停帧；
4. 如此传到发送方网卡。

好处：每一跳都有"满了就让上一跳停"的保护，所以不丢包。副作用：暂停传到的地方，**同一优先级上无关的流量也会被一起停住**（队头阻塞），严重时形成环路，整个网络卡死。

### 5.3 PFC 的问题（**论文**，微软 2016）

| 问题 | 现象与原因 | 对策 |
|---|---|---|
| **队头阻塞**（HOL，Head-of-Line blocking） | 被暂停的流把同一队列里无关的流也卡住 | 只在必要的优先级开 PFC；用 ECN 减少 PFC 触发 |
| **受害流**（victim flow） | 暂停沿路传播，让与拥塞无关的流也变慢；论文用 DCQCN 解决了这类现象 | DCQCN |
| **PFC 死锁** | **以太网包泛洪**与**暂停帧传播**相互作用：交换机上某目的的 ARP（Address Resolution Protocol，地址解析协议）表项不完整时，包被泛洪，泛洪的包又走进无损队列，形成环形缓冲依赖，暂停帧环形传播，网络停转 | **无损报文不要泛洪**；**广播和组播不进无损队列** |
| **暂停风暴**（PFC storm） | 故障网卡（接收流水线卡死）**持续发暂停帧**，ToR（Top of Rack，机架顶部交换机）、汇聚、核心依次被暂停，**一块网卡能让整个网络停转** | **看门狗**：网卡侧与交换机侧各一套，**建议都实现**（细节见[附录 A](#A)） |
| **慢接收者** | 服务器网卡本不该发 PFC，论文却观察到不少服务器每秒发出上千个暂停帧：MTT 只有 2K 项，4 KB 页只够覆盖 8 MB 内存，缓存缺失要去主存取，接收变慢，缓冲超过阈值，网卡向交换机发暂停 | 网卡侧用 **2 MB 大页**；交换机侧开**动态共享缓冲**。与 [rdma.md §4](rdma.md#4) 的"翻译表以页为粒度"是同一件事 |
| **RDMA 传输活锁** | 见[§1.1](#1) | 网卡实现 go-back-N，不要 go-back-0 |

### 5.4 PFC 的实用规则

- **只在 RDMA 数据所在的一个优先级上开 PFC**（常见 priority 3）；
- **CNP 所在的优先级不开 PFC**，用严格优先级；
- 全网**优先级到队列的映射一致**，headroom 足够；
- 开启 **PFC 看门狗**；广播、组播不进无损队列；
- 理想状态是 **ECN 先于 PFC**，PFC 只作最后保险。

有的方案选择**不开 PFC**，只靠 ECN 与网卡的选择性重传：避免队头阻塞、暂停风暴、死锁，代价是丢包时有性能损失（Ultra Ethernet 也走这个方向，[§10](#10)）。NVIDIA Cumulus 的 `nv set qos roce` 默认是**无损模式**（PFC + ECN），`nv set qos roce mode lossy` 是有损模式（只用 ECN）。

<a id="6"></a>
## 6. 两道防线怎么配合

[↑ 返回目录](#toc)

要让第一道先起作用，需要把阈值配对：**ECN 的标记阈值要低于 PFC 的 xoff 阈值**。

| 概念 | 含义 |
|---|---|
| **headroom**（预留空间） | 交换机发出 PAUSE 之后，到上游真正停下之前，仍会有数据到达（在线路上的、上游处理 PAUSE 期间发出的、已开始发送的一个帧不能中断）。必须**预留**，否则仍会溢出丢包 |
| **xoff / xon** | PFC 的两个阈值：入口队列占用到 **xoff** 时发 PAUSE；降回 **xon** 时发 RESUME |
| **Kmin / Kmax** | ECN 的标记阈值（[§4.2](#4)） |

交换机的缓冲通常是**共享缓冲**，分成入端口方向与出端口方向的若干**缓冲池**；无损队列使用专门的池。

**结论**（**论文**）：合理设置阈值，只保证"PFC 不会在 ECN 之前触发"，**不保证 PFC 永远不触发**——发送方从收到 ECN 反馈到降速需要时间，期间 PFC 仍可能触发。headroom 设小了仍会丢包，设大了浪费缓冲；它与**线缆长度、链路速率、MTU**（Maximum Transmission Unit，最大传输单元）有关，通用做法是用厂商工具计算，不要手算。论文里的计算示例见[附录 A](#A)。

<a id="7"></a>
## 7. 通知包（CNP）与暂停帧（PFC）的区别

[↑ 返回目录](#toc)

两道防线各有一种消息，容易混淆，它们是完全不同层次的东西。

| | 通知包（CNP） | 暂停帧（PFC） |
|---|---|---|
| **属于哪一层** | **三层 / 四层**：RoCE v2 报文，装在 UDP 里 | **二层**：以太网控制帧，没有 IP 头，也没有 UDP 头 |
| **是不是 RDMA 协议的一部分** | 是，RoCE v2 规范里定义的一种报文，靠 RDMA 头里的**操作码**区分 | 不是，是以太网标准（IEEE 802.1Qbb） |
| **传多远** | 从接收方网卡到发送方网卡，**穿过整个网络**，途中被交换机当普通 UDP 包转发 | 只在**相邻两台设备之间**，处理完就结束，不会再转发 |
| **谁识别它** | 只有两端网卡认识；交换机不需要认识，只按 DSCP 把它放进高优先级队列 | 链路两端的设备（交换机或网卡） |
| **内容** | 很小，只说明"哪条连接堵了" | "暂停哪些优先级、暂停多久" |
| **对应哪道防线** | 第一道：提前降速 | 第二道：紧急暂停 |

**CNP 的格式**：

```
以太网头 | IP 头 | UDP 头（目的端口 4791） | RDMA 的 BTH 头（Base Transport Header，基本传输头；操作码标明这是 CNP） | 校验
```

和普通 RDMA 数据包用同一种外壳，所以交换机无需认识它；DSCP 约定用 48，让交换机把它放进优先通行的队列。规范只定义"收到带戳的包就发 CNP"和 CNP 的格式，**没有规定发送方收到后怎么降速**，降速算法（如 DCQCN）由网卡厂商实现，可以替换。

**暂停帧的格式**：以太网头（目的 MAC，Media Access Control，介质访问控制地址，是固定的组播地址，类型 0x8808）加控制内容。

> 注：CNP 的操作码（我记得是 `0x81`）、它在 InfiniBand 规范 RoCE v2 附录里的位置、暂停帧的固定目的地址与类型值，**凭记忆**，没有对照规范原文核对。能确认的是：CNP 是 RoCE v2 标准里定义的报文；PFC 是 IEEE 802.1Qbb，暂停帧本身不带 VLAN 标签。

<a id="8"></a>
## 8. 多打一（incast）

[↑ 返回目录](#toc)

**incast**（多打一）指多个发送方同时向同一个接收方发数据，汇聚点是接收方所在交换机的出口端口和接收方网卡的入向链路。接收端口带宽固定，N 个发送方各自按线速发，汇入速率可到 N 倍，多出来的流量只能堆在出口队列里，缓冲在微秒级时间内就可能被填满。

### 8.1 为什么它对 RoCE 特别难

- DCQCN 线速起步、没有慢启动，incast 一来，**ECN 标记、CNP 返回、发送方降速这一轮往返之前，缓冲可能已经满了**，于是落到 PFC 上（论文加**推断**）。
- 大块数据（如 KV cache）传输持续时间长，出口队列容易**持续堆积**，PFC 被触发的概率更高（**推断**）。

### 8.2 典型场景

以下场景由工作方式**推断**，**未在本集群测过**。

| 场景 | 谁发、谁收 | 拥塞点 | 特点 |
|---|---|---|---|
| **PD 分离的 KV cache 传输**（PD，Prefill-Decode disaggregation，预填充-解码分离；KV cache，Key-Value cache，键值缓存） | 多个 Prefill（预填充）同时把 KV cache 传给同一个 Decode（解码）节点 | Decode 一侧的入向链路 | 数据量大、持续时间长；谁发起传输（WRITE 还是 READ）决定能否控制并发（见下） |
| **KV cache 池化的多点读取**（如 Mooncake 这类把 KV cache 放在共享缓存池里的方案） | 一个请求命中的 KV cache 分散在多个缓存节点，这些节点同时回给同一个 Decode | 该 Decode 的入向链路 | 与分布式存储的并行读同构（我对 Mooncake 的存储方式是**凭一般理解**，未核对文档） |
| **MoE 的 all-to-all**（MoE，Mixture of Experts，混合专家模型；all-to-all，全对全通信） | 每个 rank（参与计算的进程）把 token 发给各专家所在 rank，某一时刻多个 rank 同时发给同一个 rank | 热门专家所在 GPU（Graphics Processing Unit，图形处理器）的入向链路 | 专家负载不均时，热门专家成为长期热点；EP（Expert Parallelism，专家并行）规模越大，扇入越大 |
| **gather / reduce 到根节点**（gather，收集；reduce，归约） | 所有 rank 把数据发给 rank 0 | 根节点的入向链路 | 发送方几乎同时开始，扇入数就是 rank 数 |
| **参数服务器的梯度上传**（Parameter Server，参数服务器） | 多个 worker（工作节点）同时把梯度推给同一个参数服务器 | 参数服务器的入向链路 | 每轮迭代的同步点上周期性爆发 |
| **分布式存储的并行读** | 文件条带化到多个存储节点，读取时它们**同时**把各自那一份回给同一个客户端 | 客户端的入向链路 | incast 最经典的来源（最早在 TCP 上被发现，称 TCP incast） |
| **Checkpoint（检查点）保存** | 大量 GPU 节点同时把检查点写向同一个存储节点或网关 | 存储端的入向链路 | 数据量大、持续时间长、全体同时开始，最严重的一类 |

**共同点**：① 同步，多个发送方几乎同时开始；② 汇聚，目的地是同一个网口；③ 每个发送方的数据量够大，能在极短时间内填满出口队列；④ 扇入越大越严重，过载倍数 ≈ 同时发送数 × 线速 ÷ 接收端口带宽。

**WRITE 与 READ 的区别**（**推断**）：**WRITE**（Prefill 主动推）：多个 Prefill 同时推就是 incast。**READ**（Decode 主动拉）：数据仍由 Prefill 一侧发出，但 Decode 能自己限制并发拉取的数量，可以缓解 incast。

**相关但不是 incast：一打多**（fan-out，扇出）。多个 Decode 同时从同一个 Prefill 或缓存节点拉数据，瓶颈在**发送方自己的端口**，是本地排队，数据留在主机内存里，不在交换机里堆积（[§2.5](#2)）。

### 8.3 怎么确认是不是 incast（建议做法，未在本集群实施）

1. 在接收节点网卡上，传输前后各取一次 `ethtool -S`，看 `rx_prioN_marked`、`rx_ecn_mark`、`rx_pause_ctrl_phy`、`rx_prioN_discards` 的差值（[§12](#12)）；
2. 控制变量：把同时向同一个接收方传输的发送方数从 1 逐步加到 N，看**单条传输的带宽与尾延迟**是否随 N 恶化得比"总带宽均分"更严重；
3. 单发送方正常、只有多发送方时出现 TTFT（Time To First Token，首词元时延）或 KV 传输的尾延迟毛刺，才指向 incast；
4. 本集群没有交换机访问权限，只能用主机侧计数器做间接判断。

### 8.4 缓解思路（通用）

用 READ 由接收方控制并发；接收侧限制同时接收的发送方数；ECN 阈值调低，让 DCQCN 更早介入；把流量分散到不同的接收节点或网卡；让不同 QP 走不同路径（[§9](#9)）。

<a id="9"></a>
## 9. 多路径：ECMP 与自适应路由

[↑ 返回目录](#toc)

### 9.1 ECMP（Equal-Cost Multi-Path，等价多路径）及其问题

| 问题 | 说明 |
|---|---|
| **ECMP 哈希** | 交换机按五元组哈希把流分散到多条等价路径。RoCE v2 用 UDP 封装，**目的端口固定 4791，源端口按 QP 变化**（通用说法），所以多个 QP 可能走不同路径 |
| **大流与哈希极化** | RDMA 流少而大，哈希不均时多条大流挤在同一条链路，别的链路闲置 |
| **选路不看负载** | 路径一旦选定，即使这条路已经堵了，流也不会换；链路故障后收敛也较慢 |
| **手工分散** | 让多个 QP 的 UDP 源端口不同。MTE 提供环境变量 `MC_MLX5_QP_UDP_SPORTS`，可逐个 QP 覆盖 RoCE v2 的 UDP 源端口（**文档**，本仓库没用过） |

### 9.2 自适应路由（Adaptive Routing，AR）

qos.md 原先只记录了"交换机按链路实时负载选路""包喷洒需要网卡重排"两句；本节其余内容由一般原理梳理，**没有核对厂商文档**。

**做法**：交换机在多条等价路径里，**根据实时负载**（出口队列深度、端口利用率）为每个包（或每一段）选出口，把流量往空闲的链路上引。

**带来的新问题：乱序**。同一条流的包走了不同路径，到达顺序会乱。RC 传统上要求按序到达，乱序的包会被当成丢包，触发重传（go-back-N），反而更慢。所以 AR 不是交换机单方面能做的事，**网卡必须配合**。

**交换机要做什么**

| 事项 | 说明 |
|---|---|
| **感知负载** | 观察本地各出口的队列深度或利用率，有的实现还会参考下游反馈 |
| **选出口** | 逐包选，或按 **flowlet**（流突发间隙）：同一条流里，两个包间隔足够长时才允许换路，间隔内的包保持同一路径，降低乱序概率 |
| **故障绕行** | 链路故障时快速把流量切走 |
| **与 ECMP 共存** | 能按优先级或流量类别选择是否启用 AR（有些流量不能乱序） |
| **ECN 和 PFC 照常工作** | AR 只改选路，拥塞时的标记和暂停仍然需要 |

**网卡要做什么**

| 事项 | 说明 |
|---|---|
| **接收端容忍乱序** | 乱序到达的包要能**直接写进内存的正确位置**，按序向上报告完成，**不把乱序当丢包**，不触发 NAK（Negative Acknowledgement，否定确认）和 go-back-N |
| **选择性重传** | 真的丢包时，只重传丢的那个，不从头重发 |
| **（可选）发送端打散路径** | 网卡给不同的 QP 或不同的包轮换 UDP 源端口，让交换机的 ECMP 哈希到不同路径，这样交换机不用支持 AR（上面 `MC_MLX5_QP_UDP_SPORTS` 就是手工版本） |
| **与拥塞控制协同** | 包走不同路径，往返时延不同，CNP 也可能来自不同路径，降速算法要能适应 |

**重要局限**：叶脊（leaf-spine）网络里，AR 主要发挥作用在**上行**，因为上行有多条路径可选；到了目的叶交换机的**最后一跳，只有一条路**。所以 AR 能缓解中间链路的不均衡，**解决不了最后一跳的 incast**（[§8](#8)）。（由拓扑结构**推断**。）

**与其他方案的关系**：**包喷洒**（packet spraying，逐包负载均衡）可看作 AR 的极端形式：每个包独立选路。NVIDIA 的 Spectrum-X 与 Ultra Ethernet 都采用这个思路（[§10](#10)）。第三方文章里"有效吞吐 95% 对 60%"的数字**没有出处，不采用**。AR 与 DCQCN 互补：AR 管**把负载摊匀**，DCQCN 管**总量太大时降速**，PFC 兜底。

**本集群**：交换机是否支持或启用了 AR，**未知**；网卡是否支持乱序容忍，**未验证**。

<a id="10"></a>
## 10. 其他拥塞控制算法

[↑ 返回目录](#toc)

| 方案 | 拥塞信号 | 要点 | 来源 |
|---|---|---|---|
| **DCQCN** | 交换机的 ECN 标记（一位） | 交换机只需 RED / ECN；网卡做速率控制；依赖 PFC | 论文，SIGCOMM 2015 |
| **TIMELY** | **RTT**（Round-Trip Time，往返时延，主机测得，不需要交换机反馈） | 谷歌提出，称为数据中心里第一个基于延迟的拥塞控制 | 论文，SIGCOMM 2015 |
| **HPCC**（High Precision Congestion Control，高精度拥塞控制） | **INT**（In-band Network Telemetry，带内网络遥测）：交换机在报文里写入链路负载等精确信息 | 能精确知道每一跳的负载；论文称与 DCQCN、TIMELY 相比，流完成时间最多缩短 95%，大规模 incast 下几乎不造成拥塞（**作者的结论**） | 论文，SIGCOMM 2019 |
| **Swift** | 端到端**延迟**（依赖网卡硬件时间戳） | 谷歌的方案，对固定延迟目标做 AIMD（Additive Increase Multiplicative Decrease，加性增乘性减）；把延迟拆成网络延迟与主机延迟；测试床上短 RPC（Remote Procedure Call，远程过程调用）尾延迟低于 50 µs | 论文，SIGCOMM 2020 |
| **Ultra Ethernet**（UEC，Ultra Ethernet Consortium，超以太网联盟） | 见下 | 不依赖无损网络；端点主导 | 规范 1.0（2025 年 6 月）；以第三方报道为准 |

**为什么有这么多方案**：一位 ECN 太粗，只知道"超过阈值"，不知道"超过多少"；ECN 方案参数很多，不同网络要重新调；PFC 的副作用（死锁、风暴、队头阻塞）促使人们想**脱离无损网络**。

| | DCQCN | TIMELY / Swift | HPCC |
|---|---|---|---|
| 信号来自 | 交换机（ECN） | 主机（RTT / 延迟） | 交换机（INT） |
| 对交换机的要求 | RED / ECN | 无 | 要支持 INT |
| 对网卡的要求 | 速率控制 + CNP | 精确的时间戳 | 处理 INT 信息 |
| 是否需要 PFC | 需要（线速起步） | 通常仍与无损网络配合 | 论文设想与 PFC 配合 |

（此表由论文摘要整理，**没有逐项核对原文**。）本集群在用哪种：**未知**，只能从主机侧看到 `rx_ecn_mark`、`rx_prio3_marked` 之类的计数器名字。

**Ultra Ethernet：脱离"无损网络"的方向（第三方报道）**：UEC 于 2025 年 6 月发布 1.0 规范（第三方报道的日期）。据报道的要点：**端点主导**的拥塞控制；**包喷洒**：同一条流的包分散到多条路径，由网卡重排；**包裁剪**（packet trimming）：交换机拥塞时丢掉载荷、只转发包头，让接收方知道丢了哪个包；**链路级重试**（link-level retry）。**没有读规范原文**。

<a id="11"></a>
## 11. 实施步骤

[↑ 返回目录](#toc)

约定（NVIDIA 常见做法，**厂商文档**）：

| 流量 | 常见 DSCP | 优先级 | PFC |
|---|---|---|---|
| RoCE 数据 | 26（`traffic_class = 104`） | 3 | 开 |
| CNP | 48 | 6 | 不开，严格优先级 |
| 其他 | 0 | 0 | 不开 |

核心思路：**让 RDMA 流量走一条固定的"专用车道"，在这条车道上同时开启两道防线。**

### 步骤 1：应用侧给流量打标记

| 程序 | 设置 |
|---|---|
| 本仓库的裸 verbs 测试 | 参数 `-t 104` |
| NCCL | 环境变量 `NCCL_IB_TC=104`，**每台机器、每个 rank 都要设** |
| 其他 | 在 QP 转到 RTR 时写 `ah_attr.grh.traffic_class = 104` |

不设的话，traffic_class 等于 0，走默认队列，享受不到后面的保护。

### 步骤 2：网卡侧（每台服务器）

NVIDIA 网卡常用 `mlnx_qos` 配置主机侧 QoS（Quality of Service，服务质量；需要 MLNX_OFED，Mellanox OpenFabrics Enterprise Distribution，Mellanox 开放网络企业发行版的工具，**本集群 Pod 里没有这个命令**）：

| 目的 | 命令示例 |
|---|---|
| 查看信任模式 | `mlnx_qos -i <网口>`，看 `Priority trust state` |
| 信任 DSCP | `mlnx_qos -i <网口> --trust dscp` |
| 把 DSCP 26 映射到优先级 3 | `mlnx_qos -i <网口> --dscp2prio set,26,3` |
| 只在优先级 3 开 PFC | `mlnx_qos -i <网口> --pfc 0,0,0,1,0,0,0,0`（八个 0/1，对应八个优先级） |
| ETS | `--tsa` 可选 `strict`、`ets`、`vendor` |

- 这些设置是**非持久的**，网卡或主机重启后失效；社区做法是用 systemd 单元或网络脚本在开机时重新应用（**第三方指南**）。
- 较新的 Linux 自带 `dcb`（iproute2）工具，可以 `dcb pfc show dev <网口>`、`dcb ets show`、`dcb app show`；是否可用取决于驱动与权限（本集群 Pod 里读不到）。
- DCQCN 的参数（CNP 的 DSCP、ECN 开关等）在 NVIDIA 驱动里通过 sysfs 或固件设置；**本篇没有查到可靠的原文，也没有在 Pod 里找到对应的 `ecn` 目录**，不展开。

### 步骤 3：交换机侧（每一台、每一跳）

NVIDIA Cumulus Linux 的 NVUE（NVIDIA User Experience，NVIDIA 的命令行配置接口）一条命令按推荐值配好：

```
nv set qos roce
nv config apply
```

它自动配置（**厂商文档**，Cumulus 5.6）：

| 项 | 默认值 |
|---|---|
| 分类 | 信任 DSCP；DSCP 24–31 → priority 3，DSCP 48–55 → priority 6 |
| 队列 | priority 3 → RoCE 队列，priority 6 → CNP 队列 |
| 调度 | CNP 严格优先级；RoCE 与其他流量各 50%（DWRR） |
| PFC | 只在 priority 3 开 |
| ECN | RoCE 队列上，最小阈值约 150 KB，最大约 1.5 MB |

其他品牌需要手工配，要点相同：

1. 信任 DSCP，全链路映射一致（错一跳，那一段就落到默认队列）；
2. **只**在 RDMA 优先级开 PFC，CNP 优先级不开 PFC、用严格优先级；
3. ECN 阈值要**低于** PFC 的 xoff（这样 ECN 先触发）；
4. 开 PFC 看门狗，广播 / 组播不进无损队列；
5. headroom 用厂商工具计算，不要手算；MTU 全链路一致，交换机用巨帧（RoCE MTU 4096 时常取 9216）。

### 步骤 4：多条流的负载均衡（可选）

大流少、哈希不均时，让不同 QP 用不同的 UDP 源端口，或在交换机上开自适应路由（[§9](#9)）。

<a id="12"></a>
## 12. 监控与排错

[↑ 返回目录](#toc)

### 12.1 看什么

| 位置 | 看什么 | 说明 |
|---|---|---|
| **交换机** | 各队列的丢包、PFC 暂停帧收发、ECN 标记、缓冲占用 | 最直接，我们没有访问权限 |
| **网卡：`ethtool -S <网口>`** | 暂停帧、按优先级的丢弃、ECN 标记 | 名字因驱动而异 |
| **网卡：`rdma statistic show link <设备>/<端口>`** | RDMA 层的重传、超时、缓冲不足等 | 见下 |
| **内核 mlx5 驱动的拥塞计数器** | `np_cnp_sent`（NP 发出的 CNP）、`rp_cnp_handled` / `rp_cnp_ignored`（RP 处理 / 忽略的 CNP）、`np_ecn_marked_roce_packets`（NP 收到的 CE 报文）；通常在 `hw_counters` 里 | 来自内核提交说明；**本集群 Pod 里没有看到** |

### 12.2 本集群 Pod 里实际有的计数器名（**实测**，server-0，2026-11-01）

**`ethtool -S fabric0`**（只列名字）：

| 类别 | 名字 |
|---|---|
| 按优先级（**0–7 各一套**） | `rx_prioN_bytes`、`rx_prioN_packets`、`tx_prioN_bytes`、`tx_prioN_packets`、`rx_prioN_discards`、`rx_prioN_buf_discard`、`rx_prioN_cong_discard`、`rx_prioN_marked`（N = 0…7；`buf_discard` 与 `cong_discard` 的区别没有查，**推断**分别对应缓冲不足与拥塞造成的丢弃，`marked` 对应被标记 ECN 的包） |
| ECN | `rx_ecn_mark`、`rx0_ecn_mark` … `rx10_ecn_mark`（按接收队列） |
| 暂停帧 | `rx_pause_ctrl_phy`、`tx_pause_ctrl_phy`（**只有这两个，没有按优先级的暂停计数**，也没有暂停时长） |
| 总丢弃 | `rx_discards_phy`、`tx_discards_phy` |
| 缓冲 | `rx_out_of_buffer`、`rx_oversize_pkts_buffer` |

有按优先级的字节 / 包计数，所以做 `-t 104` 与 `-t 0` 对照实验时，可以**直接看流量进了哪个优先级**（前后各取一次，看哪个 `rx_prioN_bytes` 在涨）；本集群里 RDMA 实际落在哪个优先级，本篇**没有测**。

**`rdma statistic show link mlx5_0/1`**：`rx_write_requests`、`rx_read_requests`、`rx_atomic_requests`、`rx_dct_connect`、`out_of_buffer`、`out_of_sequence`、`duplicate_request`、`rnr_nak_retry_err`、`packet_seq_err`、`implied_nak_seq_err`、`local_ack_timeout_err`、`resp_local_length_error`、`resp_cqe_error`、`req_cqe_error`、`req_remote_invalid_request`、`req_remote_access_errors`、`resp_remote_access_errors`、`resp_cqe_flush_error`、`req_cqe_flush_error`、`req_transport_retries_exceeded`、`req_rnr_retries_exceeded`。

这些是**累计值**：当时 `out_of_buffer`、`packet_seq_err`、`local_ack_timeout_err`、`req_transport_retries_exceeded`、`req_cqe_error` 是非零的（个位数到二位数）；来源没有查，也不能据此说明"现在有问题"——Pod 里先后做过很多实验，包括会触发重试耗尽的错误输入。**判断拥塞要看同一次测试前后的差值**，不要看绝对值。

### 12.3 排错的基本思路

**健康的样子**：有 ECN 标记，PFC 暂停很少，没有丢弃。

1. 跑一个已知流量的测试，**前后各取一次计数**，看差值；
2. 确认 RDMA 流量进入了预期的队列（`rx_prioN_bytes` / `tx_prioN_bytes` 在涨）；
3. 拥塞时先看 **ECN 标记是否出现**，再看 **PFC 暂停**——ECN 先于 PFC 才是预期；
4. 看有没有意外的丢弃和重传（`rx_prio*_discards`、`out_of_sequence`、`packet_seq_err`、`local_ack_timeout_err`）；
5. 暂停帧计数一直在涨且吞吐很低，要怀疑**暂停风暴**或死锁（[§5.3](#5)）。微软的经验是：**暂停时长比暂停帧个数更能反映拥塞的严重程度**；本集群 Pod 里的 `ethtool -S` 只有个数；
6. 有 CE 标记但没有降速，要看 CNP 有没有产生、有没有走高优先级（[§4.4](#4)）。

| 现象 | 可能原因 |
|---|---|
| 完全没有 ECN 标记 | 流量没打标记（走了默认队列）；或交换机没配 ECN；或 ECN 位为 Not-ECT（[§3.2](#3)） |
| 暂停一直在涨，速度很慢 | ECN 阈值设得太高，PFC 先触发；或出现暂停风暴 / 死锁 |
| 有 CE 标记但不降速 | CNP 没产生，或没走高优先级 |

<a id="13"></a>
## 13. 与本集群的关系

[↑ 返回目录](#toc)

**已知（实测）**

- 本仓库的裸 verbs 程序用 `-t 104`（= DSCP 26），NCCL 用 `NCCL_IB_TC=104`，即**步骤 1（应用侧打标记）做了**。主机内存 READ 实验里，`-t 104` 下带宽稳定在约 43.8 GB/s，`-t 0` 下约 24.6 GB/s 且不稳定，WRITE 不受影响，**但没有定论是不是队列配置不同造成的**（[roce-swtich.md §7](roce-swtich.md#7)、[c-multi-node-cpu.md](c-multi-node-cpu.md)）。
- **pyverbs 的 CM（Connection Manager，连接管理器）版没有设置 `traffic_class`**（等于 0），MTE 的 `MC_IB_TC` 默认 -1（含义见官方文档）；这两条实验路径走的是默认类别，**不能代表** `-t 104` 的表现（[py-multi-node-cpu.md §5.6](py-multi-node-cpu.md#5)、[mte.md §1.4](mte.md#1-4)）。
- Pod 里 `ethtool -S fabric0` 和 `rdma statistic` 能读到的计数器见[§12.2](#12)。

**Pod 里读不到的（实测）**

- `dcb pfc show dev fabric0`、`dcb ets show`、`dcb app show`、`dcb buffer show`、`dcb maxrate show` 全部返回 `Attribute read: Operation not supported`——**主机侧的 PFC / ETS / DSCP 信任 / 缓冲配置读不到**（原因没有查：可能是驱动不提供给容器，也可能由宿主机管理）。
- 没有 `mlnx_qos`、`mlxconfig`、`mlxreg`、`lldptool`；`mlx5_0` 的 `hw_counters` 目录不存在；`/sys/class/net/fabric0/ecn` 不存在。所以 **CNP 计数器和 DCQCN 参数看不到**。

**未知**

- 交换机上 DSCP 26 / DSCP 0 各映射到哪个队列，有没有 PFC、ECN、整形，是否启用自适应路由；
- 网卡是否信任 DSCP（而不是 PCP）、PFC 是否开在优先级 3、DCQCN 参数是多少、ECN 位怎么设置；
- 实验时 fabric 的真实负载。

**目前能做的**：步骤 1，加[§12](#12)的主机侧计数器验证；步骤 2、3 需要宿主机和交换机的权限。

<a id="14"></a>
## 14. 术语表

[↑ 返回目录](#toc)

**缩写（按字母序）**

| 缩写 | 英文全称 | 中文 |
|---|---|---|
| AF31 | Assured Forwarding 31 | 确保转发 31（DSCP 26 的名称） |
| AI | Artificial Intelligence | 人工智能 |
| AIMD | Additive Increase Multiplicative Decrease | 加性增乘性减 |
| AR | Adaptive Routing | 自适应路由 |
| ARP | Address Resolution Protocol | 地址解析协议 |
| BTH | Base Transport Header | 基本传输头（RDMA 报文头） |
| CE | Congestion Experienced | 经历拥塞（ECN 的"堵车戳"） |
| CM | Connection Manager | 连接管理器 |
| CNP | Congestion Notification Packet | 拥塞通知包 |
| CP | Congestion Point | 拥塞点（DCQCN 里的交换机） |
| CPU | Central Processing Unit | 中央处理器 |
| DCB | Data Center Bridging | 数据中心桥接 |
| DCBX | Data Center Bridging Exchange | 数据中心桥接交换协议 |
| DCQCN | Data Center Quantized Congestion Notification | 数据中心量化拥塞通知 |
| DCTCP | Data Center TCP | 数据中心 TCP |
| DSCP | Differentiated Services Code Point | 差分服务代码点 |
| DWRR | Deficit Weighted Round Robin | 差额加权轮询 |
| ECMP | Equal-Cost Multi-Path | 等价多路径 |
| ECN | Explicit Congestion Notification | 显式拥塞通知 |
| ECT | ECN-Capable Transport | 支持 ECN 的传输 |
| EP | Expert Parallelism | 专家并行 |
| ETS | Enhanced Transmission Selection | 增强传输选择 |
| GPU | Graphics Processing Unit | 图形处理器 |
| HOL | Head-of-Line blocking | 队头阻塞 |
| HPCC | High Precision Congestion Control | 高精度拥塞控制 |
| IEEE | Institute of Electrical and Electronics Engineers | 电气电子工程师学会 |
| INT | In-band Network Telemetry | 带内网络遥测 |
| IP | Internet Protocol | 网际协议 |
| KV cache | Key-Value cache | 键值缓存（大模型推理中保存的注意力中间结果） |
| LLDP | Link Layer Discovery Protocol | 链路层发现协议 |
| MAC | Media Access Control | 介质访问控制 |
| MLNX_OFED | Mellanox OpenFabrics Enterprise Distribution | Mellanox 开放网络企业发行版（驱动与工具包） |
| MoE | Mixture of Experts | 混合专家模型 |
| MTE | Mooncake Transfer Engine | Mooncake 传输引擎 |
| MTT | Memory Translation Table | 内存地址翻译表 |
| MTU | Maximum Transmission Unit | 最大传输单元 |
| NAK | Negative Acknowledgement | 否定确认 |
| NCCL | NVIDIA Collective Communications Library | NVIDIA 集合通信库 |
| NP | Notification Point | 通知点（DCQCN 里的接收端网卡） |
| NVUE | NVIDIA User Experience | NVIDIA 的命令行配置接口 |
| PCP | Priority Code Point | 优先级代码点 |
| PD | Prefill-Decode disaggregation | 预填充-解码分离 |
| PFC | Priority-based Flow Control | 基于优先级的流量控制 |
| PG | Priority Group | 优先级组 |
| PXE | Preboot Execution Environment | 预启动执行环境 |
| QCN | Quantized Congestion Notification | 量化拥塞通知（IEEE 802.1Qau） |
| QoS | Quality of Service | 服务质量 |
| QP | Queue Pair | 队列对（RDMA 的一条连接） |
| RC | Reliable Connection | 可靠连接 |
| RDMA | Remote Direct Memory Access | 远程直接内存访问 |
| RED / WRED | Random Early Detection / Weighted RED | 随机早期检测 / 加权随机早期检测 |
| RFC | Request for Comments | 请求评论文档 |
| RoCE | RDMA over Converged Ethernet | 融合以太网上的 RDMA |
| RP | Reaction Point | 反应点（DCQCN 里的发送端网卡） |
| RPC | Remote Procedure Call | 远程过程调用 |
| RTR | Ready To Receive | 准备接收（QP 的一个状态） |
| RTT | Round-Trip Time | 往返时延 |
| SIGCOMM | ACM Special Interest Group on Data Communication | ACM 数据通信特别兴趣组（网络领域顶级年会） |
| SP | Strict Priority | 严格优先级 |
| TCP | Transmission Control Protocol | 传输控制协议 |
| TLV | Type-Length-Value | 类型-长度-值 |
| ToR | Top of Rack | 机架顶部（交换机） |
| ToS | Type of Service | 服务类型（IPv4 头里的字节） |
| TTFT | Time To First Token | 首词元时延 |
| UDP | User Datagram Protocol | 用户数据报协议 |
| UEC / UET | Ultra Ethernet Consortium / Transport | 超以太网联盟 / 传输 |
| VLAN | Virtual Local Area Network | 虚拟局域网 |
| WQE | Work Queue Element | 工作队列元素 |
| WRR | Weighted Round Robin | 加权轮询 |

**概念**

| 术语 | 含义 |
|---|---|
| **Traffic Class（流量类别）** | 有两个含义，**不要混淆**：IP 头里的一个字节 = DSCP（高 6 位）+ ECN（低 2 位），verbs 里的 `traffic_class` 是它；交换机里的 traffic class 指**队列** |
| **switch priority** | 交换机内部的优先级（0–7），PFC 按它区分 |
| **trust mode（信任模式）** | 网卡 / 交换机以哪个字段（PCP 或 DSCP）做分类依据 |
| **lossless / lossy** | 无损（开 PFC，不因拥塞丢包）/ 有损（不开 PFC） |
| **headroom** | 发出 PAUSE 后到上游真正停下之前还会到达的数据，必须预留的缓冲 |
| **xoff / xon** | PFC 的两个阈值：达到 xoff 发暂停，降回 xon 恢复 |
| **Kmin / Kmax / Pmax** | RED 的三个参数：低阈值、高阈值、最大标记概率 |
| **victim flow（受害流）** | 与拥塞无关、却因暂停传播而变慢的流 |
| **PFC deadlock / storm（死锁 / 风暴）** | 暂停帧在环形依赖的缓冲之间传播，网络停转 / 某设备持续发暂停帧，暂停扩散到整个网络 |
| **watchdog（看门狗）** | 检测到长时间暂停 / 暂停帧持续发出时，自动关闭无损模式或停发暂停帧的保护机制 |
| **slow receiver（慢接收者）** | 网卡内部（如 MTT 缓存缺失）使接收变慢，从而发出 PFC |
| **incast / fan-out** | 多打一 / 一打多 |
| **go-back-0 / go-back-N / 选择性重传** | 丢包后从消息开头重发 / 从丢失的包开始重发 / 只重发丢失的包 |
| **livelock（活锁）** | 链路满速，但应用没有进展 |
| **哈希极化** | 多条流被哈希到同一条链路 |
| **flowlet** | 同一条流里被足够长间隔隔开的一段包，可以安全地换路 |
| **包喷洒 / 包裁剪** | 同一条流的包分散到多条路径 / 交换机拥塞时丢掉载荷、只转发包头 |
| **α / g** | DCQCN 里的拥塞程度估计 / 它的更新增益 |
| **β** | DCQCN 论文里让 PFC 阈值随空闲共享缓冲动态变化的参数 |

<a id="15"></a>
## 15. 参考资料

[↑ 返回目录](#toc)

**论文 / 标准**

- Zhu 等，*Congestion Control for Large-Scale RDMA Deployments*（DCQCN），SIGCOMM 2015：[微软研究院页面](https://www.microsoft.com/en-us/research/publication/congestion-control-for-large-scale-rdma-deployments/)、[PDF（杜克大学课程）](https://courses.cs.duke.edu/fall19/compsci514/papers/dcqcn.pdf)。本篇的 DCQCN 算法、参数与缓冲计算来自这篇。
- Guo 等，*RDMA over Commodity Ethernet at Scale*，SIGCOMM 2016：[PDF（伊利诺伊大学课程）](https://courses.grainger.illinois.edu/cs598hpn/fa2019/papers/microsoftrdma.pdf)。本篇的基于 DSCP 的 PFC、活锁、死锁、暂停风暴、看门狗来自这篇。
- Li 等，*HPCC: High Precision Congestion Control*，SIGCOMM 2019：[MIT DSpace](https://dspace.mit.edu/handle/1721.1/137430)。
- Kumar 等，*Swift: Delay is Simple and Effective for Congestion Control in the Datacenter*，SIGCOMM 2020：[谷歌研究院](https://research.google/pubs/swift-delay-is-simple-and-effective-for-congestion-control-in-the-datacenter/)。
- Mittal 等，*TIMELY: RTT-based Congestion Control for the Datacenter*，SIGCOMM 2015（只引用了摘要性的说法，没有读原文）。
- IEEE 802.1Qbb（PFC）、802.1Qaz（ETS 与 DCBX）、802.1AB（LLDP）；入门介绍：[Cisco 的 DCB 简介](https://www.cisco.com/c/dam/en/us/solutions/collateral/data-center-virtualization/ieee-802-1-data-center-bridging/at_a_glance_c45-460907.pdf)、[ipSpace 对 802.1Qaz 的介绍](https://blog.ipspace.net/2010/09/introduction-to-8021qaz-enhanced/)。
- RFC 3168（The Addition of Explicit Congestion Notification to IP）。

**厂商文档**

- NVIDIA Cumulus Linux 5.6，[RDMA over Converged Ethernet - RoCE](https://docs.nvidia.com/networking-ethernet-software/cumulus-linux-56/Layer-1-and-Switch-Ports/Quality-of-Service/RDMA-over-Converged-Ethernet-RoCE/)：默认的 RoCE 无损 / 有损配置、DSCP 到优先级的映射、ETS、ECN 阈值示例。
- NVIDIA MLNX_OFED 文档：[Flow Control](https://docs.nvidia.com/networking/display/NVIDIAMLNXOFEDDocumentationv24101140105LTS/Flow+Control)、[Quality of Service](https://docs.nvidia.com/networking/display/mlnxofedv494080/quality+of+service+(qos))（页面需要浏览器渲染，本篇通过搜索摘要引用 `mlnx_qos` 的用法，没有读到页面原文）。
- NVIDIA 论坛的相关讨论：[Enabling PFC disables DCQCN](https://forums.developer.nvidia.com/t/pfc-cc-doesnt-work-enabling-pfc-disables-dcqcn/292815)（用户报告，不是已验证的结论）。
- Linux 内核提交：[IB/mlx5: Support congestion related counters](https://github.com/xanmod/linux/commit/e1f24a79f424ddb03828de7c0152668c9a30146e)：定义 `np_cnp_sent`、`rp_cnp_handled` 等计数器。

**第三方 / 新闻**

- Ultra Ethernet：[Network World 的报道](https://www.networkworld.com/article/4006285/ultra-ethernet-consortium-publishes-1-0-specification-readies-ethernet-for-hpc-ai.html)、[Glenn K. Lockwood 的笔记](https://glennklockwood.com/garden/Ultra-Ethernet)（没有读规范原文）。
- Spectrum-X 与 RoCE 的对比文章：[NADDOD](https://www.naddod.com/ai-insights/from-roce-to-spectrum-x-the-evolution-of-ethernet-networking-for-ai-data-centers)（厂商博客，其中的性能数字没有出处，本篇不采用）。

**仓库里的相关文章**：[rdma.md §9](rdma.md#9)、[roce-swtich.md](roce-swtich.md)、[understanding_RoCE.MD](understanding_RoCE.MD)、[c-multi-node-cpu.md](c-multi-node-cpu.md)。

<a id="A"></a>
## 附录 A. 论文里的数值与公式

[↑ 返回目录](#toc)

以下数值来自微软 / Mellanox 在 40 Gbps 数据中心的论文，**只作量级参考，不能照搬**。

### A.1 DCQCN 的算法细节（**论文**）

**NP（接收端网卡）**：对每条流，收到一个 CE 报文就**立即**发一个 CNP；之后至多每 **N = 50 µs** 发一个。没有拥塞时不发任何 CNP。CNP 以**高优先级**发送。

**RP（发送端网卡）**：设当前速率 R<sub>C</sub>、目标速率 R<sub>T</sub>、拥塞程度估计 α（初值 1）、增益 g。

- **收到 CNP（降速）**：R<sub>T</sub> = R<sub>C</sub>；R<sub>C</sub> = R<sub>C</sub> × (1 − α/2)；α = (1 − g)·α + g。
- **K 微秒内没收到 CNP**：α = (1 − g)·α。论文用 K = 55 µs，且必须大于 CNP 发送间隔。
- **增速**（由一个定时器和一个字节计数器驱动，与 QCN 相同）：
  1. **快速恢复**（fast recovery）：连续 F = 5 次，R<sub>C</sub> = (R<sub>T</sub> + R<sub>C</sub>)/2；
  2. **加性增**（additive increase）：R<sub>T</sub> 加固定小量 R<sub>AI</sub>（论文取 40 Mbps），R<sub>C</sub> = (R<sub>T</sub> + R<sub>C</sub>)/2；
  3. **超级增**（hyper increase）：快速提速。
- 论文参数：定时器 55 µs、字节计数器 10 MB、Kmax 200 KB、Kmin 5 KB、Pmax 1%、g = 1/256。

### A.2 缓冲的计算示例（**论文**）

以 40 Gbps、12 MB 共享缓冲、32 端口、8 个 PFC 优先级、MTU 1500 的 Trident II 交换机为例：

- 每端口每优先级的 headroom t_flight ≈ **22.4 KB**；
- PFC 阈值上界 t_PFC ≤ (B − 8·n·t_flight) / (8·n) ≈ 24.47 KB（B 为总缓冲，n 为端口数）；
- **要保证 ECN 先于 PFC 触发**，需要 t_PFC > n · t_ECN。静态上界算出来 t_ECN < 0.85 KB（小于一个 MTU，不可行）；
- 解决办法：让 t_PFC **随空闲共享缓冲动态变化**：t_PFC = β·(B − 8·n·t_flight − s)/8（s 为已占用缓冲），取 β = 8，得到 t_ECN < 21.75 KB。

### A.3 PFC 看门狗的细节（**论文**）

- **网卡侧**：网卡上的微控制器检测到接收流水线停止一段时间（默认 100 ms）且仍在发暂停帧，就关掉发暂停帧；**不会**自动恢复（网卡已经坏了，等服务器修复）。
- **交换机侧**：服务器侧端口的出队列排不出去、同时持续收到该网卡的暂停帧，就关闭该端口的无损模式并丢弃往返的无损包；暂停帧消失一段时间（默认 200 ms）后自动重新开启。
- 两者互补，论文都实现了，并建议**都**实现。

<a id="B"></a>
## 附录 B. 状态汇总

[↑ 返回目录](#toc)

| 结论 | 状态 |
|---|---|
| go-back-0 在 0.4% 丢包下活锁；改为 go-back-N 后消失 | **论文**（微软 2016） |
| 基于 DSCP 的 PFC 的动机与做法；PFC 死锁、暂停风暴的成因与看门狗对策 | **论文**（微软 2016） |
| DCQCN 的 CP / NP / RP 算法、公式、参数、缓冲计算 | **论文**（DCQCN，SIGCOMM 2015） |
| DCQCN 不能取代 PFC（线速起步）；交换机只需支持 RED / ECN | **论文** |
| ECN 编码；PFC 按优先级暂停；ETS 按权重分配带宽；DCBX 基于 LLDP | **标准** |
| CNP 是 RoCE v2 标准里定义的报文，装在 UDP 里（目的端口 4791） | **标准** / 推断 |
| CNP 的操作码 `0x81`、暂停帧的固定目的地址与类型值 | **凭记忆，未核对** |
| QCN 的 6 位量化反馈、只能在二层工作 | **凭记忆，未核对** |
| 降速由网卡硬件 / 固件完成，不占 CPU | **推断**（由 DCQCN 设计得出） |
| 发送网卡本地排队无损（数据留在主机内存），拥塞发生在交换机出口与接收端 | **推断**（由 RDMA 工作方式得出，未验证） |
| `-t 104` 的 ECN 位为 0（Not-ECT），网卡是否会改成 ECT 未验证；若不改，交换机可能不打 ECN 标记 | **推断**，未验证 |
| PCP 是通用二层优先级字段，PFC 最早按它分类；RoCE v2 里 PCP 跨子网不保留，主流用 DSCP | **论文**（微软 2016）加推断 |
| 若网卡信任 PCP 且无 VLAN 标签，`-t 104` 的 DSCP 标记不生效 | **推断**，未验证（需看宿主机 `mlnx_qos` 的信任模式） |
| MoE all-to-all、gather、参数服务器、分布式存储并行读、checkpoint 保存等也属于 incast 场景 | **推断**（未在本集群测过） |
| PD 分离里 incast 的拥塞点、READ 比 WRITE 更易控制并发 | **推断**（未在本集群测过） |
| 自适应路由需要网卡容忍乱序并选择性重传；AR 解决不了最后一跳的 incast；flowlet 细节与各厂商实现 | **推断 / 凭记忆，未核对厂商文档** |
| HPCC 用 INT、"流完成时间最多缩短 95%" | **论文摘要**（作者自己的结论，没读原文） |
| "DCQCN 有 15 个参数" | **第三方摘要**，没有核对原文 |
| TIMELY 用 RTT、Swift 用端到端延迟与网卡时间戳 | **论文摘要**（没读原文） |
| Cumulus 的默认 RoCE 配置（priority 3 / 6、ETS、ECN 阈值约 150 KB / 1.5 MB） | **厂商文档**（Cumulus 5.6） |
| `mlnx_qos` 的用法 | **厂商文档 / 第三方指南**（通过搜索摘要，没读到页面原文） |
| UEC 1.0 的要点（端点主导、包喷洒、包裁剪、链路级重试） | **第三方报道**（没读规范原文） |
| 本集群 Pod 里 `ethtool -S fabric0` 有按优先级（0–7）的字节 / 包 / 丢弃 / 标记计数，暂停计数只有 `rx_pause_ctrl_phy` / `tx_pause_ctrl_phy`；`rdma statistic show link mlx5_0/1` 的计数器名字 | **实测** |
| 本集群 Pod 里 `dcb … show` 全部 `Operation not supported`；没有 `mlnx_qos`、`hw_counters`、`ecn` 目录 | **实测** |
| 这些计数器当前的非零值的来源 | **未查** |
| 本集群交换机的 QoS 配置、网卡的 trust / PFC / DCQCN 参数、是否启用自适应路由 | **未知** |
| 本集群里 `-t 104` 与 `-t 0` 的 READ 差异是否由队列配置造成 | **假设**，未证实（见 [roce-swtich.md §7](roce-swtich.md#7)） |

---

*本篇为通用说明，参考资料检索于 2026-11-01；不含本集群交换机的实际配置。*
