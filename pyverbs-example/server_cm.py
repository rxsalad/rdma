#!/usr/bin/env python3

import time
import ctypes

from pyverbs.cmid import CMID, AddrInfo
from pyverbs.qp import QPInitAttr, QPCap
from pyverbs.librdmacm_enums import rdma_port_space, RAI_PASSIVE


PORT = "18515"
BUFFER_SIZE = 1024


def main():
    # ------------------------------------------------------------
    # 1. Create RDMA CM listening endpoint
    # ------------------------------------------------------------
    addr = AddrInfo(
        src="0.0.0.0",
        src_service=PORT,
        port_space=rdma_port_space.RDMA_PS_TCP,
        flags=RAI_PASSIVE,
    ) # 我是 Server，我要监听这个地址

    cap = QPCap(
        max_send_wr=1,
        max_recv_wr=1,
        max_send_sge=1,
        max_recv_sge=1,
    )

    qp_init = QPInitAttr(cap=cap)

    server = CMID(
        creator=addr,
        qp_init_attr=qp_init,
    )

    print(f"Listening on RDMA port {PORT}...")

    server.listen()

    # ------------------------------------------------------------
    # 2. Wait for client
    # ------------------------------------------------------------
    client = server.get_request()

    print("Client connected")
    print("RDMA device:", client.dev_name)
    print("RDMA port:", client.port_num)
    print("QPN:", client.qpn)

    # ------------------------------------------------------------
    # 3. Accept RDMA connection
    # ------------------------------------------------------------
    client.accept()

    print("RDMA connection accepted")

    # ------------------------------------------------------------
    # 4. Register receive buffer
    # ------------------------------------------------------------
    mr = client.reg_msgs(BUFFER_SIZE)

    print(f"MR buffer address: {hex(mr.buf)}")
    print(f"MR lkey: {mr.lkey}")

    # ------------------------------------------------------------
    # 5. Post receive
    #
    # IMPORTANT:
    # The receive WR must be posted before the client sends.
    # ------------------------------------------------------------
    client.post_recv(
        mr,
        BUFFER_SIZE,
    )

    print("Receive WR posted")
    print("Waiting for RDMA SEND...")

    # ------------------------------------------------------------
    # 6. Poll receive completion
    # ------------------------------------------------------------
    while True:
        wc = client.get_recv_comp()

        if wc is not None:
            print(f"Receive completion status: {wc.status}")

            if wc.status != 0:
                raise RuntimeError(
                    f"Receive failed, status={wc.status}"
                )

            print(f"Received {wc.byte_len} bytes")

            # ----------------------------------------------------
            # mr.buf is an integer containing the memory address.
            #
            # Do NOT do:
            #
            #     bytes(mr.buf[:wc.byte_len])
            #
            # Instead, read directly from that address.
            # ----------------------------------------------------
            data = ctypes.string_at(
                mr.buf,
                wc.byte_len,
            )

            print("Message:", data.decode(errors="replace"))

            break

        time.sleep(0.001)

    # ------------------------------------------------------------
    # 7. Cleanup
    # ------------------------------------------------------------
    client.close()
    server.close()

    print("Server done")


if __name__ == "__main__":
    main()
