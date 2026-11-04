# RDMA / RoCE Lab Notes

**English** | [中文](README-CN.md)

Hands-on notes on **RDMA (Remote Direct Memory Access) over RoCE v2 (RDMA over Converged Ethernet version 2)**, measured on GPU nodes of DOKS (DigitalOcean Kubernetes Service). The material runs from the basics, through bare C and Python programs and MTE (Mooncake Transfer Engine), to QoS (Quality of Service) and congestion control on the switches.

> **Language note**: the in-depth documents are written in **Chinese**; this README is the English entry point. Source code, commands, counter names and numbers are language-independent. Every conclusion in the documents carries a reliability tag (**measured**, **inferred**, **unverified**, ...); please pay attention to them.

## Where to start

| If you want to... | Read |
|---|---|
| Understand what RDMA is and why one NIC (network interface card) shows up as two interfaces | [rdma.md](rdma.md) |
| Understand how a RoCE v2 network does congestion control (PFC, ECN, DCQCN) | [qos.md](qos.md), which opens with a "three-minute overview" |
| See what the switch side must be configured with, and how to check it | [roce-swtich.md](roce-swtich.md) (the file name has a typo, kept as is) |
| See measurements of two machines reading and writing host memory over RDMA | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| Same, for GPU memory | [c-multi-node.md](c-multi-node.md) (read the previous one first) |
| Do the same thing in Python | [py-multi-node-cpu.md](py-multi-node-cpu.md) |
| Use MTE for data transfer | [mte.md](mte.md) |
| Single-node GPU-to-GPU over NVLink / P2P | [c-single.md](c-single.md) |
| Emulate RoCE in software, no RDMA NIC needed | [soft-roce.MD](soft-roce.MD), [soft-roce-local.MD](soft-roce-local.MD) |

Suggested reading order: `rdma.md` → `c-multi-node-cpu.md` → `c-multi-node.md` → `qos.md`.

## Document index

**Concepts and networking**

| Document | Contents |
|---|---|
| [rdma.md](rdma.md) | RDMA fundamentals: objects, how one operation flows, why memory must be registered, transport types, GPUDirect RDMA |
| [qos.md](qos.md) | Congestion control and QoS for RoCE v2: two lines of defense (ECN plus DCQCN slow the sender early, PFC pauses as a last resort), NIC vs. switch responsibilities, incast, adaptive routing |
| [roce-swtich.md](roce-swtich.md) | Switch-side QoS checklist and verification, including an 11-item list to confirm with the network administrator |
| [understanding_RoCE.MD](understanding_RoCE.MD) | Introduction to RoCE networking on DOKS workers (in English) |
| [soft-roce.MD](soft-roce.MD), [soft-roce-local.MD](soft-roce-local.MD) | Setting up Soft-RoCE (RXE) for learning and testing |

**Experiments**

| Document | Contents | Code |
|---|---|---|
| [c-single.md](c-single.md) | Single node: NVLink and P2P (peer-to-peer), kernels reading and writing a peer GPU's memory directly, CUDA IPC | `p2p/0_to_7.cu`, `remote_access.cu`, `ipc_tcp.cu` |
| [c-multi-node-cpu.md](c-multi-node-cpu.md) | Two nodes, host memory: RDMA WRITE / READ, bandwidth, the effect of `-t 104` | `p2p/server-cpu.c`, `client-cpu.c` |
| [c-multi-node.md](c-multi-node.md) | Two nodes, GPU memory: GPUDirect RDMA, the `nvidia_peermem` and dmabuf implementations | `p2p/server-gpu.cu`, `client-gpu.cu` |
| [py-multi-node-cpu.md](py-multi-node-cpu.md) | pyverbs: an OOB (out-of-band) version and an `rdma_cm` version | `pyverbs-example/` |
| [mte.md](mte.md) | MTE: installation, dependencies, CPU-memory and cross-node GPU-memory transfers | `mte/` |

**Toolchain**

| Document | Contents |
|---|---|
| [c-build.md](c-build.md) | How a `.cu` file becomes an executable that depends only on the driver |
| [cuda-toolchain.md](cuda-toolchain.md) | PTX, cubin, the two JIT (just-in-time compilation) layers and caches |
| [debug.md](debug.md) | gdb, cuda-gdb and VS Code, debugging inside a Pod |

## Environment

- **Nodes**: DOKS workers, each with 8 NVIDIA B300 GPUs and 8 RoCE v2 NICs (`fabric0`, `fabric2`, ... in Linux, matching RDMA devices `mlx5_0`, `mlx5_2`, ...). GPUs inside a node are fully connected through NVSwitch.
- **Pods**: [`n-server2.yaml`](n-server2.yaml) deploys a two-replica StatefulSet, giving `server-0` and `server-1`. Each requests 8 GPUs and RDMA resources on every fabric NIC. The image is `vllm/vllm-openai:cu130-nightly` (CUDA 13.0).
- **The yaml files**: [`n-server.yaml`](n-server.yaml) has two replicas and no debug tools. [`n-server1.yaml`](n-server1.yaml) has one replica, adds gdb and cuda-gdb plus the `SYS_PTRACE` capability, and is used for single-node debugging ([debug.md](debug.md)). [`n-server2.yaml`](n-server2.yaml) differs from `n-server1.yaml` only in the replica count (two); the multi-node experiments use it.
- **Entry point**: every `kubectl` command runs on the jump host `nuc` (`ssh nuc`).
- **Network**: each fabric NIC sits in its own VRF (Virtual Routing and Forwarding). The two ends exchange connection details over TCP on `eth0` (OOB, out-of-band); data moves by RDMA over RoCE v2.

## Key results at a glance

All **measured** on this cluster. Conditions and limitations are in each document's summary and appendix.

