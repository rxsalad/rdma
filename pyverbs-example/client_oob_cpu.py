#!/usr/bin/env python3
"""
client_oob_cpu.py —— 主动方：经 RDMA 直接读写对端（server）的 CPU 内存。

【角色】
  client 是"发起操作"的一方。它对 server 的内存做一次 RDMA WRITE 和一次 RDMA READ。
  这两个都是"单边操作"：数据由两端网卡直接搬运，server 的 CPU 不参与。

【OOB 是什么】
  OOB = Out-Of-Band（带外）。单边操作要求发起方事先知道对端的 QP 号、PSN、GID，
  以及对端内存的地址(addr)和访问凭证(rkey)。这些信息先经 TCP 交换（只有几十字节），
  之后数据走 RDMA，不再经过 TCP。

【整体流程】
  ① 打开 RDMA 设备 → 建 PD / CQ / MR / QP（QP 此时处于 INIT 状态）
  ② 经 TCP 连接 server，交换连接信息，拿到 server 内存的 addr 和 rkey
  ③ 用对端的信息把 QP 依次切到 RTR → RTS，连接就绪
  ④ RDMA WRITE：把本端数据写进 server 内存的前半；经 TCP 发 'W' 通知 server 去看
  ⑤ RDMA READ ：把 server 内存后半的预置数据读回本端，打印
  ⑥ 经 TCP 发 'Q' 通知结束，清理退出

【内存布局】（本端一块 4096 字节）
  [   0, 2048)  要写给对端的数据（RDMA WRITE 的来源）
  [2048, 4096)  READ 回来的落点

【运行】Pod 里 pyverbs 装在系统 Python 3.10 下，所以用 python3.10：
  python3.10 -u client_oob_cpu.py --server <server 的 eth0 IP> --dev mlx5_0 --gid-index 3
"""

import argparse
import ctypes          # 用来直接读写 MR 的内存（mr.buf 只是一个整数地址）
import json
import random
import socket
import struct
import time

from pyverbs.addr import GID, GlobalRoute, AHAttr          # 地址相关：GID、全局路由、地址向量
from pyverbs.cq import CQ                                  # 完成队列
from pyverbs.device import Context                         # 打开 RDMA 设备
from pyverbs.enums import (ibv_access_flags, ibv_qp_state, ibv_qp_type,
                           ibv_send_flags, ibv_wr_opcode)  # 枚举：权限、QP 状态/类型、发送标志、操作码
from pyverbs.mr import MR                                  # 内存区域（注册内存）
from pyverbs.pd import PD                                  # 保护域
from pyverbs.qp import QP, QPAttr, QPCap, QPInitAttr      # 队列对及其属性
from pyverbs.wr import SGE, SendWR                         # SGE：一段内存；SendWR：一个发送工作请求

BUF_SIZE = 4096            # 注册的内存总大小
HALF = BUF_SIZE // 2       # 前半是 WRITE 的来源，后半是 READ 的落点
CLIENT_MSG = b"Hello from client, written by RDMA WRITE"   # 要写给 server 的数据


# ============================================================
# OOB：在 TCP 上收发 JSON（只用来交换控制信息）
# ============================================================
def recv_exact(sock, n):
    """从 TCP 连接里**精确**读 n 个字节。
    TCP 是字节流：一次 recv 不保证读够，所以要循环到凑够 n 字节为止。"""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:                       # 读到空 = 对端关闭了连接
            raise RuntimeError("OOB 连接被对端关闭")
        data += chunk
    return data


def send_json(sock, obj):
    """发一条消息：先发 4 字节长度（网络字节序），再发 JSON 正文。
    先发长度是为了让对端知道这条消息有多长（TCP 没有消息边界）。"""
    payload = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_json(sock):
    """收一条消息：先读 4 字节长度，再按长度读 JSON 正文。"""
    (n,) = struct.unpack("!I", recv_exact(sock, 4))
    return json.loads(recv_exact(sock, n))


