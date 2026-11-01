#!/usr/bin/env python3
"""
server_oob_cpu.py —— 被动方：注册一块 CPU 内存，让对端经 RDMA 直接读写它。

【角色】
  server 是"被访问"的一方。它只做准备工作（注册内存、建连），之后 client 对它的内存做
  RDMA WRITE / READ 时，server 的 CPU 完全不参与——数据由两端网卡直接搬运。
  这就是"单边操作"：对端的 CPU 不知道数据什么时候到、什么时候被读走。

【OOB 是什么】
  OOB = Out-Of-Band（带外）。RDMA 的单边操作要求发起方事先知道对端的
  QP 号、起始包序号(PSN)、GID（RoCE 的地址）、内存地址(addr)和访问凭证(rkey)。
  这些信息只能先通过"另一条通道"交换——这里用最简单的 TCP。
  注意：TCP 只传这几十个字节的控制信息；真正的数据走 RDMA，不经过 TCP。

【整体流程】
  ① 打开 RDMA 设备 → 建 PD / CQ / MR / QP（QP 此时处于 INIT 状态）
  ② 在 TCP 上监听，收到 client 的连接信息后，把自己的信息（含 addr、rkey）回给它
  ③ 用对端的信息把 QP 依次切到 RTR（准备接收）→ RTS（准备发送），连接就绪
  ④ 等 client 通过 TCP 发来 'W'（RDMA WRITE 做完了），读一下自己的内存，证明数据到了
  ⑤ 等 client 发来 'Q'（结束），清理退出

【内存布局】（一块 4096 字节）
  [   0, 2048)  留给 client 用 RDMA WRITE 写入，初始为 0
  [2048, 4096)  server 预先放好的数据，供 client 用 RDMA READ 读取

【运行】Pod 里 pyverbs 装在系统 Python 3.10 下，所以用 python3.10：
  python3.10 -u server_oob_cpu.py --dev mlx5_14 --gid-index 3
  （-u 让输出不缓冲，重定向到文件时才能及时看到日志）
"""

import argparse
import ctypes          # 用来直接读写 MR 的内存（mr.buf 只是一个整数地址）
import json
import random
import socket
import struct

from pyverbs.addr import GID, GlobalRoute, AHAttr          # 地址相关：GID、全局路由、地址向量
from pyverbs.cq import CQ                                  # 完成队列
from pyverbs.device import Context                         # 打开 RDMA 设备
from pyverbs.enums import ibv_access_flags, ibv_qp_state, ibv_qp_type   # 枚举：权限、QP 状态、QP 类型
from pyverbs.mr import MR                                  # 内存区域（注册内存）
from pyverbs.pd import PD                                  # 保护域
from pyverbs.qp import QP, QPAttr, QPCap, QPInitAttr      # 队列对及其属性

