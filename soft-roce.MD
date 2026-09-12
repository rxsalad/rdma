# Soft RoCE (RXE)

This guide provides a basic introduction and step-by-step instructions for setting up a Soft-RoCE environment for learning and testing.

```
              Droplet: soft-roce1
              ┌─────────────────┐
              │                 │
Internet ─────│ eth0            │  OOB / control
              │                 │
VPC ──────────│ eth1            │  Ethernet
              │   ↓             │
              │ rxe_eth1        │  Soft-RoCE RDMA
              └─────────────────┘
                       │
                       │ RDMA over Ethernet
                       │
              ┌─────────────────┐
              │ rxe_eth1        │  Soft-RoCE RDMA
              │   ↑             │
VPC ──────────│ eth1            │  Ethernet
              │                 │              
Internet ─────│ eth0            │  OOB / control
              └─────────────────┘
              Droplet: soft-roce2 
```

## Concepts - RDMA, Ethernet, and Soft-RoCE

RDMA and Ethernet serve different purposes and are exposed through separate Linux interfaces and APIs. Ethernet provides the traditional networking stack, including network devices, IP, routing, TCP, and UDP, while RDMA provides a different communication model through the Linux RDMA subsystem and libibverbs, with primitives such as Queue Pairs (QPs), Completion Queues (CQs), Send/Receive, and RDMA Read/Write. RDMA is independent of the underlying network technology and can operate over InfiniBand, RoCE (RDMA over Converged Ethernet), or iWARP.

A single physical NIC can support both conventional Ethernet networking and RDMA. For example, eth1 is the Linux Ethernet netdev used by the normal networking stack, while an RDMA device is exposed separately through the RDMA subsystem and accessed through libibverbs. **With hardware RoCE, these are typically different software representations of the same physical NIC: the Ethernet interface provides the underlying connectivity, while the NIC hardware offloads much of the RDMA and packet processing.**

Soft-RoCE (RXE) provides RDMA over Ethernet entirely in software. It creates an RDMA device such as rxe_eth1 and binds it to an existing Ethernet netdev such as eth1, for example with `rdma link add rxe_eth1 type rxe netdev eth1`. Applications can then use the standard libibverbs API through rxe_eth1, while the actual packets are transmitted through eth1 as Ethernet/RoCE traffic.

The key difference is that Soft-RoCE relies on the CPU for significant portions of RDMA protocol processing, packet processing, encapsulation/decapsulation, and data movement, whereas a hardware RoCE NIC offloads much of this work to dedicated hardware. As a result, Soft-RoCE has higher CPU overhead, latency, and generally much lower performance than hardware RoCE. It is therefore primarily useful for learning, development, and functional testing, or when hardware RDMA is unavailable, rather than for production high-performance workloads.


## Environment Setup

Provision two droplets in the same VPC. Each droplet has two Ethernet interfaces:

- eth0 — Internet: Used for out-of-band (OOB) communication and RDMA control-plane traffic.
- eth1 — VPC: Used for RDMA data-plane traffic via Soft-RoCE.

``` bash
# doctl compute size list
# doctl compute ssh-key list
# doctl vpcs list

# doctl compute image list --public | grep ubuntu | grep base | grep 24
235153036    24.04 (LTS) x64                                                                                                   base           Ubuntu          ubuntu-24-04-x64                                    true      7           2026-07-01T16:38:32Z

# doctl compute droplet create soft-roce1 --image 235153036 --region atl1 --size s-4vcpu-8gb-80gb-amd-v5 --enable-monitoring --ssh-keys 55531023
# doctl compute droplet create soft-roce2 --image 235153036 --region atl1 --size s-4vcpu-8gb-80gb-amd-v5 --enable-monitoring --ssh-keys 55531023

# doctl compute droplet list | grep roce
ID           Name                                                      Public IPv4        Private IPv4    Public IPv6                Memory     VCPUs    Disk    Region    Image                                    VPC UUID         Status    Tags                                                                                   Features                                       Volumes
599758356    soft-roce1                                                165.245.135.10     10.128.16.11                               8192       4        80      atl1      Ubuntu 24.04 (LTS) x64                   0024df6a-c379-4221-b290-c332115a834b    active                                                                                           monitoring,droplet_agent,private_networking    
599758386    soft-roce2                                                129.212.187.224    10.128.16.13                               8192       4        80      atl1      Ubuntu 24.04 (LTS) x64                   0024df6a-c379-4221-b290-c332115a834b    active    
```

Check the network connectivity between the two droplets.

| Droplet         | soft-roce1      | soft-roce2       |
|-----------------|-----------------|------------------|
| eth0 - OOB      | 165.245.135.10  | 129.212.187.224  |
| eth1 - RoCE/RXE | 10.128.16.11    | 10.128.16.13     |

``` bash
ping -c 1 165.245.135.10
ping -c 1 10.128.16.11
ping -c 1 129.212.187.224
ping -c 1 10.128.16.13
```

## Soft-RoCE Setup

Configure Soft-RoCE on eth1 of soft-roce1, allowing the VPC Ethernet interface to serve as the RDMA data-plane interface.

