#!/usr/bin/env python3
"""
server_cm_cpu.py —— 被动方（RDMA CM 版）：注册 CPU 内存，让对端经 RDMA 直接读写它。

【与 server_oob_cpu.py 的区别】
  OOB 版：自己建 QP、用 TCP 交换连接信息、手工把 QP 切到 RTR / RTS。
  CM 版 ：交给 rdma_cm（RDMA Connection Manager）：
          - 用 **IP 地址**寻址，rdma_cm 自己找到对应的 RDMA 设备、解析路由、建 QP、切状态；
          - 不需要额外的 TCP；
          - 连接建好后，内存的 addr / rkey 改用一条 RDMA SEND 告诉对端；
          - "写完了"、"结束"这类控制信息也走 RDMA SEND / RECV。

【角色】
  server 是"被访问"的一方。连接建好后，client 对它的内存做 RDMA WRITE / READ 时，
  server 的 CPU 完全不参与（单边操作）。

【整体流程】
  ① 创建监听端点（绑定本端 fabric 网口的 IP，等于选定了网卡），listen
  ② get_request()：取到一个连接请求，得到代表这条连接的新 CMID（已带好 PD / CQ / QP）
  ③ 注册两块 CPU 内存：mr_w（允许远端写）、mr_r（允许远端读）
  ④ 在 accept 之前先挂好接收缓冲（用来收 client 的 'W'、'Q'）
  ⑤ accept()，然后用一条 SEND 把 mr_w / mr_r 的 addr、rkey 发给 client
  ⑥ 等 client 的 'W'（RDMA WRITE 做完了）→ 读自己的内存 → SEND 'K' 确认
  ⑦ 等 client 的 'Q'（结束），清理退出

【内存】
  mr_w  2048 字节  允许远端写（reg_write），初始为 0        —— client 用 RDMA WRITE 写它
  mr_r  2048 字节  允许远端读（reg_read），预置数据          —— client 用 RDMA READ 读它

【注意】rdma_cm 靠 IP 找 RDMA 设备，所以 --bind 要填 **fabric 网口的 IP**（不是 eth0）。
  本集群 server-1 的 mlx5_14 对应 fabric14，地址 fd02:0:0:527::1。

【运行】pyverbs 装在系统 Python 3.10 下：
  python3.10 -u server_cm_cpu.py --bind fd02:0:0:527::1
"""

import argparse
import ctypes          # 用来直接读写 MR 的内存（mr.buf 只是一个整数地址）
import struct
import time

from pyverbs.cm_enums import RAI_PASSIVE, rdma_port_space   # RAI_PASSIVE：被动端；rdma_port_space：端口空间
from pyverbs.cmid import AddrInfo, CMID                      # AddrInfo：地址信息；CMID：一条 CM 连接 / 监听端点
from pyverbs.qp import QPCap, QPInitAttr                     # QP 的能力与初始属性

SIZE = 2048                                                  # 每块内存的大小
SERVER_MSG = b"Hello from server CPU memory (read me with RDMA READ)"   # 预置给 client 读取的数据
INFO_FMT = "!QIQII"      # 内存信息的二进制格式（网络字节序）：addr_w(8) rkey_w(4) addr_r(8) rkey_r(4) msg_len(4)


def wait_send(cm):
    """忙轮询"发送完成项"（SEND 发出去、网卡确认完成后产生）。
    get_send_comp() 返回完成项 wc；还没完成时返回 None。wc.status == 0 表示成功。"""
    deadline = time.time() + 10
    while time.time() < deadline:
        wc = cm.get_send_comp()
        if wc is not None:
            assert wc.status == 0, f"发送失败 status={wc.status}"
            return
        time.sleep(0.0005)
    raise TimeoutError("等待发送完成超时")


