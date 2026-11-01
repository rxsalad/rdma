#!/usr/bin/env python3
"""
client_cm_cpu.py —— 主动方（RDMA CM 版）：经 RDMA 直接读写对端（server）的 CPU 内存。

【与 client_oob_cpu.py 的区别】
  OOB 版：自己建 QP、用 TCP 交换连接信息、手工把 QP 切到 RTR / RTS。
  CM 版 ：交给 rdma_cm（RDMA Connection Manager）：
          - 用 **IP 地址**寻址，rdma_cm 自己找到对应的 RDMA 设备、解析路由、建 QP、切状态；
          - 不需要额外的 TCP；
          - server 内存的 addr / rkey 通过连接建好后的第一条 RDMA SEND 收到；
          - "写完了"、"结束"这类控制信息也走 RDMA SEND / RECV。

【角色】
  client 是"发起操作"的一方：对 server 的内存做一次 RDMA WRITE 和一次 RDMA READ。
  两者都是单边操作，数据由两端网卡直接搬运，server 的 CPU 不参与。

【整体流程】
  ① 创建 CM 端点（给出源 / 目的 IP），rdma_cm 找设备、解析路由、建 QP
  ② 在 connect 之前先挂好接收缓冲（收 server 的内存信息、收 'K' 确认）
  ③ connect()：建立连接（阻塞到连上）
  ④ 收到 server 的内存信息（mr_w 与 mr_r 的 addr、rkey）
  ⑤ RDMA WRITE：本端数据 → server 的 mr_w；再 SEND 'W' 通知 server 去看
  ⑥ 等 server 的 'K' 确认，然后 RDMA READ：server 的 mr_r → 本端，打印
  ⑦ SEND 'Q' 通知结束，清理退出

【注意】rdma_cm 靠 IP 找 RDMA 设备，所以 --server 要填 server 的 **fabric 网口 IP**，
  --src 填本端 fabric 网口的 IP（决定用哪块网卡）。

  【VRF】本集群里每块 fabric 网卡都挂在自己独立的 VRF 上（fabric0 → vrf100 → 路由表 100 …），
  主路由表里没有 fabric 网段的路由。内核按 IP 解析路由时，必须知道"从哪个网口出去"，
  而 --src 正是这个信息：给了 --src，内核才会去该网口所在 VRF 的路由表里查；
  **不给 --src，在本集群上会失败**（Errno 101, Network is unreachable，已实测）。
  所以本集群上 --src 实际是必填的。

  本集群：
    server-0 的 mlx5_0  ↔ fabric0  = fd02:0:400:52e::1
    server-1 的 mlx5_14 ↔ fabric14 = fd02:0:0:527::1

【运行】pyverbs 装在系统 Python 3.10 下：
  python3.10 -u client_cm_cpu.py --src fd02:0:400:52e::1 --server fd02:0:0:527::1
"""

import argparse
import ctypes          # 用来直接读写 MR 的内存（mr.buf 只是一个整数地址）
import struct
import time

from pyverbs.cm_enums import rdma_port_space                # rdma_port_space：rdma_cm 的端口空间
from pyverbs.cmid import AddrInfo, CMID                     # AddrInfo：地址信息；CMID：一条 CM 连接
from pyverbs.enums import ibv_send_flags                    # 发送标志（如 SIGNALED）
from pyverbs.pyverbs_error import PyverbsRDMAError          # rdma_cm 调用失败时抛出的异常
from pyverbs.qp import QPCap, QPInitAttr                    # QP 的能力与初始属性

SIZE = 2048                                                 # 每块内存的大小
CLIENT_MSG = b"Hello from client, written by RDMA WRITE"    # 要写给 server 的数据
INFO_FMT = "!QIQII"      # server 内存信息的二进制格式（网络字节序）：addr_w(8) rkey_w(4) addr_r(8) rkey_r(4) msg_len(4)