``` bash
root@soft-roce1:~# apt update

root@soft-roce1:~# uname -r
6.8.0-124-generic

# Install extra kernel modules, including rdma_rxe (Soft-RoCE kernel module)
root@soft-roce1:~# apt install linux-modules-extra-$(uname -r)

# Load rdma_rxe, which implements Soft-RoCE / RXE, meaning RDMA over ordinary Ethernet in software rather than using a physical RoCE-capable NIC. 
root@soft-roce1:~# modprobe rdma_rxe
root@soft-roce1:~# lsmod | grep rxe
rdma_rxe              192512  0
ib_uverbs             184320  1 rdma_rxe
ip6_udp_tunnel         16384  1 rdma_rxe
udp_tunnel             32768  1 rdma_rxe
ib_core               507904  2 rdma_rxe,ib_uverbs

# Load rdma_rxe automatically at boot
root@soft-roce1:~# echo rdma_rxe | sudo tee /etc/modules-load.d/rdma_rxe.conf
root@soft-roce1:~# cat /etc/modules-load.d/rdma_rxe.conf

# Installs the userspace RDMA diagnostic tools, including ibv_devices and ibv_devinfo, etc.
root@soft-roce1:~# dpkg -l | grep ibverbs-utils
root@soft-roce1:~# apt install ibverbs-utils

# Create an RXE/Soft-RoCE RDMA device named rxe_eth1 and bind it to Ethernet interface eth1.
root@soft-roce1:~# rdma link add rxe_eth1 type rxe netdev eth1

# Create a systemd service
root@soft-roce1:~# tee /etc/systemd/system/rxe-links.service > /dev/null <<'EOF'
[Unit]
Description=Create Soft-RoCE RDMA links
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/rdma link add rxe_eth1 type rxe netdev eth1
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

# Reload systemd and enable the service
root@soft-roce1:~# systemctl daemon-reload 
root@soft-roce1:~# systemctl enable rxe-links.service 

# Start it immediately without rebooting (not required here)
root@soft-roce1:~# systemctl start rxe-links.service

root@soft-roce1:~# ip -br a
lo               UNKNOWN        127.0.0.1/8 ::1/128 
eth0             UP             165.245.135.10/20 10.50.0.16/16 fe80::446a:fcff:fe79:4f7/64 
eth1             UP             10.128.16.11/20 fe80::10e9:88ff:fe4a:347f/64 

# Check the RDMA link
root@soft-roce1:~# rdma link
link rxe_eth1/1 state ACTIVE physical_state LINK_UP netdev eth1 

# Check the kernel RDMA device
root@soft-roce1:~# ls /sys/class/infiniband/
rxe_eth1

# Check the userspace RDMA device
root@soft-roce1:~# ls /dev/infiniband/
by-ibdev  uverbs0

# Check RDMA devices with ibv_devices
root@soft-roce1:~# ibv_devices
    device          	   node GUID
    ------          	----------------
    rxe_eth1        	10e988fffe4a347f

# Check GIDs
root@soft-roce1:~# ibv_devinfo -v | grep GID
			GID[  0]:		fe80::10e9:88ff:fe4a:347f, RoCE v2
			GID[  1]:		::ffff:10.128.16.11, RoCE v2
```

Apply the same Soft-RoCE configuration to soft-roce2.

``` bash
...

root@soft-roce2:~# ip -br a
lo               UNKNOWN        127.0.0.1/8 ::1/128 
eth0             UP             129.212.187.224/20 10.50.0.17/16 fe80::e864:cdff:fe82:502d/64 
eth1             UP             10.128.16.13/20 fe80::f1:81ff:fe30:b180/64 

root@soft-roce2:~# rdma link
link rxe_eth1/1 state ACTIVE physical_state LINK_UP netdev eth1 

root@soft-roce2:~# ls /sys/class/infiniband/
rxe_eth1

root@soft-roce2:~# ls /dev/infiniband/
by-ibdev  uverbs0

root@soft-roce2:~# ibv_devices
    device          	   node GUID
    ------          	----------------
    rxe_eth1        	00f181fffe30b180

root@soft-roce2:~# ibv_devinfo -v | grep GID
			GID[  0]:		fe80::f1:81ff:fe30:b180, RoCE v2
			GID[  1]:		::ffff:10.128.16.13, RoCE v2
```

## perftest

Install perftest on both droplets:

```
root@soft-roce1:~# apt install perftest

root@soft-roce2:~# apt install perftest
```

Before RDMA data transfer begins, the local and remote endpoints need to exchange connection information, such as QP numbers, PSNs, GIDs, keys, and other parameters required to establish the RDMA connection.

This information can be exchanged using:

- TCP/IP sockets — the traditional perftest connection-exchange mechanism (used in this test).

- RDMA CM (Connection Manager) — which uses IP networking for connection management and can use UDP/TCP depending on the specific CM operation and implementation.

The connection-exchange channel is used only for establishing/configuring the RDMA connection; it is separate from the actual RDMA data path.

### Test: TCP/IP connection exchange over eth0, RDMA data over rxe_eth1/eth1

```
# soft-roce1
root@soft-roce1:~# ib_send_bw -d rxe_eth1 129.212.187.224 (soft-roce2: eth0 ip)

---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF		Device         : rxe_eth1
 Number of qps   : 1		Transport type : IB
 Connection type : RC		Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 TX depth        : 128
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs	 : OFF
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x0015 PSN 0x4d9a0f
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:128:16:11      (soft-roce1: eth1 ip)
 remote address: LID 0000 QPN 0x0014 PSN 0xabb58c
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:128:16:13      (soft-roce2: eth1 ip)
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
 65536      1000             195.56             188.62 		         0.003018
---------------------------------------------------------------------------------------

# soft-roce2
root@soft-roce2:~# ib_send_bw -d rxe_eth1

WARNING: BW peak won't be measured in this run.
************************************
* Waiting for client to connect... *
************************************
---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF		Device         : rxe_eth1
 Number of qps   : 1		Transport type : IB
 Connection type : RC		Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 RX depth        : 512
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs	 : OFF
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x0014 PSN 0xabb58c
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:128:16:13      (soft-roce2: eth1 ip)
 remote address: LID 0000 QPN 0x0015 PSN 0x4d9a0f
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:128:16:11      (soft-roce1: eth1 ip)
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
 65536      1000             0.00               189.91 		         0.003039
---------------------------------------------------------------------------------------
```

