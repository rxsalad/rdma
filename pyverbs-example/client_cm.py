#!/usr/bin/env python3

import sys
import time
import ctypes

from pyverbs.cmid import CMID, AddrInfo
from pyverbs.qp import QPInitAttr, QPCap
from pyverbs.librdmacm_enums import rdma_port_space


PORT = "18515"
BUFFER_SIZE = 1024


def main():

    server_ip = "10.0.1.2"
    
    # ------------------------------------------------------------
    # Create RDMA CM endpoint
    # ------------------------------------------------------------
    addr = AddrInfo(
        dst=server_ip,
        dst_service=PORT,
        port_space=rdma_port_space.RDMA_PS_TCP,
    )

    # 定义 QP 能力，各种WQE的数量
    cap = QPCap(
        max_send_wr=1,
        max_recv_wr=1,
        max_send_sge=1,
        max_recv_sge=1,
    )

    # 初始化 QP 属性；CM连接管理器，创建底层的PD、CQ、QP等
    qp_init = QPInitAttr(cap=cap)
    client = CMID(
        creator=addr,
        qp_init_attr=qp_init,
    )

    print(f"Connecting to {server_ip}:{PORT}...")

    # 发起RDMA连接，
    client.connect()

    print("RDMA connection established")
    print("RDMA device:", client.dev_name)
    print("RDMA port:", client.port_num)
    print("QPN:", client.qpn)

    # ------------------------------------------------------------
    # Register send buffer
    # ------------------------------------------------------------
    message = b"Hello from pyverbs client!"

    # 注册发送缓冲区
    mr = client.reg_msgs(BUFFER_SIZE)

    print(f"MR buffer address: {hex(mr.buf)}")
    print(f"MR lkey: {mr.lkey}")

    # 把 Python bytes 拷贝到 MR
    # mr.buf is an address (integer), not a Python buffer.
    # Copy Python bytes into the registered memory.
    ctypes.memmove(
        mr.buf,
        message,
        len(message),
    )

    print("Message:", message)

    # ------------------------------------------------------------
    # RDMA SEND
    # ------------------------------------------------------------
    print("Sending...")

    client.post_send(
        mr,
        length=len(message),
    )


    # 轮询 SEND completion
    while True:
        wc = client.get_send_comp()

        if wc is not None:
            if wc.status != 0:
                raise RuntimeError(
                    f"Send failed, status={wc.status}"
                )

            print("Send completed")
            break

        time.sleep(0.001)

    client.close()

    print("Client done")


if __name__ == "__main__":
    main()