def wait_send(cm):
    """忙轮询"发送完成项"。
    对 SEND：网卡确认发出后产生；对 RDMA WRITE / READ：操作完成后产生（需要带 SIGNALED 标志）。
    get_send_comp() 返回完成项 wc；还没完成时返回 None。wc.status == 0 表示成功。"""
    deadline = time.time() + 10
    while time.time() < deadline:
        wc = cm.get_send_comp()
        if wc is not None:
            assert wc.status == 0, f"操作失败 status={wc.status}"
            return
        time.sleep(0.0005)                       # 忙轮询，所以 client 的 CPU 在这段时间占用较高
    raise TimeoutError("等待完成超时")


def wait_recv(cm):
    """忙轮询"接收完成项"（对端的 SEND 到达、数据落进我们挂的接收缓冲后产生），返回 wc。
    完成项按"挂接收缓冲的顺序"返回：第一个 post_recv 对应第一个到达的 SEND。"""
    deadline = time.time() + 30
    while time.time() < deadline:
        wc = cm.get_recv_comp()
        if wc is not None:
            assert wc.status == 0, f"接收失败 status={wc.status}"
            return wc
        time.sleep(0.0005)
    raise TimeoutError("等待接收超时")


def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, help="server 的 fabric 网口 IP（不是 eth0）")
    ap.add_argument("--src", default=None,
                    help="本端 fabric 网口 IP；它决定用哪块 RDMA 网卡，也让内核在该网口所在 VRF 的路由表里查路由。"
                         "在 fabric 网口挂 VRF 的环境（如本集群）上必填，否则 Network is unreachable")
    ap.add_argument("--port", default="18515", help="CM 端口（字符串）")
    args = ap.parse_args()

    # ========== 步骤 1：创建端点 ==========
    # AddrInfo 描述"我要从哪里连到哪里"：
    #   src          本端 IP（可选）；rdma_cm 据此选择本端的网口与 RDMA 设备
    #   dst / dst_service  对端 IP 与端口
    #   port_space=RDMA_PS_TCP  rdma_cm 的"端口空间"，对应 RC（可靠连接）语义；名字里的 TCP 与 TCP 协议无关
    addr = AddrInfo(src=args.src, dst=args.server, dst_service=args.port,
                    port_space=rdma_port_space.RDMA_PS_TCP)
    # QPCap 限定 QP 的队列深度（工作请求个数、每个请求的内存段数）
    cap = QPCap(max_send_wr=4, max_recv_wr=4, max_send_sge=1, max_recv_sge=1)
    # 创建 CMID：rdma_cm 在这一步里依次完成——按 IP 找到网口与 RDMA 设备、解析地址与路由、
    # 创建 PD / CQ / QP。我们不需要手工做这些，也不需要手工做 QP 状态转换。
    # 填 eth0 的 IP 会在这里失败（"No such device"），因为 eth0 上没有 RDMA 设备。
    try:
        cm = CMID(creator=addr, qp_init_attr=QPInitAttr(cap=cap))
    except PyverbsRDMAError as e:
        # 常见原因（在本集群上都实测遇到过）：
        #   Errno 101 Network is unreachable：没给 --src。fabric 网口各自挂在独立的 VRF 上，
        #                                      主路由表里没有 fabric 网段的路由，必须告诉内核出接口。
        #   Errno 19  No such device         ：--server 填成了 eth0 的 IP，eth0 上没有 RDMA 设备。
        print("创建 CM 端点失败：", e)
        print("提示：--server 要填 server 的 fabric 网口 IP；--src 要填本端 fabric 网口 IP"
              "（fabric 网口挂了 VRF 时必填）。可用 `ip -br -6 addr` 与 `rdma link` 查看。")
        raise

    # ========== 步骤 2：connect 之前，先挂好接收缓冲 ==========
    # 【重要】SEND 要求接收方**事先**把接收缓冲挂到接收队列（RQ）里；缓冲没挂好时对端发来的 SEND 没地方放。
    # server 在连接一建立后就会发内存信息过来，所以必须在 connect() 之前就挂好。
    # reg_msgs(size) = 注册一块用于收发消息的内存。
    info = cm.reg_msgs(64)                       # 第 1 条：用来收 server 的内存信息
    ack = cm.reg_msgs(16)                        # 第 2 条：用来收 server 的确认 'K'
    cm.post_recv(info, 64)                       # 完成项会按挂的顺序返回：先内存信息，后确认
    cm.post_recv(ack, 16)

    # ========== 步骤 3：连接 ==========
    print(f"连接 [{args.server}]:{args.port} ...")
    cm.connect()                                 # 阻塞到连接建立；返回时 QP 已处于可收发状态
    print("连接已建立，QPN =", cm.qpn)

    # ========== 步骤 4：收 server 的内存信息 ==========
    # 单边操作要求发起方事先知道对端内存的 addr 和 rkey。server 用一条 SEND 发来，
    # 内容是 struct.pack 打包的二进制：mr_w 的 addr/rkey、mr_r 的 addr/rkey、预置数据长度。
    wait_recv(cm)
    addr_w, rkey_w, addr_r, rkey_r, msg_len = struct.unpack(
        INFO_FMT, ctypes.string_at(info.buf, struct.calcsize(INFO_FMT)))   # 从接收缓冲里读出并解包
    print(f"对端内存：mr_w addr={hex(addr_w)} rkey={rkey_w}；mr_r addr={hex(addr_r)} rkey={rkey_r}")

    # 注册本端的两块内存：WRITE 的来源、READ 的落点。
    # reg_msgs 含本地写权限，READ 回来的数据要写进 dst_mr，所以够用。
    src_mr = cm.reg_msgs(SIZE)                   # WRITE 的来源
    dst_mr = cm.reg_msgs(SIZE)                   # READ 的落点
    ctypes.memset(dst_mr.buf, 0, SIZE)                          # 落点先清零
    ctypes.memmove(src_mr.buf, CLIENT_MSG, len(CLIENT_MSG))     # 来源放要写出去的数据

    # ========== 步骤 5：RDMA WRITE —— 本端 src_mr → 对端 mr_w ==========
    # post_write(本端 MR, 长度, 对端地址, 对端 rkey)：
    #   单边操作的关键就是这里要直接给出对端内存的 addr 和 rkey，网卡凭 rkey 校验权限后直接写入。
    #   IBV_SEND_SIGNALED：要求这次操作完成后产生一个完成项；不带就收不到完成通知（本篇显式带上）。
    cm.post_write(src_mr, len(CLIENT_MSG), addr_w, rkey_w,
                  flags=ibv_send_flags.IBV_SEND_SIGNALED)
    wait_send(cm)
    print(f"RDMA WRITE 完成：写入 {len(CLIENT_MSG)} 字节 -> {CLIENT_MSG!r}")

    # WRITE 对 server 是"无声"的，它不知道数据到了。用一条 SEND 发 'W' 通知它去看一眼。
    ctrl = cm.reg_msgs(16)
    ctypes.memmove(ctrl.buf, b"W", 1)
    cm.post_send(ctrl, length=1)
    wait_send(cm)
    wait_recv(cm)                                # 等 server 看完后回的确认 'K'（对应第 2 条接收缓冲）

    # ========== 步骤 6：RDMA READ —— 对端 mr_r → 本端 dst_mr ==========
    # 与 WRITE 对称：这次是网卡从对端内存读数据，再写回本端 dst_mr。
    cm.post_read(dst_mr, msg_len, addr_r, rkey_r,
                 flags=ibv_send_flags.IBV_SEND_SIGNALED)
    wait_send(cm)
    got = ctypes.string_at(dst_mr.buf, msg_len)  # 数据已经被网卡写进本端内存，直接读出来
    print(f"RDMA READ  完成：读回 {msg_len} 字节 -> {got!r}")

    # ========== 步骤 7：通知结束并清理 ==========
    ctypes.memmove(ctrl.buf, b"Q", 1)            # 通知 server 可以结束了
    cm.post_send(ctrl, length=1)
    wait_send(cm)
    cm.close()                                   # 关闭连接（释放 QP / CQ / MR 等）
    print("client 结束")


if __name__ == "__main__":
    main()