| Question | Result | Source |
|---|---|---|
| Steady-state RDMA bandwidth, host memory | About **44 GB/s** (88% of the NIC's nominal 400 Gb/s); single QP (Queue Pair), single 64 MiB message | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| RDMA bandwidth, GPU memory | WRITE about **29.8 GB/s**, READ about **30.0 GB/s**, only 60% of nominal; not the network limit, since host memory reaches 44 GB/s on the same NIC pair | [c-multi-node.md](c-multi-node.md) |
| Single-node NVLink | 779.4 GB/s, about 26x RDMA (different measurement methods, compare orders of magnitude only) | [c-single.md](c-single.md) |
| Is the remote CPU involved? | No: while being read or written, the server CPU is at about 1% and the client at about 98% (busy polling) | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| Two GPU RDMA implementations | `nvidia_peermem` and dmabuf reach the **same bandwidth**; registration cost differs (128 MiB: dmabuf about 0.23 ms, peermem about 2.1 ms) | [c-multi-node.md](c-multi-node.md) |
| Are short tests reliable? | **No**: WRITE starts near 22 GB/s and ramps up; `-n 100` shows 22.1, `-n 6000` averages 43.3 | [c-multi-node-cpu.md](c-multi-node-cpu.md) |
| Installing MTE | The official `pip install mooncake-transfer-engine` does not work here (CUDA 12 build); install `mooncake-transfer-engine-cuda13` | [mte.md](mte.md) |
| pyverbs | The apt package ships only a Python 3.10 binary while PyTorch lives in 3.12, so they cannot share a process | [py-multi-node-cpu.md](py-multi-node-cpu.md) |

<a id="tc"></a>
### An important gotcha: always set `-t 104` (traffic class)

**On this cluster, NCCL (NVIDIA Collective Communications Library) must run with `NCCL_IB_TC=104`. The equivalent switch in this repo's bare verbs programs is `-t 104`, and it must be set on both ends.** 104 is DSCP (Differentiated Services Code Point) 26 times 4, with the ECN (Explicit Congestion Notification) bits at 0; see [qos.md §3.2](qos.md#3).

On the same NIC pair, RDMA to host memory:

| | READ bandwidth | First READ |
|---|---|---|
| `-t 0` (default) | About 24.6 GB/s, dropping to 17 GB/s | 12-103 ms |
| `-t 104` | 43.8 GB/s, constant | 1.55 ms |

- WRITE is unaffected; the GPU-memory version is also insensitive within the measured range (`-t 0` and `-t 104` give the same numbers).
- **The cause has not been identified.** "`-t 104` lands in a queue tuned for RDMA" is only a hypothesis, and some evidence argues against it (the NIC counters for congestion, retransmission and drops did not increase). Settling it needs the switch configuration; see the checklist in [roce-swtich.md §6.1](roce-swtich.md#6).
- The pyverbs `rdma_cm` version does not set `traffic_class` (it stays 0) and MTE's `MC_IB_TC` defaults to -1, so these two paths **do not represent** the behavior of `-t 104`.

## Quick start: reproduce the host-memory experiment

This is the shortest path; the full steps, options and pitfalls are in [c-multi-node-cpu.md §5 and §6](c-multi-node-cpu.md).

```bash
# 1. On nuc, deploy two Pods (server-0 and server-1)
kubectl apply -f n-server2.yaml

# 2. Copy the code into both Pods and compile there (plain C, only libibverbs)
gcc -O2 -Wall -Wextra p2p/server-cpu.c -o server-cpu -libverbs
gcc -O2 -Wall -Wextra p2p/client-cpu.c -o client-cpu -libverbs

# 3. On server-1, start the passive side first (-t 104 on both ends)
./server-cpu -t 104

# 4. On server-0, run the active side; -a is server-1's eth0 IP
./client-cpu -a <eth0 IP of server-1> -t 104

# Steady-state bandwidth: use a large iteration count
./client-cpu -a <IP> -t 104 -n 6000 -w 8
```

Common pitfalls: start the server first; `-s` must match on both ends; only even-numbered NICs work; short runs underestimate WRITE. The rest are in the cheat sheet of c-multi-node-cpu.md.

## Repository layout

```
.
├── README.md  README-CN.md
├── rdma.md  qos.md  roce-swtich.md  understanding_RoCE.MD   concepts and networking
├── c-single.md  c-multi-node-cpu.md  c-multi-node.md        C / CUDA experiments
├── py-multi-node-cpu.md  mte.md                             Python / MTE experiments
├── c-build.md  cuda-toolchain.md  debug.md                  toolchain and debugging
├── soft-roce.MD  soft-roce-local.MD                         software-emulated RoCE
├── n-server.yaml  n-server1.yaml  n-server2.yaml            Pod deployments
├── p2p/               C / CUDA sources: single-node P2P, multi-node CPU and GPU versions, registration benchmark
├── pyverbs-example/   pyverbs scripts: OOB version and CM (Connection Manager) version
└── mte/               MTE Python scripts: CPU and GPU versions
```

## Conventions

- **Reliability tags**: **measured** (observed on this cluster), **inferred** (derived from a mechanism or an observation), **documented** (from official documentation, not verified here), **unverified** / **untested** (no evidence). Each document's appendix has a per-claim status table. In the Chinese originals these appear as 实测, 推断, 文档, 未验证 / 未测.
- **Abbreviations**: `qos.md` and `roce-swtich.md` give the English full name and the Chinese for every abbreviation at first use, with a glossary. Older documents may not follow this consistently.
- The "measured" data comes from runs in the environment above between October and November 2026. **Other NIC models, switches and network sizes will give different results.**
- **There is no access to the switches' actual configuration**, so everything about switches is general practice, not this cluster's real setup.

## License

MIT, see [LICENSE](LICENSE).