# ============================================================
# 发起一次单边操作（WRITE 或 READ），并等它完成
# ============================================================
def rdma_op(qp, cq, opcode, local_addr, length, lkey, remote_addr, rkey):
    """向 QP 的发送队列提交一个 RDMA WRITE / READ，然后轮询 CQ 等完成。

    参数：
      opcode       IBV_WR_RDMA_WRITE 或 IBV_WR_RDMA_READ
      local_addr   本端内存的起始地址（WRITE 的来源 / READ 的落点）
      length       传输的字节数
      lkey         本端 MR 的本地凭证（本端用）
      remote_addr  对端内存的起始地址（经 OOB 拿到）
      rkey         对端 MR 的远端凭证（经 OOB 拿到）；网卡凭它校验权限
    """
    # SGE（scatter-gather element）描述一段本端内存：地址 + 长度 + lkey
    sge = SGE(local_addr, length, lkey)
    # SendWR 是一个"发送工作请求"。wr_id 只是个标签，会原样出现在完成项里，用来认出是哪次操作。
    # IBV_SEND_SIGNALED：要求这次操作完成后在 CQ 里放一个完成项；不带就收不到完成通知。
    wr = SendWR(wr_id=1, opcode=opcode, num_sge=1, sg=[sge],
                send_flags=ibv_send_flags.IBV_SEND_SIGNALED)
    # 【单边操作的关键】告诉网卡：对端内存在哪（remote_addr）、凭什么访问（rkey）。
    # SEND 不需要这一步，因为 SEND 的数据落在对端预先挂好的接收缓冲里；WRITE/READ 要直接指定对端地址。
    wr.set_wr_rdma(rkey, remote_addr)
    qp.post_send(wr)                             # 把请求放进发送队列；之后网卡自己取走并执行，CPU 不用管

    # 轮询完成队列（CQ）：网卡做完后会放一个完成项（CQE）。
    # 这是"忙轮询"——不停地问，直到有结果；所以 client 的 CPU 在这段时间占用很高（约一个核）。
    deadline = time.time() + 10
    while time.time() < deadline:
        n, wcs = cq.poll(1)                      # 最多取 1 个完成项，返回 (个数, 完成项列表)
        if n:
            if wcs[0].status != 0:               # status 0 = 成功；非 0 是错误码（如超时重试耗尽）
                raise RuntimeError(f"RDMA 操作失败：wc.status={wcs[0].status}")
            return
        time.sleep(0.0005)
    raise TimeoutError("等待完成超时（检查 GID 下标、hop_limit、traffic_class、网络互通）")


