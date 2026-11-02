"""
server_cpu.py —— MTE 的 server（接收方）（被写入的一方）：注册一块 CPU 内存，等 client 经 RDMA 直接写进来。

【角色】
  server 是"被访问"的一方。client 对它的内存做 RDMA WRITE 时，server 的 CPU 不参与数据搬运，
  数据由两端网卡直接搬运（单边操作）。server 只做三件事：注册内存、把内存信息告诉 client、
  等 client 说"写完了"之后校验。

【整体流程】
  ① 创建并初始化 MTE 引擎（选网卡、发现拓扑、开始监听握手端口）
  ② 准备一块 1 MiB 的内存，向 MTE 注册（之后对端才能远程写它）
  ③ 在 OOB（带外）TCP 端口上等 client 来取"内存信息"（会话 ID、地址、长度）
  ④ 等 client 经 OOB 发来 "done"（RDMA WRITE 对 server 是"无声"的，它自己不知道数据到了）
  ⑤ 校验整块内存、注销内存、退出

【用法】（server-1，先启动）
  PYTHONPATH=/vllm-workspace/mte13 python3 -u server_cpu.py --host 10.133.0.253 --dev mlx5_14

  --host  本机 eth0 的 IP。它既是 MTE 的 local_hostname（也是本端 segment 的名字），
          又是对端做 TCP 握手时连的地址，所以必须是对端能连上的地址
  --dev   RDMA 设备名（逗号分隔）；留空则让 MTE 自动发现所有网卡

【为什么用 python3 而不是 python3.10】
  MTE 的包（mooncake-transfer-engine-cuda13）装在 Python 3.12 下，见 mte.md §2。
"""
import argparse
import ctypes          # 用来分配一块"普通进程内存"，并取得它的地址
import json
import socket

from mooncake.engine import TransferEngine     # MTE 的 Python 接口

METADATA_SERVER = "P2PHANDSHAKE"      # 元数据服务：这个特殊值 = 不用 etcd/redis/http，两端点对点握手
BUFFER_SIZE = 1024 * 1024             # 接收缓冲 1 MiB
EXPECT_BYTE = 7                       # client 往里写的字节值；用来校验


def main():
    # ---------- 命令行参数 ----------
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="本机 eth0 的 IP")
    ap.add_argument("--dev", default="", help="RDMA 设备名，如 mlx5_14；空 = 自动发现")
    ap.add_argument("--protocol", default="rdma", help="rdma 或 tcp")
    ap.add_argument("--oob-port", type=int, default=5555, help="OOB 端口：把 buffer 信息交给 client")
    args = ap.parse_args()

    # ========== 步骤 1：创建并初始化引擎 ==========
    # initialize(local_hostname, metadata_server, protocol, device_name)：
    #   local_hostname   本端的名字和握手地址；本端的 segment 就用它命名（要求全局唯一）
    #   metadata_server  元数据服务；P2PHANDSHAKE = 点对点握手，不需要独立服务
    #   protocol         "rdma"（RoCE/IB）或 "tcp"
    #   device_name      用哪些 RDMA 网卡；空串 = 自动发现
    # 这一步内部会：枚举网卡、做拓扑发现（本环境约 4.5 秒）、自动选 GID、在一个随机端口上监听握手。
    # 返回 0 表示成功，所以要检查（原版没检查）。
    engine = TransferEngine()
    ret = engine.initialize(args.host, METADATA_SERVER, args.protocol, args.dev)
    if ret != 0:
        raise RuntimeError(f"engine.initialize 失败，返回 {ret}")
    # 对端要用"会话 ID"来指定"我要写给谁"。P2P 握手模式下端口是随机的，
    # 要用 get_rpc_port() 取回真正的端口，拼成 "host:port"。
    session_id = f"{args.host}:{engine.get_rpc_port()}"

    # ========== 步骤 2：准备并注册内存 ==========
    # 这里是一块普通的进程内存（不是 GPU 显存）。ctypes 数组初始为全 0。
    server_buffer = (ctypes.c_uint8 * BUFFER_SIZE)()
    server_ptr = ctypes.addressof(server_buffer)      # 这块内存的虚拟地址（整数）
    server_len = ctypes.sizeof(server_buffer)

    # register_memory(地址, 长度)：把内存注册给 MTE，让网卡能直接 DMA  访问它（会钉住这些页）。
    # 相当于 verbs 里的 ibv_reg_mr；之后对端的读写请求必须落在这块已注册的范围内。
    # 返回 0 表示成功。
    ret = engine.register_memory(server_ptr, server_len)
    if ret != 0:
        raise RuntimeError("Mooncake memory registration failed.")

    print(f"Server session ID: {session_id}")
    print(f"Server buffer address: {server_ptr}, length: {server_len}")
    print(f"写入前第一个字节：{server_buffer[0]}")       # 应当是 0

    listener = None
    try:
        # ========== 步骤 3：OOB 监听，把"内存信息"交给 client ==========
        # client 要对我们做 RDMA 写，必须事先知道三样东西：目标会话 ID、内存地址、长度。
        # 这些信息要走一条"带外"（out-of-band）通道，这里用普通 TCP（走 eth0，不是 RDMA）。
        # 注意：和 pyverbs 的 OOB 版不同，这里不需要传 qpn / gid / rkey——MTE 在内部自己处理。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", args.oob_port))
        listener.listen(1)
        print(f"Waiting for client to connect on port {args.oob_port}...", flush=True)
        conn, peer = listener.accept()
        with conn:
            # 用 JSON + 换行 发给 client（client 按换行切分）
            payload = {"session_id": session_id, "ptr": server_ptr, "len": server_len}
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            print(f"Buffer information sent to client {peer}.", flush=True)

            # ========== 步骤 4：等 client 说"写完了" ==========
            # RDMA WRITE 是单边操作：数据悄悄写进了我们的内存，我们的 CPU 和程序都不知道。
            # 所以 client 写完后，要用 OOB 发一个 "done" 通知我们去看。
            # （原版用 input() 等人按回车；kubectl exec 没有终端，input() 会失败，所以改成等 OOB 消息。）
            # 注意：这条 OOB 连接在整个传输期间必须一直保持打开。
            conn.settimeout(120)
            data = conn.recv(16)
            if not data.startswith(b"done"):
                raise RuntimeError(f"没有收到 client 的完成通知：{data!r}")

        # ========== 步骤 5：校验 ==========
        # client 把整块 1 MiB 都填成了 EXPECT_BYTE（7）。逐字节检查，统计不等于 7 的个数。
        # 这里直接读自己的内存——数据是网卡写进来的，不需要任何"接收"调用。
        raw = bytes(server_buffer)
        bad = sum(1 for b in raw if b != EXPECT_BYTE)
        print(f"First byte in server buffer: {server_buffer[0]}")
        print(f"校验：{server_len} 字节中，值不是 {EXPECT_BYTE} 的有 {bad} 个 -> {'通过' if bad == 0 else '失败'}")
    finally:
        # ========== 清理 ==========
        # 注销内存（对应 ibv_dereg_mr）。放在 finally 里，出错时也会执行。
        ret = engine.unregister_memory(server_ptr)
        if ret != 0:
            raise RuntimeError("Mooncake memory deregistration failed.")
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    main()