BUF_SIZE = 4096            # 注册的内存总大小
HALF = BUF_SIZE // 2       # 前半给 client 写，后半放 server 的预置数据
SERVER_MSG = b"Hello from server CPU memory (read me with RDMA READ)"   # 预置给 client 读取的数据


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
# 主流程
# ============================================================
def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default="mlx5_14", help="RDMA 设备名（用 ibv_devices 查看）")
    ap.add_argument("--gid-index", type=int, default=3,
                    help="GID 下标：要选 RoCE v2 的非链路本地项（本集群是 3；fe80:: 开头的不能跨机）")
    ap.add_argument("--bind", default="0.0.0.0", help="OOB（TCP）监听地址")
    ap.add_argument("--port", type=int, default=18515, help="OOB（TCP）端口")
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

    # 注册内存（MR）：让网卡能直接 DMA 访问这块内存，并得到访问凭证 rkey。
    # 访问权限要显式声明：
    #   LOCAL_WRITE  本机可写（注册内存的基本权限）
    #   REMOTE_WRITE 允许对端用 RDMA WRITE 写进来
    #   REMOTE_READ  允许对端用 RDMA READ 读出去
    acc = (ibv_access_flags.IBV_ACCESS_LOCAL_WRITE
           | ibv_access_flags.IBV_ACCESS_REMOTE_WRITE
           | ibv_access_flags.IBV_ACCESS_REMOTE_READ)
    mr = MR(pd, BUF_SIZE, acc)                   # 一步完成"分配 4096 字节 + 注册"，会钉住这些内存页
    # mr.buf 是这块内存的虚拟地址（整数），不是 Python 的 bytes，要用 ctypes 直接读写它
    ctypes.memset(mr.buf, 0, BUF_SIZE)                              # 全部清零
    ctypes.memmove(mr.buf + HALF, SERVER_MSG, len(SERVER_MSG))      # 后半放预置数据，等 client 来 READ
    print(f"已注册 CPU 内存：addr={hex(mr.buf)} rkey={mr.rkey} len={BUF_SIZE}")

    # 创建 QP（队列对）：RDMA 通信的端点。
    # QPInitAttr 的 qp_type=RC 表示"可靠连接"：一对一、按序、由网卡硬件负责确认与重传。
    init = QPInitAttr(qp_type=ibv_qp_type.IBV_QPT_RC, scq=cq, rcq=cq,    # 发送、接收的完成项都放进同一个 CQ
                      cap=QPCap(max_send_wr=4, max_recv_wr=4,            # 发送/接收队列最多挂几个工作请求
                                max_send_sge=1, max_recv_sge=1))         # 每个请求最多几段内存
    qp_attr = QPAttr(port_num=1)                 # 使用网卡的端口 1
    # 【关键】QP 自己也要声明是否接受对端的读写。只给 MR 开权限、QP 不开，对端的 WRITE/READ 会被拒绝。
    qp_attr.qp_access_flags = (ibv_access_flags.IBV_ACCESS_REMOTE_WRITE
                               | ibv_access_flags.IBV_ACCESS_REMOTE_READ)
    qp = QP(pd, init, qp_attr)                   # 创建后自动处于 INIT 状态
    psn = random.getrandbits(24)                 # 本端起始包序号（PSN，24 位），用于可靠传输的序号检查

    # ========== 步骤 3：OOB——监听 TCP，与 client 交换连接信息 ==========
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # 允许快速重启时重用端口
    srv.bind((args.bind, args.port))
    srv.listen(1)
    print(f"OOB 监听 {args.bind}:{args.port}，等待 client ...")
    conn, peer = srv.accept()                    # 阻塞，直到 client 连上来
    print("OOB 连接来自", peer)

    # 交换顺序：server 先收后发，client 先发后收，两边正好对上（两边都先收会互相等待而死锁）。
    theirs = recv_json(conn)                     # client 的 {qp_num, psn, gid}
    send_json(conn, {"qp_num": qp.qp_num,        # 我的 QP 号：对端要把包发给哪个 QP
                     "psn": psn,                 # 我的起始 PSN
                     "gid": str(local_gid),      # 我的 GID：对端靠它路由到我这块网卡
                     "addr": mr.buf,             # 我的内存起始地址：对端 WRITE/READ 的目标地址
                     "rkey": mr.rkey,            # 我的内存访问凭证：每次单边操作都必须带上
                     "msg_len": len(SERVER_MSG)})  # 预置数据的长度，方便 client 知道读多少

    # ========== 步骤 4：QP 状态转换 INIT → RTR → RTS ==========
    # 到这一步，"连接"的含义是：本端 QP 里填好了对端的 QP 号、PSN、GID，并切到可发送状态。
    # （内存授权 addr+rkey 是另一层，每次操作时才用，不属于"连接"。）

    # --- INIT → RTR（Ready To Receive，准备接收）：需要对端的信息 ---
    a = QPAttr()
    a.qp_state = ibv_qp_state.IBV_QPS_RTR
    a.path_mtu = port.active_mtu                 # 路径 MTU，取网卡的 active_mtu（这里是 4096）
    a.dest_qp_num = theirs["qp_num"]             # 对端 QP 号
    a.rq_psn = theirs["psn"]                     # 期望收到的第一个包的序号 = 对端的起始 PSN
    a.max_dest_rd_atomic = 16                    # 本端最多同时处理多少个对端发来的 RDMA READ / 原子操作
    a.min_rnr_timer = 12                         # 接收方未就绪（RNR）时通知对端的等待时间
    # 地址向量：RoCE 没有 InfiniBand 的 LID，必须带 GRH（全局路由头），用 GID 寻址。
    gr = GlobalRoute(dgid=GID(theirs["gid"]),    # 目的 GID（对端）
                     sgid_index=args.gid_index,  # 用本端 GID 表里的哪一项做源地址
                     hop_limit=64,               # IP 头的 TTL；显式设 64 以便跨三层转发（实测默认 1 在本集群也能用）
                     traffic_class=args.tc)      # IP 头的 Traffic Class：高 6 位 DSCP，决定交换机队列
    a.ah_attr = AHAttr(port_num=1, is_global=1, gr=gr, dlid=0)   # is_global=1：带 GRH；dlid=0：RoCE 不用 LID
    qp.to_rtr(a)

    # --- RTR → RTS（Ready To Send，准备发送）：需要本端的 PSN 与重传参数 ---
    a = QPAttr()
    a.qp_state = ibv_qp_state.IBV_QPS_RTS
    a.sq_psn = psn                               # 本端发出的第一个包的序号
    a.timeout = 14                               # 等 ACK 的超时 = 4.096µs × 2^14 ≈ 67 ms
    a.retry_cnt = 7                              # 超时后最多重传 7 次，仍失败则报错（IBV_WC_RETRY_EXC_ERR）
    a.rnr_retry = 7                              # 对端 RNR 时的重试次数；7 表示无限重试
    a.max_rd_atomic = 16                         # 本端最多同时发起多少个 RDMA READ / 原子操作
    qp.to_rts(a)
    print("QP 已就绪（RTS）。之后 server 不再参与数据传输，只等 client 的通知。")

    # ========== 步骤 5：等 client 的 OOB 通知 ==========
    # RDMA WRITE 对 server 是"无声"的：数据悄悄写进了内存，server 的 CPU 和程序都不知道。
    # 所以 client 写完后要经 TCP 发一个 'W' 告诉 server"可以去看了"。
    while True:
        cmd = conn.recv(1)
        if cmd == b"W":
            # 内存里 client 写入的是字符串，后面都是 0；按第一个 \0 截断即可得到原字符串
            got = ctypes.string_at(mr.buf, HALF).split(b"\0", 1)[0]
            print("[server] client 的 RDMA WRITE 之后，我的内存 [0,2048) 里是：", got)
            conn.sendall(b"K")                   # 回个确认，client 收到后才继续做 READ
        else:                                    # 'Q'（client 结束）或连接已关闭
            break

    # ========== 步骤 6：清理 ==========
    conn.close()
    srv.close()
    for obj in (qp, cq, mr, pd, ctx):            # 按创建的逆序释放；MR 要先注销（close）再让内存消失
        obj.close()
    print("server 结束")


if __name__ == "__main__":
    main()
