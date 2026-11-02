"""
client_gpu.py —— MTE 的 client（发送方）（GPU 显存版）：把本端 GPU 显存里的数据，经 RDMA 直接写进
对端 GPU 的显存（跨节点 GPU → GPU，GPUDirect RDMA）。

与 client_cpu.py（CPU 内存版）的区别：
  - 发送缓冲在 GPU 上（torch.full(..., device="cuda:N")），用设备指针 data_ptr()；
  - register_memory 注册的是显存地址，网卡直接从显存 DMA 读出数据；
  - 数据从本端 GPU 显存到对端 GPU 显存，不经过任何一端的 CPU 内存。

【整体流程】
  ① 经 OOB 连到 server，取到它的会话 ID、显存地址、长度（连接保持到传输完成）
  ② 选定 GPU，初始化 MTE 引擎（--dev 选与该 GPU 就近的网卡）
  ③ 在 GPU 上分配发送缓冲、填成"位置相关"的模式（第 i 字节 = i mod 251），向 MTE 注册
  ④ transfer_sync_write：RDMA 写，返回时数据已写完
  ⑤ 经 OOB 发 "done"；注销内存、退出

【用法】（server-0，server 已启动之后）
  PYTHONPATH=/vllm-workspace/mte13 python3 -u client_gpu.py \\
      --host 10.133.1.12 --dev mlx5_0 --gpu 0 --peer 10.133.0.253

【GPU 与网卡】GPU0 ↔ mlx5_0，GPU7 ↔ mlx5_14（同一个 PCIe 交换机，见 server_gpu.py 的说明）。

【Python】用 python3（3.12）：torch 和 MTE 都装在 3.12 下，见 mte.md §2。
"""
import argparse
import json
import socket
import time

import torch                                   # 用它在 GPU 上分配显存
from mooncake.engine import TransferEngine     # MTE 的 Python 接口

METADATA_SERVER = "P2PHANDSHAKE"      # 点对点握手；两端必须一致
MOD = 251                             # 发送数据的模式：第 i 字节 = i mod 251（251 是质数，
                                      # 不会与 2 的幂对齐，能发现数据错位、重复或只写了一部分）


def recv_json_line(sock):
    """从 OOB 连接读一行 JSON（以换行结尾）。TCP 是字节流，所以循环收到出现换行为止。"""
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))


def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="本机 eth0 的 IP（MTE 的 local_hostname）")
    ap.add_argument("--peer", required=True, help="server 的 eth0 IP")
    ap.add_argument("--dev", required=True, help="RDMA 设备名，应与 --gpu 在同一个 PCIe 交换机下")
    ap.add_argument("--gpu", type=int, default=0, help="使用哪块 GPU")
    ap.add_argument("--size-mib", type=int, default=64, help="发送缓冲大小（MiB）")
    ap.add_argument("--repeat", type=int, default=1, help="重复传输次数（第 1 次含建连开销）")
    ap.add_argument("--protocol", default="rdma", help="rdma 或 tcp")
    ap.add_argument("--oob-port", type=int, default=5555)
    args = ap.parse_args()

    size = args.size_mib * 1024 * 1024

    # ========== 步骤 1：经 OOB 取 server 的显存信息 ==========
    # 连接要保持到传输完成：传完后还要靠它给 server 发 "done"。
    sock = socket.create_connection((args.peer, args.oob_port))
    try:
        info = recv_json_line(sock)
        server_session_id = info["session_id"]     # 目标会话 ID
        server_ptr = info["ptr"]                   # 对端 GPU 显存的设备地址
        server_len = info["len"]                   # 对端缓冲长度
        print(f"Server session ID: {server_session_id}")
        print(f"Server GPU buffer address: {hex(server_ptr)}, length: {server_len}")

        # ========== 步骤 2：选定 GPU，初始化引擎 ==========
        torch.cuda.set_device(args.gpu)
        engine = TransferEngine()
        ret = engine.initialize(args.host, METADATA_SERVER, args.protocol, args.dev)
        if ret != 0:
            raise RuntimeError(f"engine.initialize 失败，返回 {ret}")
        print(f"Client session ID: {args.host}:{engine.get_rpc_port()}")

        # ========== 步骤 3：在本端 GPU 上分配发送缓冲并注册 ==========
        # 在显存里分配 size 字节，第 i 字节 = i mod 251；data_ptr() 是设备地址。
        # 用位置相关的模式而不是整块同一个值，server 才能发现数据错位或只写了一部分。
        buf = (torch.arange(size, device=f"cuda:{args.gpu}") % MOD).to(torch.uint8)
        torch.cuda.synchronize()                   # 确保填充真的完成，再让网卡去读
        ptr = buf.data_ptr()
        n = min(size, server_len)                  # 对端地址必须落在对端已注册范围内，取较小值

        ret = engine.register_memory(ptr, size)    # 本端显存也要注册（网卡要 DMA 读它）
        if ret != 0:
            raise RuntimeError(f"显存注册失败，返回 {ret}")

        try:
            # ========== 步骤 4：RDMA 写（GPU 显存 → 对端 GPU 显存） ==========
            # 参数与 CPU 版相同：(目标会话 ID, 本端地址, 对端地址, 长度)。
            # 区别只在于两个地址都是显存的设备地址；MTE 据此选就近网卡并做 GPUDirect RDMA。
            for i in range(args.repeat):
                t0 = time.perf_counter()
                ret = engine.transfer_sync_write(server_session_id, ptr, server_ptr, n)
                dt = time.perf_counter() - t0
                if ret < 0:
                    raise RuntimeError(f"Transfer failed, ret={ret}")
                gib_s = n / dt / (1 << 30)
                print(f"第 {i + 1} 次：写入 {n} 字节，耗时 {dt * 1000:.2f} ms（约 {gib_s:.2f} GiB/s，"
                      f"{'含建连开销，' if i == 0 else ''}仅供参考，不是基准测试）", flush=True)
            print("Transfer successful.", flush=True)

            # ========== 步骤 5：通知 server 去校验 ==========
            sock.sendall(b"done\n")
        finally:
            ret = engine.unregister_memory(ptr)
            if ret != 0:
                raise RuntimeError("Mooncake memory deregistration failed.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
