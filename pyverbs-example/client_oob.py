#!/usr/bin/env python3

import socket
import struct
import json
import time
import random
import ctypes

from pyverbs.device import Context
from pyverbs.pd import PD
from pyverbs.cq import CQ
from pyverbs.qp import QP, QPInitAttr, QPCap, QPAttr
from pyverbs.mr import MR
from pyverbs.addr import GID, GlobalRoute, AHAttr
from pyverbs.wr import SGE, SendWR
from pyverbs.libibverbs_enums import (
    ibv_qp_type, ibv_qp_state, ibv_mtu, ibv_access_flags,
    ibv_wr_opcode, ibv_send_flags,
)


# ============================================================
# Configuration
# ============================================================

SERVER_OOB_IP = "192.168.68.192" # TCP OOB control plane
OOB_PORT = 18515

RDMA_DEV = "rxe_eth1"
RDMA_PORT = 1             # RDMA device 上的 port 1
GID_INDEX = 1             # GID table index to use
BUFFER_SIZE = 1024
MESSAGE = b"Hello from pyverbs OOB client!"

PATH_MTU = ibv_mtu.IBV_MTU_1024
MAX_DEST_RD_ATOMIC = 1
MAX_RD_ATOMIC = 1
MIN_RNR_TIMER = 12
TIMEOUT = 14
RETRY_CNT = 7
RNR_RETRY = 7


# ============================================================
# OOB helpers
# ============================================================

