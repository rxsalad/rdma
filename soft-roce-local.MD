# Soft RoCE (RXE)

## Envrionment

| Droplet         | sr1             | sr2              |
|-----------------|-----------------|------------------|
| eth0 - OOB      | 192.168.68.191  | 192.168.68.192   |
| eth1 - RoCE/RXE | 10.0.1.1        | 10.0.1.2         |
| eth1 - RoCE/RXE | 10.0.2.1        | 10.0.2.2         |

``` bash
ping -c 1 192.168.68.191
ping -c 1 192.168.68.192
ping -c 1 10.0.1.1 
ping -c 1 10.0.1.2
ping -c 1 10.0.2.1
ping -c 1 10.0.2.2 
```



``` bash
# dpkg -l | grep libibverbs-dev
# dpkg -l | grep  librdmacm-dev
# apt install libibverbs-dev librdmacm-dev

# cat /usr/include/infiniband/verbs.h
# cat /usr/include/rdma/rdma_cma.h

# apt install make
# apt install build-essential

# gcc --version
gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
Copyright (C) 2023 Free Software Foundation, Inc.
This is free software; see the source for copying conditions.  There is NO
warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

# make --version
GNU Make 4.3
Built for x86_64-pc-linux-gnu
Copyright (C) 1988-2020 Free Software Foundation, Inc.
License GPLv3+: GNU GPL version 3 or later <http://gnu.org/licenses/gpl.html>
This is free software: you are free to change and redistribute it.
There is NO WARRANTY, to the extent permitted by law.
``` 

## Test: OOB

```
# sr1

ib_send_bw -d rxe_eth1 192.168.68.192

---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF          Device         : rxe_eth1
 Number of qps   : 1            Transport type : IB
 Connection type : RC           Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 TX depth        : 128
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs     : OFF
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x003b PSN 0x20bad3
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:01
 remote address: LID 0000 QPN 0x003b PSN 0xcb9fde
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:02
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
 65536      1000             319.41             285.60             0.004570
---------------------------------------------------------------------------------------

# sr1

ib_send_bw -d rxe_eth1

WARNING: BW peak won't be measured in this run.
************************************
* Waiting for client to connect... *
************************************
---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF          Device         : rxe_eth1
 Number of qps   : 1            Transport type : IB
 Connection type : RC           Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 RX depth        : 512
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs     : OFF
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x003b PSN 0xcb9fde
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:02
 remote address: LID 0000 QPN 0x003b PSN 0x20bad3
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:01
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
Conflicting CPU frequency values detected: 3806.344000 != 4245.963000. CPU Frequency is not max.
 65536      1000             0.00               286.29             0.004581
---------------------------------------------------------------------------------------
```

## Test: RDMA CM，over RDMA NICs

```
# sr1

ib_send_bw -d rxe_eth1 -R 10.0.1.2

---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF          Device         : rxe_eth1
 Number of qps   : 1            Transport type : IB
 Connection type : RC           Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 TX depth        : 128
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs     : ON
 Data ex. method : rdma_cm
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x003d PSN 0xa5167e
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:01
 remote address: LID 0000 QPN 0x003d PSN 0x710af6
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:02
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
Conflicting CPU frequency values detected: 4443.399000 != 3962.266000. CPU Frequency is not max.
 65536      1000             294.87             294.87             0.004718
---------------------------------------------------------------------------------------

# sr2

ib_send_bw -d rxe_eth1 -R
WARNING: BW peak won't be measured in this run.

************************************
* Waiting for client to connect... *
************************************
---------------------------------------------------------------------------------------
                    Send BW Test
 Dual-port       : OFF          Device         : rxe_eth1
 Number of qps   : 1            Transport type : IB
 Connection type : RC           Using SRQ      : OFF
 PCIe relax order: ON
 ibv_wr* API     : OFF
 RX depth        : 512
 CQ Moderation   : 1
 Mtu             : 1024[B]
 Link type       : Ethernet
 GID index       : 1
 Max inline data : 0[B]
 rdma_cm QPs     : ON
 Data ex. method : rdma_cm
---------------------------------------------------------------------------------------
 Waiting for client rdma_cm QP to connect
 Please run the same command with the IB/RoCE interface IP
---------------------------------------------------------------------------------------
 local address: LID 0000 QPN 0x003d PSN 0x710af6
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:02
 remote address: LID 0000 QPN 0x003d PSN 0xa5167e
 GID: 00:00:00:00:00:00:00:00:00:00:255:255:10:00:01:01
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[MB/sec]    BW average[MB/sec]   MsgRate[Mpps]
Conflicting CPU frequency values detected: 3847.937000 != 4507.890000. CPU Frequency is not max.
 65536      1000             0.00               295.54             0.004729
---------------------------------------------------------------------------------------
```

## Test: C, RDMA CM