# ============================================================
# 主流程
# ============================================================
def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, help="server 的 OOB（eth0）IP")
    ap.add_argument("--port", type=int, default=18515, help="OOB（TCP）端口")
    ap.add_argument("--dev", default="mlx5_0", help="RDMA 设备名（用 ibv_devices 查看）")
    ap.add_argument("--gid-index", type=int, default=3,
                    help="GID 下标：要选 RoCE v2 的非链路本地项（本集群是 3；fe80:: 开头的不能跨机）")
    ap.add_argument("--tc", type=int, default=104,
                    help="traffic_class（= DSCP×4 + ECN；104 即 DSCP 26）。决定报文进入交换机的哪个队列")
    args = ap.parse_args()

    # ========== 步骤 1：打开设备，检查端口，读本端 GID ==========
    ctx = Context(name=args.dev)                 # 打开 RDMA 设备（相当于 ibv_open_device）
    port = ctx.query_port(1)                     # 查询端口 1 的属性（RDMA 网卡的端口号从 1 开始）
    assert port.state == 4, f"端口不是 ACTIVE：state={port.state}"   # state 4 = ACTIVE，链路必须是通的
    # GID 是 RoCE 的"地址"，由网口的 IP 生成。对端靠它找到我们。
    local_gid = ctx.query_gid(1, args.gid_index)
    print(f"设备 {args.dev}，active_mtu={port.active_mtu}，本端 GID[{args.gid_index}] = {local_gid}")

    # ========== 步骤 2：创建 RDMA 资源 PD / CQ / MR / QP ==========
    pd = PD(ctx)                                 # 保护域：QP 与 MR 必须属于同一个 PD 才能配合使用
    cq = CQ(ctx, 16, None, None, 0)              # 完成队列，最多 16 项；操作做完后网卡在这里放"完成项"
    # 注册本端内存。client 只需要 LOCAL_WRITE：
    #   WRITE 时网卡从这里读数据（读本机内存不需要特殊权限）；
    #   READ 时对端的数据要写回这块内存，所以需要本地写权限。
    # 本端不需要 REMOTE_* 权限，因为没有人会远程访问 client 的内存。
    mr = MR(pd, BUF_SIZE, ibv_access_flags.IBV_ACCESS_LOCAL_WRITE)
    ctypes.memset(mr.buf, 0, BUF_SIZE)                          # 全部清零
    ctypes.memmove(mr.buf, CLIENT_MSG, len(CLIENT_MSG))         # 前半放要写出去的数据

    # 创建 QP。RC = 可靠连接：一对一、按序、由网卡硬件负责确认与重传。
    init = QPInitAttr(qp_type=ibv_qp_type.IBV_QPT_RC, scq=cq, rcq=cq,    # 发送、接收的完成项都放进同一个 CQ
                      cap=QPCap(max_send_wr=4, max_recv_wr=4,            # 发送/接收队列最多挂几个工作请求
                                max_send_sge=1, max_recv_sge=1))         # 每个请求最多几段内存
    qp = QP(pd, init, QPAttr(port_num=1))        # 创建后自动处于 INIT 状态；client 的 QP 不需要开远端访问权限
    psn = random.getrandbits(24)                 # 本端起始包序号（PSN，24 位），用于可靠传输的序号检查

    # ========== 步骤 3：OOB——连接 server 的 TCP，交换连接信息 ==========
    sock = socket.create_connection((args.server, args.port), timeout=60)
    # 交换顺序：client 先发后收，server 先收后发，两边正好对上。
    send_json(sock, {"qp_num": qp.qp_num,        # 我的 QP 号：对端要把响应发给哪个 QP
                     "psn": psn,                 # 我的起始 PSN
                     "gid": str(local_gid)})     # 我的 GID：对端靠它路由回来
    theirs = recv_json(sock)                     # server 的 {qp_num, psn, gid, addr, rkey, msg_len}
    print(f"对端：QPN={theirs['qp_num']} GID={theirs['gid']} "
          f"addr={hex(theirs['addr'])} rkey={theirs['rkey']}")

    # ========== 步骤 4：QP 状态转换 INIT → RTR → RTS ==========
    # "连接"的含义：本端 QP 里填好了对端的 QP 号、PSN、GID，并切到可发送状态。
    # （对端内存的 addr+rkey 是另一层授权，每次操作时才用，不属于"连接"。）

    # --- INIT → RTR（Ready To Receive）：需要对端的信息 ---
    a = QPAttr()
    a.qp_state = ibv_qp_state.IBV_QPS_RTR
    a.path_mtu = port.active_mtu                 # 路径 MTU，取网卡的 active_mtu（这里是 4096）
    a.dest_qp_num = theirs["qp_num"]             # 对端 QP 号
    a.rq_psn = theirs["psn"]                     # 期望收到的第一个包的序号 = 对端的起始 PSN
    a.max_dest_rd_atomic = 16                    # 本端最多同时处理多少个对端发来的 RDMA READ / 原子操作
    a.min_rnr_timer = 12                         # 接收方未就绪（RNR）时通知对端的等待时间
    # 地址向量：RoCE 没有 InfiniBand 的 LID，必须带 GRH（全局路由头），用 GID 寻址。
    gr = GlobalRoute(dgid=GID(theirs["gid"]),    # 目的 GID（对端）；GID 字符串必须是完整的 8 段，不能用 :: 缩写
                     sgid_index=args.gid_index,  # 用本端 GID 表里的哪一项做源地址
                     hop_limit=64,               # IP 头的 TTL；显式设 64 以便跨三层转发（实测默认 1 在本集群也能用）
                     traffic_class=args.tc)      # IP 头的 Traffic Class：高 6 位 DSCP，决定交换机队列
    a.ah_attr = AHAttr(port_num=1, is_global=1, gr=gr, dlid=0)   # is_global=1：带 GRH；dlid=0：RoCE 不用 LID
    qp.to_rtr(a)

    # --- RTR → RTS（Ready To Send）：需要本端的 PSN 与重传参数 ---
    a = QPAttr()
    a.qp_state = ibv_qp_state.IBV_QPS_RTS
    a.sq_psn = psn                               # 本端发出的第一个包的序号
    a.timeout = 14                               # 等 ACK 的超时 = 4.096µs × 2^14 ≈ 67 ms
    a.retry_cnt = 7                              # 超时后最多重传 7 次，仍失败则报错（IBV_WC_RETRY_EXC_ERR）
    a.rnr_retry = 7                              # 对端 RNR 时的重试次数；7 表示无限重试
    a.max_rd_atomic = 16                         # 本端最多同时发起多少个 RDMA READ / 原子操作（READ 必须 ≥ 1）
    qp.to_rts(a)
    print("QP 已就绪（RTS）")

    # ========== 步骤 5：RDMA WRITE —— 本端 [0, ...) → 对端内存 [0, ...) ==========
    rdma_op(qp, cq, ibv_wr_opcode.IBV_WR_RDMA_WRITE,
            mr.buf, len(CLIENT_MSG), mr.lkey,    # 本端：来源地址、长度、lkey
            theirs["addr"], theirs["rkey"])      # 对端：目标地址（前半起点）、rkey
    print(f"RDMA WRITE 完成：写入 {len(CLIENT_MSG)} 字节 -> {CLIENT_MSG!r}")
    # WRITE 对 server 是"无声"的，它不知道数据到了，所以经 OOB 通知它去看一眼。
    sock.sendall(b"W")
    assert sock.recv(1) == b"K"                  # 等 server 看完并确认

    # ========== 步骤 6：RDMA READ —— 对端内存 [2048, ...) → 本端 [2048, ...) ==========
    n = theirs["msg_len"]                        # server 预置数据的长度
    rdma_op(qp, cq, ibv_wr_opcode.IBV_WR_RDMA_READ,
            mr.buf + HALF, n, mr.lkey,           # 本端：落点地址（后半起点）、长度、lkey
            theirs["addr"] + HALF, theirs["rkey"])   # 对端：来源地址（后半起点）、rkey
    got = ctypes.string_at(mr.buf + HALF, n)     # 数据已经被网卡写进本端内存，直接读出来
    print(f"RDMA READ  完成：读回 {n} 字节 -> {got!r}")

    # ========== 步骤 7：通知结束并清理 ==========
    sock.sendall(b"Q")
    sock.close()
    for obj in (qp, cq, mr, pd, ctx):            # 按创建的逆序释放
        obj.close()
    print("client 结束")


if __name__ == "__main__":
    main()