def recv_all(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("OOB connection closed unexpectedly")
        data.extend(chunk)
    return bytes(data)


def send_json(sock, obj):
    payload = json.dumps(obj).encode("utf-8")
    header = struct.pack("!I", len(payload))
    sock.sendall(header)
    sock.sendall(payload)


def recv_json(sock):
    header = recv_all(sock, 4)
    length = struct.unpack("!I", header)[0]
    payload = recv_all(sock, length)
    return json.loads(payload.decode("utf-8"))


# ============================================================
# GID helper
# ============================================================

def gid_from_string(gid_string):
    """
    Convert a textual GID such as:

        0000:0000:0000:0000:0000:ffff:0a00:0102

    into a pyverbs GID object.

    This is required because the remote GID belongs to the
    remote host and therefore cannot be obtained from the
    local GID table.
    """

    gid = GID()

    # GID contains a 16-byte ibv_gid.
    #
    # pyverbs GID exposes the underlying value through
    # the string representation. For pyverbs versions where
    # GID(value) is supported, use that first.
    try:
        return GID(gid_string)
    except TypeError:
        pass

    # Fallback for pyverbs versions whose GID constructor
    # does not accept a string.
    #
    # Convert:
    #
    #   0000:0000:0000:0000:0000:ffff:0a00:0102
    #
    # to 16 bytes.
    parts = gid_string.split(":")

    if len(parts) != 8:
        raise ValueError(f"Invalid GID: {gid_string}")

    raw = b"".join(
        int(x, 16).to_bytes(2, byteorder="big") for x in parts
    )

    if len(raw) != 16:
        raise ValueError(f"Invalid GID length: {gid_string}")

    # Try common pyverbs representations.
    #
    # Some pyverbs versions expose `raw` as a writable
    # 16-byte buffer.
    try:
        gid.raw = raw
        return gid
    except Exception:
        pass

    raise RuntimeError(
        "Unable to construct remote GID with this "
        "pyverbs version.\n"
        f"GID = {gid_string}"
    )


# ============================================================
# RDMA resource creation
# ============================================================

def create_resources():
    print()
    print("Opening RDMA device:", RDMA_DEV)

    ctx = Context(name=RDMA_DEV)
    port_attr = ctx.query_port(RDMA_PORT)

    print("RDMA port state:", port_attr.state)
    print("RDMA physical state:", port_attr.phys_state)
    print("RDMA link layer:", port_attr.link_layer)
    print("RDMA GID table length:", port_attr.gid_tbl_len)

    if port_attr.state != 4:
        raise RuntimeError(
            f"RDMA port {RDMA_PORT} is not ACTIVE: "
            f"state={port_attr.state}"
        )

    if port_attr.phys_state != 5:
        raise RuntimeError(
            f"RDMA port {RDMA_PORT} is not LINK_UP: "
            f"physical_state={port_attr.phys_state}"
        )

    local_gid = ctx.query_gid(RDMA_PORT, GID_INDEX)
    print("Local GID:", local_gid)

    pd = PD(ctx)
    cq = CQ(ctx, 16, None, None, 0)

    cap = QPCap(
        max_send_wr=1,
        max_recv_wr=1,
        max_send_sge=1,
        max_recv_sge=1,
    )

    qp_init_attr = QPInitAttr(
        qp_type=ibv_qp_type.IBV_QPT_RC,
        scq=cq,
        rcq=cq,
        cap=cap,
    )

    qp_attr = QPAttr(port_num=RDMA_PORT)
    qp = QP(pd, qp_init_attr, qp_attr)

    print("QP created")
    print("QPN:", qp.qp_num)

    local_psn = random.getrandbits(24)
    print("Local PSN:", local_psn)

    mr = MR(
        pd,
        BUFFER_SIZE,
        ibv_access_flags.IBV_ACCESS_LOCAL_WRITE,
    )

    print("MR buffer address:", hex(mr.buf))
    print("MR lkey:", mr.lkey)

    return ctx, pd, cq, qp, mr, local_gid, local_psn


# ============================================================
# QP state transition
# ============================================================

def qp_to_rtr(qp, remote_qpn, remote_psn, remote_gid):
    print()
    print("Moving QP to RTR...")

    attr = QPAttr()
    attr.qp_state = ibv_qp_state.IBV_QPS_RTR
    attr.path_mtu = PATH_MTU
    attr.dest_qp_num = remote_qpn
    attr.rq_psn = remote_psn
    attr.max_dest_rd_atomic = MAX_DEST_RD_ATOMIC
    attr.min_rnr_timer = MIN_RNR_TIMER

    gr = GlobalRoute(dgid=remote_gid, sgid_index=GID_INDEX)
    ah_attr = AHAttr(
        port_num=RDMA_PORT,
        is_global=1,
        gr=gr,
        dlid=0,
    )
    attr.ah_attr = ah_attr

    qp.to_rtr(attr)
    print("QP state: RTR")


def qp_to_rts(qp, local_psn):
    print("Moving QP to RTS...")

    attr = QPAttr()
    attr.qp_state = ibv_qp_state.IBV_QPS_RTS
    attr.sq_psn = local_psn
    attr.timeout = TIMEOUT
    attr.retry_cnt = RETRY_CNT
    attr.rnr_retry = RNR_RETRY
    attr.max_rd_atomic = MAX_RD_ATOMIC

    qp.to_rts(attr)
    print("QP state: RTS")


# ============================================================
# Main
# ============================================================

def main():
    print("========================================")
    print("Pyverbs OOB RDMA Client")
    print("========================================")
    print("OOB server :", SERVER_OOB_IP)
    print("OOB port   :", OOB_PORT)
    print("RDMA device:", RDMA_DEV)
    print("RDMA port  :", RDMA_PORT)
    print("GID index  :", GID_INDEX)
    print("========================================")

    ctx, pd, cq, qp, mr, local_gid, local_psn = create_resources()

    sock = None
    try:
        print()
        print(f"Connecting to OOB server {SERVER_OOB_IP}:{OOB_PORT}...")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(600)
        sock.connect((SERVER_OOB_IP, OOB_PORT))
        print("OOB TCP connection established")

        # ----------------------------------------------------
        # Exchange RDMA connection information
        # ----------------------------------------------------

        local_info = {
            "qp_num": int(qp.qp_num),
            "psn": int(local_psn),
            "gid_index": int(GID_INDEX),
            "gid": str(local_gid),
        }

        print()
        print("Sending local RDMA information:")
        print(local_info)

        send_json(sock, local_info)
        remote_info = recv_json(sock)

        print()
        print("Received remote RDMA information:")
        print(remote_info)

        remote_qpn = int(remote_info["qp_num"])
        remote_psn = int(remote_info["psn"])
        remote_gid_string = remote_info["gid"]

        print()
        print("Remote QPN:", remote_qpn)
        print("Remote PSN:", remote_psn)
        print("Remote GID:", remote_gid_string)

        # ----------------------------------------------------
        # Construct remote GID
        # ----------------------------------------------------

        remote_gid = gid_from_string(remote_gid_string)
        print("Remote GID object:", remote_gid)

        # ----------------------------------------------------
        # QP RESET -> RTR
        # ----------------------------------------------------

        qp_to_rtr(qp, remote_qpn, remote_psn, remote_gid)

        # ----------------------------------------------------
        # QP RTR -> RTS
        # ----------------------------------------------------

        qp_to_rts(qp, local_psn)

        print()
        print("RDMA QP connection established")

        # ----------------------------------------------------
        # Prepare SEND buffer
        # ----------------------------------------------------

        ctypes.memmove(mr.buf, MESSAGE, len(MESSAGE))

        print()
        print("Message:", MESSAGE)

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        sge = SGE(mr.buf, len(MESSAGE), mr.lkey)

        send_wr = SendWR(
            wr_id=1,
            opcode=ibv_wr_opcode.IBV_WR_SEND,
            num_sge=1,
            sg=[sge],
            send_flags=ibv_send_flags.IBV_SEND_SIGNALED,
        )

        print("Posting RDMA SEND...")
        qp.post_send(send_wr)
        print("SEND posted")

        # ----------------------------------------------------
        # Wait for SEND completion
        # ----------------------------------------------------

        print("Waiting for SEND completion...")

        deadline = time.time() + 10

        while time.time() < deadline:
            count, wcs = cq.poll(1)

            if count > 0:
                wc = wcs[0]
                print("SEND completion status:", wc.status)

                if wc.status != 0:
                    raise RuntimeError(
                        f"RDMA SEND failed: status={wc.status}"
                    )

                print("Send completed")
                break

            time.sleep(0.001)

        else:
            raise TimeoutError("Timed out waiting for SEND completion")

    finally:
        if sock is not None:
            sock.close()

        try:
            qp.close()
        except Exception:
            pass

        try:
            cq.close()
        except Exception:
            pass

        try:
            pd.close()
        except Exception:
            pass

        try:
            ctx.close()
        except Exception:
            pass

    print()
    print("Client done")


if __name__ == "__main__":
    main()