```
# sr1

git clone https://github.com/SYaoJun/rdma-example.git
cd ~/work/rdma-example/src/
make clean 
make

./client -a 10.0.1.2 -s textstring
Passed string is : textstring , with count 10 
Trying to connect to server at : 10.0.1.2 port: 20886 
The client is connected successfully 
---------------------------------------------------------
buffer attr, addr: 0x60d36ef6ac30 , len: 10 , stag : 0x3cf3 
---------------------------------------------------------
...
SUCCESS, source and destination buffers match 
Client resource clean up is complete 


# sr2

git clone https://github.com/SYaoJun/rdma-example.git
cd ~/work/rdma-example/src/
make clean 
make

./server -a 10.0.1.2
Server is listening successfully at: 10.0.1.2 , port: 20886 
A new connection is accepted from 10.0.1.1 
Client side buffer information is received...
---------------------------------------------------------
buffer attr, addr: 0x5ffe8b2572e0 , len: 10 , stag : 0x3f96 
---------------------------------------------------------
The client has requested buffer length of : 10 bytes 
A disconnect event is received from the client...
Server shut-down is complete 
```

## Test: Python, RDMA CM

```
# sr1

cd ~/work/pyverbs-example/

python3 client_cm.py 
Connecting to 10.0.1.2:18515...
RDMA connection established
RDMA device: rxe_eth1
RDMA port: 1
QPN: 66
MR buffer address: 0x22e5f000
MR lkey: 16897
Message: b'Hello from pyverbs client!'
Sending...
Send completed
Client done

# sr2

cd ~/work/pyverbs-example/

python3 server_cm.py 
Client connected
RDMA device: rxe_eth1
RDMA port: 1
QPN: 65
RDMA connection accepted
MR buffer address: 0x1fcfa000
MR lkey: 15876
Receive WR posted
Waiting for RDMA SEND...
Receive completion status: 0
Received 26 bytes
Message: Hello from pyverbs client!
Server done
```

## Test: Python, OOB

```
# sr1

cd ~/work/pyverbs-example/

python3 client_oob.py 
========================================
Pyverbs OOB RDMA Client
========================================
OOB server : 192.168.68.192
OOB port   : 18515
RDMA device: rxe_eth1
RDMA port  : 1
GID index  : 1
========================================

Opening RDMA device: rxe_eth1
RDMA port state: 4
RDMA physical state: 5
RDMA link layer: 2
RDMA GID table length: 1024
Local GID: 0000:0000:0000:0000:0000:ffff:0a00:0101
QP created
QPN: 67
Local PSN: 15026837
MR buffer address: 0x16c4000
MR lkey: 17277

Connecting to OOB server 192.168.68.192:18515...
OOB TCP connection established

Sending local RDMA information:
{'qp_num': 67, 'psn': 15026837, 'gid_index': 1, 'gid': '0000:0000:0000:0000:0000:ffff:0a00:0101'}

Received remote RDMA information:
{'qp_num': 66, 'psn': 270697, 'gid_index': 1, 'gid': '0000:0000:0000:0000:0000:ffff:0a00:0102'}

Remote QPN: 66
Remote PSN: 270697
Remote GID: 0000:0000:0000:0000:0000:ffff:0a00:0102
Remote GID object: 0000:0000:0000:0000:0000:ffff:0a00:0102

Moving QP to RTR...
QP state: RTR
Moving QP to RTS...
QP state: RTS

RDMA QP connection established

Message: b'Hello from pyverbs OOB client!'
Posting RDMA SEND...
SEND posted
Waiting for SEND completion...
SEND completion status: 0
Send completed

Client done

# sr2

cd ~/work/pyverbs-example/

python3 server_oob.py 
========================================
Pyverbs OOB RDMA Server
========================================
OOB bind  : 192.168.68.192
OOB port  : 18515
RDMA device: rxe_eth1
RDMA port : 1
GID index : 1
========================================

Opening RDMA device: rxe_eth1
RDMA port state: 4
RDMA physical state: 5
RDMA link layer: 2
RDMA GID table length: 1024
Local GID: 0000:0000:0000:0000:0000:ffff:0a00:0102
QP created
QPN: 66
Local PSN: 270697
MR buffer address: 0x3de12000
MR lkey: 16341

Listening for OOB connection on 192.168.68.192:18515...
OOB connection from: ('192.168.68.191', 40358)

Received remote RDMA information:
{'qp_num': 67, 'psn': 15026837, 'gid_index': 1, 'gid': '0000:0000:0000:0000:0000:ffff:0a00:0101'}

Remote QPN: 67
Remote PSN: 15026837
Remote GID: 0000:0000:0000:0000:0000:ffff:0a00:0101

Sending local RDMA information:
{'qp_num': 66, 'psn': 270697, 'gid_index': 1, 'gid': '0000:0000:0000:0000:0000:ffff:0a00:0102'}

Remote GID object: 0000:0000:0000:0000:0000:ffff:0a00:0101

Moving QP to RTR...
QP state: RTR
Moving QP to RTS...
QP state: RTS

RDMA QP connection established

Posting receive WR...
Receive WR posted

Waiting for RDMA SEND...
Receive completion status: 0
Received bytes: 30
Message: Hello from pyverbs OOB client!

Server done
```