def wait_recv(cm):
    """忙轮询"接收完成项"（对端的 SEND 到达、数据落进我们挂的接收缓冲后产生），返回 wc。
    完成项按"挂接收缓冲的顺序"返回：第一个 post_recv 对应第一个到达的 SEND。"""
    deadline = time.time() + 60
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
    ap.add_argument("--bind", required=True,
                    help="监听的 IP（fabric 网口的地址）。它决定用哪块 RDMA 网卡：rdma_cm 按 IP → 网口 → RDMA 设备查找")
    ap.add_argument("--port", default="18515", help="CM 端口（字符串）")
    args = ap.parse_args()

    # ========== 步骤 1：创建监听端点并开始监听 ==========
    # AddrInfo 描述"我要在哪个地址上做什么"：
    #   src / src_service  本端绑定的 IP 与端口
    #   port_space=RDMA_PS_TCP  rdma_cm 的"端口空间"，对应 RC（可靠连接）语义；名字里的 TCP 与 TCP 协议无关
    #   flags=RAI_PASSIVE  表示我是被动端（server）：只监听，不主动连别人
    addr = AddrInfo(src=args.bind, src_service=args.port,
                    port_space=rdma_port_space.RDMA_PS_TCP, flags=RAI_PASSIVE)
    # QPCap 限定之后为每条连接创建的 QP 的队列深度（工作请求个数、每个请求的内存段数）
    cap = QPCap(max_send_wr=4, max_recv_wr=4, max_send_sge=1, max_recv_sge=1)
    # CMID = 一个 RDMA CM 标识符。这里它是"监听端点"。qp_init_attr 会作为模板，用于给后来的每条连接建 QP。
    listener = CMID(creator=addr, qp_init_attr=QPInitAttr(cap=cap))
    listener.listen()
    print(f"CM 监听 {args.bind}:{args.port}，等待 client ...")

    # ========== 步骤 2：取一个连接请求 ==========
    # get_request() 阻塞到有 client 来连，返回一个**新的** CMID，代表这条具体的连接。
    # 这个新 CMID 已经带好了 PD / CQ / QP（由 rdma_cm 创建），我们直接用它收发和注册内存。
    conn = listener.get_request()
    print("收到连接请求，QPN =", conn.qpn)

    # ========== 步骤 3：注册两块 CPU 内存 ==========
    # 注册 = 让网卡能直接 DMA 访问这块内存，并得到访问凭证 rkey（会钉住这些内存页）。
    # rdma_cm 的三个注册函数对应不同的访问权限：
    #   reg_write(size)  允许**远端写**（对端的 RDMA WRITE 可以写进来）
    #   reg_read(size)   允许**远端读**（对端的 RDMA READ 可以读出去）
    #   reg_msgs(size)   普通收发消息（SEND/RECV）用，只有本地权限
    mr_w = conn.reg_write(SIZE)
    mr_r = conn.reg_read(SIZE)
    # mr.buf 是这块内存的虚拟地址（整数），不是 Python 的 bytes，要用 ctypes 直接读写它
    ctypes.memset(mr_w.buf, 0, SIZE)                             # mr_w 清零，等 client 来写
    ctypes.memset(mr_r.buf, 0, SIZE)
    ctypes.memmove(mr_r.buf, SERVER_MSG, len(SERVER_MSG))        # mr_r 放预置数据，等 client 来读
    print(f"已注册 CPU 内存：mr_w addr={hex(mr_w.buf)} rkey={mr_w.rkey}；"
          f"mr_r addr={hex(mr_r.buf)} rkey={mr_r.rkey}")

    # ========== 步骤 4：accept 之前，先挂好接收缓冲 ==========
    # 【重要】SEND 要求接收方**事先**把接收缓冲挂到接收队列（RQ）里；缓冲没挂好时对端发来的 SEND 没地方放。
    # 连接一建立对端就可能发消息，所以必须在 accept() 之前就挂好。
    # 这里挂两条：第一条收 client 的 'W'（WRITE 做完了），第二条收 'Q'（结束）。
    w_buf = conn.reg_msgs(16)
    q_buf = conn.reg_msgs(16)
    conn.post_recv(w_buf, 16)                    # 完成项会按挂的顺序返回：先 'W' 后 'Q'
    conn.post_recv(q_buf, 16)

    # ========== 步骤 5：接受连接，把内存信息告诉 client ==========
    # accept() 之后，rdma_cm 已经把 QP 切到可用状态（相当于 OOB 版里手工做的 INIT → RTR → RTS）。
    conn.accept()
    print("连接已接受")

    # client 要对我们做 WRITE / READ，必须知道每块内存的 addr 和 rkey。
    # 用 struct.pack 把它们打包成一段二进制，放进一块消息缓冲，用一条 RDMA SEND 发给 client。
    # （client 在 connect 之前就挂好了接收缓冲，所以收得到。）
    info = conn.reg_msgs(64)
    ctypes.memmove(info.buf,
                   struct.pack(INFO_FMT, mr_w.buf, mr_w.rkey, mr_r.buf, mr_r.rkey, len(SERVER_MSG)),
                   struct.calcsize(INFO_FMT))
    conn.post_send(info, length=struct.calcsize(INFO_FMT))
    wait_send(conn)                              # 等这条 SEND 完成
    print("已把内存的 addr / rkey 发给 client。之后 server 不参与数据传输。")

    # ========== 步骤 6：等 client 通知"WRITE 做完了" ==========
    # RDMA WRITE 对 server 是"无声"的：数据悄悄写进了 mr_w，server 的 CPU 和程序都不知道。
    # 所以 client 写完后用一条 SEND 发 'W' 告诉我们"可以去看了"。
    wait_recv(conn)
    # mr_w 里 client 写入的是字符串，后面都是 0；按第一个 \0 截断即可得到原字符串
    got = ctypes.string_at(mr_w.buf, SIZE).split(b"\0", 1)[0]
    print("[server] client 的 RDMA WRITE 之后，我的内存 mr_w 里是：", got)
    ack = conn.reg_msgs(16)
    ctypes.memmove(ack.buf, b"K", 1)
    conn.post_send(ack, length=1)                # 回一个确认 'K'；client 收到后才继续做 READ
    wait_send(conn)

    # ========== 步骤 7：等 client 的 'Q'（结束），清理 ==========
    wait_recv(conn)                              # 第二条接收缓冲 = 'Q'
    conn.close()                                 # 关闭这条连接（释放它的 QP / CQ / MR 等）
    listener.close()                             # 关闭监听端点
    print("server 结束")


if __name__ == "__main__":
    main()
