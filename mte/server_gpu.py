"""
server_gpu.py —— MTE 的 server（接收方）（GPU 显存版）：在 GPU 上分配一块显存，注册给 MTE，
等 client 经 RDMA 把数据直接写进这块显存（GPUDirect RDMA），然后在 GPU 上校验。

与 server_cpu.py（CPU 内存版）的区别：
  - 内存用 PyTorch 在 GPU 上分配（torch.zeros(..., device="cuda:N")），拿设备指针 data_ptr()；
  - register_memory 注册的是显存地址；
  - 校验在 GPU 上完成（torch 张量比较），数据全程不经过 CPU 内存。

【整体流程】
  ① 初始化 MTE 引擎（--dev 选与该 GPU 在同一个 PCIe 交换机下的网卡）
  ② 在 GPU 上分配显存并清零，向 MTE 注册
  ③ 经 OOB（TCP，走 eth0）把"会话 ID、显存地址、长度"交给 client
  ④ 等 client 的 "done"，在 GPU 上校验，注销内存

【用法】（server-1，先启动）
  PYTHONPATH=/vllm-workspace/mte13 python3 -u server_gpu.py \\
      --host 10.133.0.253 --dev mlx5_14 --gpu 7

【GPU 与网卡要就近】
  本集群每块 GPU 与两块网卡在同一个 PCIe 交换机下（nvidia-smi topo -m 里显示 PXB）：
  GPU0 ↔ mlx5_0/1，GPU1 ↔ mlx5_2/3，……，GPU7 ↔ mlx5_14/15。Pod 里只有偶数号网卡，
  所以 GPU N 配 mlx5_(2N)：GPU7 配 mlx5_14，GPU0 配 mlx5_0。
  （"mlx5_N 对应 nvidia-smi 里的 NICN"是按顺序对应的推断。）

【Python】用 python3（3.12）：torch 和 MTE 都装在 3.12 下，见 mte.md §2。
"""
import argparse
import json
import socket

import torch                                   # 用它在 GPU 上分配显存、做校验
from mooncake.engine import TransferEngine     # MTE 的 Python 接口

METADATA_SERVER = "P2PHANDSHAKE"      # 点对点握手，不需要独立的元数据服务
MOD = 251                             # client 写入的模式：第 i 字节 = i mod 251，必须与 client 一致


def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="本机 eth0 的 IP（MTE 的 local_hostname）")
    ap.add_argument("--dev", required=True, help="RDMA 设备名，应与 --gpu 在同一个 PCIe 交换机下")
    ap.add_argument("--gpu", type=int, default=0, help="使用哪块 GPU")
    ap.add_argument("--size-mib", type=int, default=64, help="显存缓冲大小（MiB）")
    ap.add_argument("--protocol", default="rdma", help="rdma 或 tcp")
    ap.add_argument("--oob-port", type=int, default=5555)
    args = ap.parse_args()

    size = args.size_mib * 1024 * 1024

    # ========== 步骤 1：选定 GPU，初始化引擎 ==========
    # 先 set_device，让后面所有 CUDA 操作（包括 MTE 内部判断指针位置）都落在这块 GPU 上。
    torch.cuda.set_device(args.gpu)
    engine = TransferEngine()
    ret = engine.initialize(args.host, METADATA_SERVER, args.protocol, args.dev)
    if ret != 0:
        raise RuntimeError(f"engine.initialize 失败，返回 {ret}")
    session_id = f"{args.host}:{engine.get_rpc_port()}"      # 对端用它指定"写给谁"

    # ========== 步骤 2：在 GPU 上分配显存并注册 ==========
    # torch.zeros(..., device="cuda:N") 在第 N 块 GPU 的显存里分配 size 字节并清零。
    # data_ptr() 是这块显存的设备地址（整数）——不是主机内存地址。
    buf = torch.zeros(size, dtype=torch.uint8, device=f"cuda:{args.gpu}")
    torch.cuda.synchronize()                                  # 确保清零已经真正完成
    ptr = buf.data_ptr()

    # register_memory：把这块显存注册给 MTE / 网卡（GPUDirect RDMA：网卡直接 DMA 访问显存）。
    # 能成功的前提是宿主机加载了 nvidia_peermem（或走 dmabuf），本集群的 Pod 里能看到 nvidia_peermem。
    ret = engine.register_memory(ptr, size)
    if ret != 0:
        raise RuntimeError(f"显存注册失败，返回 {ret}")

    print(f"Server session ID: {session_id}")
    print(f"Server GPU buffer: cuda:{args.gpu}, device address={hex(ptr)}, length={size}")
    print(f"写入前：显存中值不是 0 的字节数 = {int((buf != 0).sum().item())}")    # 应当是 0

    listener = None
    try:
        # ========== 步骤 3：OOB 监听，把"显存信息"交给 client ==========
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", args.oob_port))
        listener.listen(1)
        print(f"Waiting for client to connect on port {args.oob_port}...", flush=True)
        conn, peer = listener.accept()
        with conn:
            payload = {"session_id": session_id, "ptr": ptr, "len": size}
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            print(f"Buffer information sent to client {peer}.", flush=True)

            # ========== 步骤 4：等 client 说"写完了" ==========
            # RDMA 写对 server 是"无声"的，所以靠 OOB 的 "done" 通知。
            conn.settimeout(180)
            data = conn.recv(16)
            if not data.startswith(b"done"):
                raise RuntimeError(f"没有收到 client 的完成通知：{data!r}")

        # ========== 步骤 5：在 GPU 上校验 ==========
        # 在 GPU 上重新生成"期望的模式"，与收到的显存逐字节比较（位置相关，能发现错位和只写一部分）。
        # 数据是网卡直接写进显存的，这里不需要任何"接收"调用；先同步一下再读。
        torch.cuda.synchronize()
        expect = (torch.arange(size, device=f"cuda:{args.gpu}") % MOD).to(torch.uint8)
        bad = int((buf != expect).sum().item())                # 在 GPU 上比较，只把一个整数取回 CPU
        print(f"First bytes in GPU buffer: {buf[:8].tolist()}（期望 [0, 1, 2, ..., 7]）")
        print(f"校验：{size} 字节中，与期望模式不一致的有 {bad} 个 -> {'通过' if bad == 0 else '失败'}")
    finally:
        # ========== 清理 ==========
        ret = engine.unregister_memory(ptr)
        if ret != 0:
            raise RuntimeError("Mooncake memory deregistration failed.")
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    main()
