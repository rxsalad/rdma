"""
client_cpu.py —— MTE 的 client（发送方）（主动发起 RDMA 写的一方）：向 server 注册的 CPU 内存写一块数据。

【角色】
  client 是"发起操作"的一方：先经 OOB（带外）TCP 向 server 要来"内存信息"（会话 ID、地址、长度），
  再用 transfer_sync_write 把本端的 1 MiB 数据经 RDMA 直接写进 server 的内存。
  这是单边操作：数据由两端网卡搬运，server 的 CPU 不参与。

【整体流程】
  ① 经 OOB 连到 server，取到它的会话 ID、内存地址、长度（这条 OOB 连接一直保持到传输完成）
  ② 创建并初始化 MTE 引擎
  ③ 准备本端 1 MiB 的内存、填成 7，向 MTE 注册
  ④ transfer_sync_write：RDMA 写，返回时数据已经写完
  ⑤ 经 OOB 发 "done" 通知 server 去校验；注销内存、退出

【用法】（server-0，server 已启动之后）
  PYTHONPATH=/vllm-workspace/mte13 python3 -u client_cpu.py \\
      --host 10.133.1.12 --dev mlx5_0 --peer 10.133.0.253

  --host  本机 eth0 的 IP（MTE 的 local_hostname，也是本端 segment 的名字）
  --peer  server 的 eth0 IP（OOB 在这里取内存信息）
  --dev   本机 RDMA 设备名；留空则自动发现

【为什么用 python3 而不是 python3.10】
  MTE 的包装在 Python 3.12 下，见 mte.md §2。
"""
import argparse
import ctypes          # 用来分配一块"普通进程内存"，并取得它的地址
import json
import socket

from mooncake.engine import TransferEngine     # MTE 的 Python 接口

METADATA_SERVER = "P2PHANDSHAKE"      # 元数据服务：点对点握手，不需要独立服务（两端必须一致）
BUFFER_SIZE = 1024 * 1024             # 发送缓冲 1 MiB
FILL_BYTE = 7                         # 把发送缓冲整块填成这个值，便于 server 校验


def recv_json_line(sock):
    """从 OOB 连接读一行 JSON（以换行结尾）。
    TCP 是字节流，不保证一次 recv 就收到完整消息，所以循环收到出现换行为止。"""
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:                  # 对端关闭
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))


def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="本机 eth0 的 IP")
    ap.add_argument("--peer", required=True, help="server 的 eth0 IP")
    ap.add_argument("--dev", default="", help="RDMA 设备名，如 mlx5_0；空 = 自动发现")
    ap.add_argument("--protocol", default="rdma", help="rdma 或 tcp")
    ap.add_argument("--oob-port", type=int, default=5555)
    args = ap.parse_args()

    # ========== 步骤 1：经 OOB 取 server 的内存信息 ==========
    # 对端内存的地址不会自动出现在 client 这边，要走一条"带外"通道（普通 TCP，走 eth0）拿到。
    # 注意：这条连接要一直保持到传输完成——传完后还要靠它给 server 发 "done"。
    # （原版在这里用了 with，拿到信息就关闭了连接；这里改成保持打开。）
    sock = socket.create_connection((args.peer, args.oob_port))
    try:
        buffer_info = recv_json_line(sock)
        server_session_id = buffer_info["session_id"]     # 目标会话 ID（server 的 "host:port"）
        server_ptr = buffer_info["ptr"]                   # 对端内存的虚拟地址
        server_len = buffer_info["len"]                   # 对端内存的长度
        print(f"Server session ID: {server_session_id}")
        print(f"Server buffer address: {server_ptr}, length: {server_len}")

        # ========== 步骤 2：创建并初始化引擎 ==========
        # 参数含义见 server_cpu.py。这一步内部会枚举网卡、拓扑发现（本环境约 4.5 秒）、自动选 GID、
        # 并在一个随机端口上监听握手。返回 0 表示成功。
        engine = TransferEngine()
        ret = engine.initialize(args.host, METADATA_SERVER, args.protocol, args.dev)
        if ret != 0:
            raise RuntimeError(f"engine.initialize 失败，返回 {ret}")
        session_id = f"{args.host}:{engine.get_rpc_port()}"     # 本端的会话 ID（仅用于打印）

        # ========== 步骤 3：准备并注册本端内存 ==========
        client_buffer = (ctypes.c_uint8 * BUFFER_SIZE)()
        client_ptr = ctypes.addressof(client_buffer)            # 本端内存的虚拟地址
        client_len = ctypes.sizeof(client_buffer)
        ctypes.memset(client_ptr, FILL_BYTE, client_len)        # 整块填成 7，作为要写出去的数据

        # 发送端的本地缓冲也要注册（网卡要 DMA 读它，对应 verbs 里的 lkey）。
        ret = engine.register_memory(client_ptr, client_len)
        if ret != 0:
            raise RuntimeError("Mooncake memory registration failed.")

        try:
            # ========== 步骤 4：RDMA 写 ==========
            print(f"Client session ID: {session_id}")
            print("Transferring data to server...", flush=True)
            # transfer_sync_write(目标会话 ID, 本端地址, 对端地址, 长度)：
            #   单边写：把本端 [client_ptr, +长度) 写到对端 [server_ptr, +长度)。
            #   同步版：函数返回时数据已写完；返回 0 成功，负数失败。
            #   第一次向某个对端传输时，MTE 内部会先 openSegment（按目标名向对端取元数据）并按需建立
            #   RDMA 连接；选哪块网卡、要不要切片，也由 MTE 决定，调用方不用管。
            #   对端地址必须落在对端已注册的范围内，所以这里取两边长度的较小值。
            ret = engine.transfer_sync_write(
                server_session_id,
                client_ptr,
                server_ptr,
                min(client_len, server_len),
            )
            if ret < 0:
                raise RuntimeError(f"Transfer failed, ret={ret}")
            print("Transfer successful.", flush=True)

            # ========== 步骤 5：通知 server ==========
            # RDMA 写对 server 是"无声"的，它自己不知道数据已到，所以经 OOB 发 "done" 让它去校验。
            # （原版在这里 sleep(10) 等一等；同步写返回时数据已经写完，用不着等。）
            sock.sendall(b"done\n")
        finally:
            # 注销本端内存（对应 ibv_dereg_mr）。放在 finally 里，出错时也会执行。
            ret = engine.unregister_memory(client_ptr)
            if ret != 0:
                raise RuntimeError("Mooncake memory deregistration failed.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
