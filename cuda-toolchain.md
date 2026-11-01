# CUDA 工具链：PTX、cubin 与发行/运行机制

> 定位：通用 CUDA 知识手册。主线：**编译 → 架构命名 → 构建期 arch 决定 → 发行打包 → 宿主嵌入 → 运行时加载执行 → 两层 JIT 与缓存**，外加检查工具与实例链路。
> 实测案例：mn-1-dep-0（单节点 8× NVIDIA B300，compute capability 10.3，CUDA 13.0，vLLM 0.32.0，实测于 2026-10-21）；vLLM 运行时编译细节见同目录 `vllm-jit.md`。

## 目录

- [§1 编译全景：从 .cu 到 GPU 执行](#1-编译全景从-cu-到-gpu-执行)
- [§2 设备侧产物：PTX、cubin 与 fatbin](#2-设备侧产物ptxcubin-与-fatbin)
- [§3 架构命名与换算](#3-架构命名与换算)
- [§4 构建期 arch 列表由谁决定：TORCH_CUDA_ARCH_LIST 的三层覆盖](#4-构建期-arch-列表由谁决定torch_cuda_arch_list-的三层覆盖)
- [§5 发行（AOT）：fatbin 里装什么](#5-发行aotfatbin-里装什么)
- [§6 宿主侧：设备代码如何进入宿主二进制](#6-宿主侧设备代码如何进入宿主二进制)
- [§7 运行时：模块加载与四级匹配](#7-运行时模块加载与四级匹配)
- [§8 库级 JIT：运行时从源码编译](#8-库级-jit运行时从源码编译)
- [§9 缓存全景（实测）](#9-缓存全景实测)
- [§10 cuobjdump 实战（实测采样）](#10-cuobjdump-实战实测采样)
- [§11 实例链路](#11-实例链路)
- [§12 相关文档](#12-相关文档)

## 1. 编译全景：从 .cu 到 GPU 执行
[↩ 目录](#目录)

一次 nvcc 编译由三段构成，nvcc 是总指挥：**设备链**（cicc → ptxas）、**宿主链**（gcc/g++，nvcc 自己不编主机代码，`-ccbin` 可替换）、**链接**（普通 g++/ld）。设备链：

```
CUDA C++ (.cu)
   │  nvcc 前端（cudafe++ / cicc）：模板实例化、主机/设备代码分离
   ▼
PTX ─────────── 虚拟 ISA（文本），面向"虚拟架构" compute_XX
   │  ptxas（PTX 汇编器）
   ▼
SASS ────────── 真正跑在 SM 上的机器码（每代架构不同）
   │  装进容器
   ▼
cubin ───────── ELF 容器，装某一个具体架构（sm_XX）的 SASS
   │  多个 cubin（+ 可选 PTX）打包
   ▼
fatbin ─────── 发行容器
```

nvcc 输出模式（决定管线停在哪）：

| 参数 | 产物 |
|---|---|
| `--ptx` | 只到 PTX |
| `--cubin` | 只到 cubin（PTX 是中间产物，不落盘）——DeepGEMM 用的就是这个 |
| `--fatbin` | fatbin 容器 |
| 默认（编译可执行文件） | fatbin 嵌入宿主二进制 |

产物归属：PTX / cubin / fatbin 三种都是**设备侧文件**；默认交付物是**宿主侧二进制**——`.so`（`-shared`）或可执行文件（默认），设备代码以数据段形式内嵌其中（机制见 §6）。只有显式选 `--ptx / --cubin / --fatbin` 才只出设备侧文件。

相关工具：`ptxas`（PTX→SASS）、`cuobjdump`（检查产物，§10）、`nvdisasm`（反汇编 SASS）、NVRTC（进程内运行时编译 C++）。

## 2. 设备侧产物：PTX、cubin 与 fatbin
[↩ 目录](#目录)

三种产物、两组对照：PTX 与 cubin 是**两种代码形态**的对照（虚拟 ISA vs 机器码）；cubin 与 fatbin 是**两层容器**的对照（执行单元 vs 发行包装）——回答不同问题，别混为一谈。

### 2.1 PTX 与 cubin：两种代码形态

| | PTX | cubin（内装 SASS） |
|---|---|---|
| 本质 | 虚拟 ISA / 中间表示，**文本**，人可读写 | 真实机器码，ELF 容器 |
| 面向架构 | 虚拟：`compute_103` | 实：`sm_103` |
| 兼容性 | **向前兼容**：新卡可由驱动现场 JIT | **精确但不移植**：跨代不可用 |
| 优化 | 还差一步转换 | ptxas 按精确架构精调完，直接执行 |
| 查看 | `cuobjdump -ptx` | `cuobjdump -sass` |

- 类比：PTX 之于 cubin ≈ JVM bytecode 之于 native 机器码、LLVM IR 之于 `.so`。
- SASS 基本不公开文档、每代架构都变——这是 PTX 存在的根本理由（跨代可移植的载体）。
- PTX 的双重角色：① 管线的必经中间产物（每次编译都过一遍）；② 跨代发行时嵌在 fatbin 里的"可移植承诺"（§5）。
- **PTX 从不直接被 GPU 执行**：只会被 ptxas 编译成 SASS（编译期）或被驱动 JIT 成 SASS（运行期），硬件只认 SASS。

上条里的"驱动 JIT"是本手册**首次出现**的 JIT——先预告两层，定义见后文：

- **驱动级 JIT**：驱动（libcuda 内置引擎）把 fatbin 里的 PTX 现场编译成 SASS——旧发行物在新架构卡上的兜底（定义见 §7.3）；
- **库级 JIT**：库在你的机器上从**源码**现编 kernel（DeepGEMM / Triton，定义见 §8）。

本节及 §3/§5 里的"驱动 JIT"均指前者。

### 2.2 cubin 与 fatbin：执行单元与发行容器

一句话：**cubin = 一份架构 SASS 的 ELF 容器；fatbin = 一沓 cubin（+ 可选 PTX）的发行容器**。

| | cubin | fatbin |
|---|---|---|
| 装什么 | 恰好**一个**架构的 SASS | **多个** cubin + 可选 PTX 兜底 |
| 谁消费 | 驱动直接加载（`cuModuleLoadData` 喂 cubin，§6 路线 B） | runtime 注册（`__cudaRegisterFatBinary`），驱动在其中四级匹配挑选（§7.2） |
| 形态 | 独立文件（`--cubin` 产物、DeepGEMM 缓存的 `kernel.cubin`） | **通常非独立文件**——嵌在宿主二进制的 `.nv_fatbin` 数据段（§5） |
| 角色 | 执行的最小单元 | AOT 发行 + 运行时选择的载体 |

嵌套关系：

```
宿主二进制（.so）
└── .nv_fatbin 数据段
    └── fatbin
        ├── cubin #1（sm_75 SASS）  ┐
        ├── cubin #2（sm_80 SASS）  │ 驱动加载时四级匹配，
        ├── ...                     │ 从中挑一份（§7.2）
        └── PTX（compute_XX，兜底）  ┘
```

`cuobjdump --list-elf xxx.so` 列出的 `ELF file N: ...sm_XX.cubin` 行，列的就是 fatbin 的**成员 cubin**——所以 `--list-elf` 能看到一串架构，而每个 cubin 只属于一个架构（§10）。

类比 Docker multi-arch：fatbin ≈ **manifest**（镜像清单，`docker pull` 按 CPU 架构挑一份），cubin ≈ 各架构的镜像本身，PTX 兜底 ≈ "没有现成的就现场 JIT 一份"。

为什么分两层：AOT 要一份发行物覆盖多架构 + 未来架构，需要一个容器把多个 cubin + PTX 打包成**可注册、可挑选**的整体——`__cudaRegisterFatBinary` 一次注册，驱动自己挑。而路线 B 的库运行时现编精确架构，单架构单 cubin 直接喂驱动，天然用不上这层包装（DeepGEMM 缓存里就是裸 `kernel.cubin`，§6）。

## 3. 架构命名与换算
[↩ 目录](#目录)

### 3.1 两套名字：虚拟架构与实架构

**虚拟架构 `compute_XX`——PTX 的"目的地"**

- 出现在哪：PTX 文本的 `.target` 行、nvcc 的 `arch=` / `--gpu-architecture`、`code=compute_XX`（把 PTX 本身装进 fatbin）。
- 含义：按哪一代**虚拟指令集**生成 PTX——一代硬件能力的抽象快照，不对应任何具体芯片。
- 兼容性单位：`compute_XX` 的 PTX 可被驱动 JIT 到该代**及之后所有代**实架构——§2"向前兼容"的承载者就是它。

**实架构 `sm_XX`——SASS / cubin 的"出生地"**

- 出现在哪：ptxas 的编译目标、cubin 的属性（cuobjdump 输出的 `arch = sm_XX`）、nvcc 的 `code=sm_XX`、日常说的"这卡是 sm_XX"。
- 含义：按哪一代**真实硬件**生成最终机器码——一份 cubin 只属于一个 `sm_XX`。
- 兼容性单位：仅该代芯片家族（H100/H200 同为 sm_90、共享同一份 cubin）；越界即不可用（§7 四级匹配）。

**换算**：compute capability `10.3` 去点 = `103` → 虚拟 `compute_103`、实 `sm_103`。

**配对**：两个名字成对出现在同一条 `-gencode` 里，分工——**`arch=` 决定 PTX 写给谁，`code=` 决定 fatbin 里装什么**：

```
-gencode arch=compute_103,code=sm_103                 # fatbin 只装 cubin
-gencode arch=compute_103,code=[compute_103,sm_103]   # 同上，另把 PTX 装进 fatbin（"兜底"就是这一半）
```

torch env 值只有带 `+PTX` 后缀（如 `10.3+PTX`）才生成后一种——本机按 `TORCH_CUDA_ARCH_LIST=10.3` 重建的 DeepEP `.so` 无 PTX，正是这个原因。

**对应关系澄清**（防"compute_XX 对应一组 sm_XX"的误读）：

- **命名**：compute_XX 与 sm_XX 按版本号**一一配对**（compute_103 ↔ sm_103——同一个 cc 10.3 的两个名字），不存在"一个 compute_10 覆盖一组 sm_10x"的命名；
- **兼容**："一份 compute_XX 的 PTX 能跑很多代卡"是 PTX **向后兼容**（该代及之后所有代，**开放集合**，靠驱动 JIT，§2）——这是运行时属性，不是命名对应；
- **家族**：真正"一个名字精确覆盖一组同家族 sm"的是 `sm_XXf`（§3.2，cubin 家族变体，直接执行免 JIT）——封闭集合，与 compute_XX 是两回事。

记忆法：**compute_XX 是"为哪一代虚拟机写"，sm_XX 是"在哪一代真机上跑"**——PTX 世界只认前者，cubin 世界只认后者。

### 3.2 实架构的两个变体：f 与 a

| 概念 | 名称 | 例（compute capability 10.3） |
|---|---|---|
| 虚拟架构 | `compute_XX` | `compute_103` |
| 实架构 | `sm_XX` | `sm_103` |
| family 变体（CUDA 12.9/13） | `sm_XXf` | `sm_103f`：cubin 可在 sm_103 及同家族后续型号直接运行，免驱动 JIT |
| 特性变体 | `sm_XXa` | `sm_100a`：启用芯片专属特性（TMA/wgmma 等）的 cubin，**仅同号芯片可执行** |

映射层次（覆盖范围从宽到窄）：

```
compute_XX（该代及之后所有架构，靠驱动 JIT）
  > sm_XXf（同家族，含后续型号）
  > sm_XX（一代芯片家族）
  > sm_XXa（精确到一款芯片的特性集）
  > 具体一款 GPU 型号
```

| `sm_XX` | 对应的 GPU 型号（同家族多款共用一个实架构） |
|---|---|
| `sm_80` | A100、A30 |
| `sm_86` | RTX 3090/3080/3070、A40、A6000 |
| `sm_89` | RTX 4090、L40、L40S |
| `sm_90` | H100、H200 |
| `sm_100` | B200 / GB200 |
| `sm_103` | B300（本机） |

`a` 变体的定位：为榨取芯片专属硬件特性而放弃一切可移植性，覆盖范围比 `sm_XX` 更窄（同代家族其它型号也不行）。重度依赖这些特性的高性能库（FlashInfer/DeepEP/TensorRT 系）发行物里常见**只有 `sm_XXa` cubin、无 PTX 兜底**——架构列表之外的新卡上必然加载失败（§7）。

### 3.3 查询：三个不同的问题别混淆

```bash
# 问题① 本机 GPU 是什么架构？（工具报 cc 数字或型号名，sm 名由换算得出）
nvidia-smi --query-gpu=index,name,compute_cap --format=csv
#    → NVIDIA B300 SXM6 PC, 10.3
python3 -c "import torch; print('sm_%d%d' % torch.cuda.get_device_capability(0))"
#    → sm_103

# 问题② 已装 torch 支持哪些架构？（wheel 的属性，不是 GPU 的属性）
python3 -c "import torch; print(torch.cuda.get_arch_list())"
#    → ['sm_75', 'sm_80', 'sm_86', 'sm_90', 'sm_100', 'sm_120']（本机实测，无 sm_103）

# 问题③ 某个发行物实际装了哪些架构？（唯一可靠校验）
cuobjdump --list-elf xxx.so    # 见 §10
```

①②的实测对照是个典型案例：本机 GPU 是 sm_103，已装 torch 的列表里却没有 sm_103——torch 照样能跑（元素运算 / matmul 实测正常），靠的是发行物的分发机制：libtorch_cuda.so 无 PTX、部分内核带 sm_103 cubin；vLLM 自带内核的 `.so` 无 sm_103 cubin、有 PTX → 走驱动 JIT（`~/.nv/ComputeCache` 实测持续增长，见 §7.3）。若某个 kernel 四级全 miss，才会报 "no kernel image available"。

## 4. 构建期 arch 列表由谁决定：TORCH_CUDA_ARCH_LIST 的三层覆盖
[↩ 目录](#目录)

torch 系扩展（cpp_extension）编进 `.so` 的架构列表，按以下优先级决定：

```
1. env TORCH_CUDA_ARCH_LIST 已设置 → 逐字使用（跳过一切 GPU 检测）
2. 未设置且进程可见 GPU         → 用本机 compute capability
3. 未设置且无 GPU               → torch 内置默认列表
```

生态里有两级常见覆盖，使规则 2 几乎永不生效：

- **基础镜像 ENV 预置**：推理镜像常在 Dockerfile 固化 `TORCH_CUDA_ARCH_LIST`（值来自**镜像构建机**的架构集合，与运行机器无关——列表里出现运行集群尚不存在的架构号也不奇怪；本机镜像实测值 `7.5 8.0 8.6 8.9 9.0 10.0 12.0`，有 10.0、12.0，唯独没有 10.3）；
- **项目 setup.py 兜底**：不少项目写 `os.getenv('TORCH_CUDA_ARCH_LIST', '<自家默认>')`，env 缺失时落到自家列表（DeepEP 实测兜底 `'8.0'/'9.0'`）。

**值的格式与展开规则**（实测）：每个 cc 号自动展开成完整 `-gencode` 配对——arch=compute_XX 与 code=sm_XX 用**同一个数字**一起选好，sm 不用也不能单独指定。多值空格分隔、各自展开一条（实测镜像 ENV 七个号 → 七条 gencode）：

| 值 | 展开为（实测） | 效果 |
|---|---|---|
| `10.3` | `-gencode=arch=compute_103,code=sm_103` | 只装 cubin |
| `10.3+PTX` | 上条 + `-gencode=arch=compute_103,code=compute_103` | 另嵌 PTX 兜底（§3.1 第二种形态的 torch 写法：两条 gencode） |
| `10.0a` | `-gencode=arch=compute_100a,code=sm_100a` | a 特性变体——**compute 侧也带 a**（虚拟架构同样有特性变体） |
| `10.3f` | 报错 `Unknown CUDA arch (10.3f)` | **f 家族变体选不了**——要 f 必须绕过 torch、自己拼 `--gpu-architecture=sm_103f`（DeepGEMM 即如此，vllm-jit.md §4.5） |

推论（在新架构卡上重建 torch 系扩展的通则）：

- 默认链路（镜像 ENV → 项目兜底 → 内置列表）大概率**不含**新卡的架构号——GPU 自动检测被压在最底层，永远轮不到；
- 唯一通路是**显式设置** `TORCH_CUDA_ARCH_LIST=<目标架构>`，显式值同时压过镜像 ENV 与项目兜底；
- 显式列表是**替换**而非追加——设 `10.3` 就只编 sm_103，不携带其它架构；
- 版本串**不反映** arch 内容：同一版本号可对应不同架构集合的产物，`pip show` 无法区分——唯一可靠校验是对 `.so` 做架构清单检查（§10）。

## 5. 发行（AOT）：fatbin 里装什么
[↩ 目录](#目录)

fatbin 自己的位置（§2.2 已述，此处展开）：它**通常不是磁盘上的独立文件**——AOT 发行物是宿主二进制（`.so` / 可执行文件），fatbin 以数据段（`.nv_fatbin`）嵌在其中（嵌入机制见 §6 路线 A）。独立文件形态只在两种场合出现：编译期显式要产物（`--cubin` / `--fatbin`），或 §6 路线 B 的运行时加载（如 DeepGEMM 缓存的独立 `.cubin` 文件）。本节讨论的是"这个内嵌的 fatbin 里装什么"。

五种打包策略：

| 策略 | 做法 | 代价 | 典型 |
|---|---|---|---|
| 全量 cubin | 每个目标架构一个 SASS | 架构组合爆炸、包大 | 游戏 / 专用部署 |
| PTX-only | 只嵌 PTX | 每台机器首次加载都要驱动 JIT | 追求包最小 / 覆盖未来卡 |
| **主流折中** | 常用架构 cubin + 最高档 PTX 兜底 | — | torch 等 pip wheel（`code=[compute_XX,sm_XX]`） |
| family cubin | `sm_XXf` 家族变体 | 需 CUDA 12.9+ 工具链 | DeepGEMM（`sm_103f`） |
| 特性变体集 | 只装若干 `sm_XXa` cubin、**无 PTX** | 架构列表之外的新卡**无任何出路**（§7 四级全 miss） | 高性能推理库生态（如 DeepEP 官方 wheel） |

为什么需要 PTX 兜底：旧发行版遇到新架构时，唯一通用出路就是驱动现场 JIT PTX（机制见 §7.3）。末行正是没有兜底的反面教材——`sm_XXa` 集合不含目标架构且无 PTX 时，新卡上连 JIT 的原料都没有，直接加载失败。

## 6. 宿主侧：设备代码如何进入宿主二进制
[↩ 目录](#目录)

对设备代码而言，宿主是 `.so` 还是可执行文件**没有区别**——两者都是宿主 ELF，fatbin 的嵌入机制完全相同。设备代码进入宿主二进制有两条路线。

### 路线 A：编译期嵌入（torch / 常规 CUDA 库）

nvcc 直接把 .cu 编成 `.so`（去掉 `-shared` 即生成可执行文件，机制相同），嵌入是自动的：

```bash
nvcc -shared kernel.cu -o libkernel.so \
     -gencode arch=compute_103,code=sm_103 \
     -gencode arch=compute_103,code=compute_103   # 附带 PTX 兜底
```

一个 `.so` 的诞生过程：

```
kernel.cu
  │  cudafe++：主机/设备代码分离（<<<>>> 调用点替换为 stub → cudaLaunchKernel）
  ├─ 宿主部分 → 宿主编译器（gcc/g++）→ kernel.o（普通主机代码）
  └─ 设备部分 → cicc → PTX → ptxas → SASS cubin
                   → fatbinary 工具转成 C 字节数组（.fatbin.c）
                   → 同样由宿主编译器编成 .o（GPU 代码躺在数据段）
  两个 .o 由普通链接器（g++/ld）链接 → libkernel.so（或可执行文件）
```

要点：

- **合并成 `.so` 的是普通宿主链接器**，与编纯 C++ 库无区别；nvcc 负责调度各环节。
- **设备代码以"数据"身份进入 `.so`**：fatbin 位于 `.nv_fatbin` 数据段，配上注册它的 stub 代码——**不是"被 `.so` 调用的另一半代码"**（cubin 与宿主代码是"数据 + 驱动"的关系，真正的调用发生在 §7 的 `cudaLaunchKernel`）。
- **宿主代码也被改写**：kernel 调用点被替换为走 runtime API 的 stub，因此链接时依赖 `-lcudart`（或静态嵌入 cudart）。
- 已有 `.cubin` / `.ptx` 也可直接作为 nvcc 输入，重新打包嵌入：`nvcc -shared kernel.cubin -o libkernel.so`。

### 路线 B：运行期显式加载（驱动 API）

核心思想：**kernel 在编译期可以不存在**。路线 A 里 CUDA runtime 自动做的四件事（加载 / 取名 / 打参 / 发射），全部由应用代码显式完成——"显式"是**代码层**的说法（调用写在库代码里），对使用者仍是全自动、无感。

"宿主二进制里**不含 kernel**、只嵌 **cubin 字节**"的意思：kernel 的机器码字节确实在文件里，但只是**一袋数据**——没有 `<<<>>>` 改写成的 stub、没有 `.nv_fatbin` 段、没有 `__cudaRegisterFatBinary` 注册；编译器/链接器/runtime 从头到尾不知道有 kernel 存在（类比：嵌入一张 PNG——数据在，机制不在）。

**编译期（可选的唯一动作）**——把 cubin 变成 C 字节数组嵌进宿主二进制；也可以什么都不嵌、运行期读独立 cubin 文件：

```bash
nvcc -cubin kernel.cu -o kernel.cubin    # 产物是 ELF 文件
bin2c -c kernel.cubin > kernel_cubin.h   # 或 xxd -i：文件字节原样搬进 .h
```

```c
// kernel_cubin.h（开头即 ELF 魔数——cubin 本来就是个 ELF 文件）
const unsigned char KERNEL_CUBIN[] = { 0x7f, 0x45, 0x4c, 0x46, ... };
```

这一步的本质是 **cubin 字节 → 宿主程序可拿到的内存指针**——`cuModuleLoadData` 只吃一个指向字节流的指针，不关心来源。`.h` 只是 C 源码世界的惯例文件名（`.c` / `.inc` 均可），甚至可以**完全不经过 C 源码**，共四种落法：

| 落法 | 形态 | 实例 |
|---|---|---|
| C 数组（bin2c / `xxd -i`） | 字节进 C 源码，编进宿主二进制 | 最传统（上面的示例） |
| objcopy 直变目标文件 | `objcopy -I binary kernel.cubin kernel.o`——零 C 源码，链接后以 `_binary_..._start/_end` 符号取地址 | 嵌入式 / 驱动常用 |
| 运行期读文件 | 不嵌入：`cuModuleLoad(路径)`，或读进内存再 `cuModuleLoadData(buf)` | DeepGEMM（读缓存的 `kernel.cubin`，全程无 C 数组） |
| 进程内现编 | 字节只在内存里（编译产物指针），不落盘不进源码 | NVRTC 系 |

**运行期四步**（应用代码接管）：

```c
#include <cuda.h>
#include "kernel_cubin.h"                            // 或运行期读文件
CUmodule mod; CUfunction fn;
cuModuleLoadData(&mod, KERNEL_CUBIN);                // ① 加载：字节 → 模块
cuModuleGetFunction(&fn, mod, "_Z10my_kernelPfS_i");  // ② 取名：按字符串查句柄
void *args[] = {&out, &in, &n};                       // ③ 打参：指针数组
cuLaunchKernel(fn, grid, block, 0, 0, args, NULL);    // ④ 发射：grid/block 显式传，无 <<<>>> 语法糖
```

边界：**驱动只吃编译产物**（cubin 字节 / PTX 文本），永远不吃 `.cu` 源码——`.cu` 是 nvcc 的粮食，`.cubin` 才是驱动的粮食。加载时的行为（喂 cubin 只走"精确命中"一级、架构不符直接失败等）见 §7.1 入口②；"运行时现编源码 + 路线 B 加载"的组合形态（DeepGEMM）见 §8.3。

### 两条路线对照

| | A：编译期嵌入 | B：运行时加载 |
|---|---|---|
| 宿主二进制里装什么 | fatbin（cubin + PTX），配 stub 与注册机制 | 只有 cubin 字节（纯数据）/ 外部文件 |
| 加载 API | runtime API，自动注册 + 懒加载（§7.1 入口①） | 驱动 API `cuModuleLoadData`（§7.1 入口②） |
| 符号绑定 | 编译期 `<<<>>>` | 运行期按字符串名查找 |
| JIT 层级 | 命中失败可回落驱动 JIT | 产物即精确 SASS，跳过驱动 JIT |

## 7. 运行时：模块加载与四级匹配
[↩ 目录](#目录)

### 7.1 谁触发加载：应用 → 驱动的两条入口

四级匹配发生在驱动做"模块加载"的这一刻，应用通过两条入口让驱动拿到设备代码：

- **入口 ①（路线 A，自动两步）**
  1. **注册**：`.so` 被装载（ld.so / dlopen）时，cudafe++ 生成的 stub 构造函数逐编译单元调用 `__cudaRegisterFatBinary()` / `__cudaRegisterFunction()`——把"数据段里的 fatbin 地址 + 各 kernel 句柄"登记给 runtime。此时**只登记，不匹配、不上传**。
  2. **首次使用**：第一次调到某 kernel（`<<<>>>` 展开成的 `cudaLaunchKernel` stub）时，runtime 向驱动发起模块加载；驱动解析 fatbin、走四级匹配、上传 SASS。CUDA 11.7+ 默认**懒加载**（`CUDA_MODULE_LOADING=LAZY`）——没被调用的 kernel 可以永不触发匹配；`EAGER` 则在首次 CUDA 调用时全量加载。
- **入口 ②（路线 B，显式）**：应用直接调驱动 API——`cuModuleLoadData()` → `cuModuleGetFunction()` → `cuLaunchKernel()`（四步详解见 §6 路线 B）。加载行为：喂 cubin 只走"精确命中"一级——架构与当前 GPU 不符**直接失败、无任何兜底**（cubin 里没有 PTX，连 JIT 原料都没有）；喂 **PTX 文本**则走驱动 JIT；`cuModuleLoad()` 是读文件路径的变体。

两条入口殊途同归：解析、匹配、上传都发生在**驱动（用户态 libcuda）的模块加载例程**里，对应用透明——应用能感知的只有首次调用的延迟（触发 JIT 时明显）和失败时的报错。

### 7.2 四级匹配

加载 fatbin 时，驱动按顺序找：

```
1. 精确 SASS 命中（sm_103 == sm_103；sm_XXa 变体仅同号芯片命中）→ 直接用
2. family 命中（sm_103f 家族 cubin）     → 直接用
3. 只有 PTX                              → 驱动级 JIT：PTX → SASS
4. 都没有                                → "no kernel image available"
```

四级全 miss 并不罕见：只装 `sm_XXa` cubin 集且无 PTX 的发行物（§5 表末行），在架构列表之外的卡上 1、2、3 逐级落空——用户看到的形态是加载期 `named symbol not found` 或运行期 "no kernel image available"，**没有任何一层能救**（第 3 级连 JIT 原料都缺）。"PTX 兜底"买的就是第 3 级这条唯一的通用退路。

### 7.3 驱动级 JIT 与"加载 vs 发射"

**驱动级 JIT 是什么**：把 PTX 编译成当前 GPU 架构 SASS 的动作，执行主体是**驱动**（用户态 libcuda 内置的 JIT 引擎）——"级"指执行主体所在层，对照"库级 JIT"（§8：库自己调编译器编源码）。触发时机：模块首次加载、四级匹配走到第 3 级（有 PTX、无匹配 cubin）时现场完成，对应用透明（只感觉首次调用慢）。它是 PTX"可移植承诺"（§2）的**唯一兑现机制**——旧发行物在新架构卡上的通用出路。与库级 JIT 对照：主体不同（驱动 vs 库）、输入不同（PTX vs 源码）、时机不同（模块加载 vs 请求到来）。

**驱动 JIT 缓存**：编译结果（SASS）缓存在 `~/.nv/ComputeCache`——同一段 PTX 全机只编一次，后续进程直接装缓存。默认上限 ~1GB，满后逐出最旧条目；`CUDA_CACHE_MAXSIZE` / `CUDA_CACHE_PATH` 可调、`CUDA_CACHE_DISABLE=1` 关闭。本机实测 575M→658M、服务运行中持续增长——vLLM 自带内核无 sm_103 cubin、靠 PTX JIT 的直接产物。缓存位置与持久化方案见 §9.3。

**加载 vs 发射**：模块首次使用时，驱动把匹配的 SASS 上传 GPU，代码从此驻留 GPU 侧；此后每次 kernel 调用只发射**启动命令**（grid/block 配置 + 参数指针），不再传输代码本身——调一万次 matmul，SASS 只上传一次。

## 8. 库级 JIT：运行时从源码编译
[↩ 目录](#目录)

两层运行时编译，按**执行主体**分层：

| 层 | 执行主体 | 输入 | 时机 |
|---|---|---|---|
| 驱动级（§7.3） | 驱动（libcuda 内置 JIT 引擎） | PTX | 模块加载、四级匹配第 3 级 |
| **库级（本节）** | 库自己调编译器 | **源码** | 请求带来新参数组合时 |

组件全景（后三行是库级 JIT 的三种实现）：

| 组件 | 发行形态 | 运行时发生什么 |
|---|---|---|
| torch（AOT 对照项） | fatbin：多架构 SASS + PTX | 驱动匹配，偶发驱动 JIT（**不是**库级） |
| DeepGEMM | 源码模板 + nvcc 子进程 | `nvcc --cubin` → 精确 arch cubin → `cuModuleLoadData`，跳过驱动 JIT |
| Triton | Python 源码 | 自带 LLVM → PTX → `ptxas` → cubin（不走 nvcc） |
| NVRTC 类库 | C++ 源码 | 进程内编译 C++ → PTX/cubin |

### 8.1 特化：为什么必须运行时编译

最优 kernel 的形状取决于**运行时才知道的参数**，编译期不存在"一份通用的最快 kernel"，只存在"针对特定参数实例化的最快 kernel"。DeepGEMM 按 m/n/k、block 划分特化；Triton 按 dtype、`constexpr`、指针对齐、stride、num_warps 等组合特化——每个参数组合一份 cubin，**缓存键 = 特化参数 + 目标架构的哈希**。

### 8.2 通用五步闭环（所有库级 JIT 同构）

```
① 触发：请求带来新参数组合
② 查缓存：命中 → 直接加载（常态，零编译）
③ 生成源码：模板实例化（DeepGEMM 生成 kernel.cu）/ AST（Triton）
④ 调编译器 → cubin（三种路线见上表）
⑤ 存缓存 + cuModuleLoadData 加载（§6 路线 B）
```

两个必然推论：

- **产物是精确 SASS、跳过驱动 JIT**：库编译时就知道目标 GPU（启动时查一次架构），直接编精确 arch——加载走"精确命中"级，驱动层只剩"加载"没有"编译"（§7.1 入口②）。
- **运维三特征**：首次成本（每特化一次：DeepGEMM 秒级 / Triton 百毫秒级）；**缓存丢失即重付**（Pod 重建 / 节点回收后首个请求慢——持久化 `~/.cache` 的动机）；编译期间可观测到 nvcc / cc1plus 进程（`vllm-jit.md` 主题：正常现象，非故障）。

### 8.3 DeepGEMM 实例：路线 B + 库级 JIT 的组合

kernel 按请求的 shape/block 动态生成，编译期根本不存在。注意这是**两件正交的事**叠在一起——

- **动态编译**（得到 cubin）：属于库级 JIT，**不是路线 B 的组成部分**——路线 B 对 cubin 的来源没有要求（编译期备好的字节、磁盘现成文件、运行时现编，都行）；
- **动态加载**（cubin 喂给驱动）：这才是路线 B。

编译参数的来源要分清：GPU 架构（sm_103f）启动时**查一次环境**、之后不变；每个请求变化的是 **shape/配置**——这才是"动态"的真正来源。时间线上两者频率不同：**编译只在缓存未命中时发生**（每 shape 一次，此后缓存接管）；**加载每个进程启动都要重做**（§7.3 加载侧）。

缓存目录里 `kernel.cu` 与 `kernel.cubin` 并存但角色不同：前者是库级 JIT 的输入（喂 nvcc），后者才是喂给驱动的产物——这也解释了 DeepGEMM 的缓存是独立 cubin 文件而不是 `.so`。查找闭环、key 组成、条目解剖等 DeepGEMM 细节的权威记录见 `vllm-jit.md` §4。

## 9. 缓存全景（实测）
[↩ 目录](#目录)

三套互不相干的缓存（持久化要点详见 §9.3）：

| 缓存 | 归属 | 默认位置 | 持久化要点 |
|---|---|---|---|
| DeepGEMM 编译缓存 | 库级 | `~/.cache/vllm/deep_gemm` | 在 `.cache` 内，挂载即覆盖（机制见 vllm-jit.md） |
| Triton 缓存 | 库级 | `~/.triton/cache` | `.cache` 之外；`TRITON_CACHE_DIR` 可搬（§9.3） |
| 驱动 JIT 缓存 | 驱动级 | `~/.nv/ComputeCache` | `.cache` 之外；`CUDA_CACHE_PATH` 可搬（§9.3） |

### 9.1 布局实测（mn-1-dep-0 节点的 `/root/.cache`，全部由 Pod 进程生成）

```
/root/.cache/                     ← hostPath 整体挂载的持久化域
├── huggingface/     642G   HF 权重（启动脚本 hf download）
├── deepep-build/    588M   DeepEP 构建缓存（源码 + wheel + 版本戳；分支 C 写、分支 B 读）
├── vllm/            290M   vLLM 全家桶（VLLM_CACHE_ROOT 默认位置）：
│   ├── torch_compile_cache/ 284M  torch.compile 产物（按 DP rank 分目录）
│   ├── deep_gemm/          69 条 kernel 档案（5.9M；60 启动期 + 9 运行期）
│   ├── flashinfer_autotune_cache/ 48K
│   └── modelinfos/、dummy_cache/   小杂项
├── nv-computecache/  575M   驱动 JIT 缓存（CUDA_CACHE_PATH 搬家，§7.3；两级十六进制分片）
├── triton/          11M    Triton 缓存（TRITON_CACHE_DIR 搬家；哈希目录 + tmp）
└── flashinfer/      392K   FlashInfer 主缓存
```

生成时间线（两代 Pod）：10-21 冷启生成 huggingface / deepep-build / vllm / flashinfer；10-22 热启生成搬家的 nv-computecache / triton（从零积累，代价见 §9.3）。曾出现过一个**非 Pod 产物** `snowflake`（4K、属主组 989 的节点级系统用户，推测节点守护进程所为）——重启后消失，临时产物。

### 9.2 deep_gemm 条目解剖

一个特化的全部档案在 `<kernel 名>.<hash>/`：四个文件（`kernel.cu` 模板实例化包装、`kernel.cubin`、`meta.json` 完整命令行档案、`.committed` 提交标记）。**条目逐文件解剖详见 `vllm-jit.md` §4.5**——该文档是 DeepGEMM 细节的权威。

### 9.3 `.cache` 之外的缓存：原因与搬家方案

`~/.nv`（驱动 JIT 缓存）与 `~/.triton`（Triton）都不在 `.cache` 下——各自走**传统 dot-dir 惯例**（`~/.nv` 是驱动的自留地，早于 XDG `~/.cache` 惯例、为兼容从不搬家；`~/.triton` 是 `TRITON_HOME` 默认 `~/` 下的 `.triton/`），XDG 约定对它们无效。挂 `~/.cache` 的持久化方案盖不到这两处，但两者都支持环境变量**搬家进已挂载的域**——一个卷盖全部：

```bash
TRITON_CACHE_DIR=/root/.cache/triton          # 实测（triton 3.7.1 knobs）：直接覆盖缓存目录
CUDA_CACHE_PATH=/root/.cache/nv-computecache  # 驱动 JIT 缓存（§7.3；搬家后首次重建）
```

**已实测生效**（2026-10-22，`mn-1-dep.yaml` 内置这两个 env 后滚动重启）：`nv-computecache` 从零积累至 **575M**、`triton` 积累 **63 条 / 11M**，均落在挂载域内——"一个卷盖全部"落地；代价是搬家后各重建一次（驱动 JIT 在启动+流量中重新积累）。

量值参考：本机 `~/.triton` 仅 68K（此负载 Triton 用得少，影响可忽略）；ComputeCache 的增长特性见 §7.3。

## 10. cuobjdump 实战（实测采样）
[↩ 目录](#目录)

cuobjdump 是 CUDA 版的 `objdump`——把 `.so` / 可执行文件 / `.cubin` 里的设备代码开箱检查。四种常用视图：

| 命令 | 看什么 | 输出形态 |
|---|---|---|
| `--list-elf` | 装了哪些架构的 cubin | 每架构一行 `ELF file N: 名.sm_XX.cubin` |
| `--list-ptx` | 装了哪些架构的 PTX 兜底 | `PTX file N: ...`；`No PTX file found`＝无兜底 |
| `-ptx` | PTX 文本本身 | 人可读的虚拟 ISA |
| `-sass` | SASS 反汇编 | 机器码形态，开头是 fatbin 头 |

**案例 1：架构清单，一行定生死**——同一 DeepEP 扩展，官方 stock 与本机重建（`TORCH_CUDA_ARCH_LIST=10.3`，§4）的差别全在 `--list-elf` 里：

```
$ cuobjdump --list-elf 官方stock版_C.so          $ cuobjdump --list-elf 重建版_C.so
ELF file 1: ....1.sm_100a.cubin                  ELF file 1: ....1.sm_103.cubin
ELF file 2: ....2.sm_90a.cubin                    （只此一条）
```

**案例 2：PTX 兜底判读**——`--list-ptx` 对无兜底的发行物给出明确信号（stock DeepEP，§5 表末行的发行形态）：

```
$ cuobjdump --list-ptx 官方stock版_C.so
cuobjdump info : No PTX file found to extract from '..._C.so'   ← 零 PTX，新架构无 JIT 出路
```

**案例 3：主流折中的完整形态**——`vllm/_C_stable_libtorch.abi3.so` 是 §5"主流折中"策略的活样本（cubin 列表 + PTX 兜底同时在场）：

```
$ cuobjdump --list-elf ...abi3.so | grep -oE 'sm_[0-9a-z]+' | sort | uniq -c
     59 sm_75    68 sm_80    48 sm_86    54 sm_89    55 sm_90
     26 sm_90a   69 sm_100   82 sm_120              ← 七种 cubin
$ cuobjdump --list-ptx ...abi3.so | grep -oE 'sm_[0-9a-z]+' | sort | uniq -c
     19 sm_80     1 sm_89     8 sm_90              ← 外加 PTX 兜底
```

**案例 4：PTX 文本 vs SASS 形态**——同一个工具的两个视图，直观对照 §2.1 表格：

```
$ cuobjdump -ptx ...abi3.so        ← PTX：文本、人可读、.target 声明架构
.target sm_80
.address_size 64
.extern .shared .align 16 .b8 _ZN6marlin2shE[];
.visible .entry _ZN6marlin6MarlinILl1125899...     ← 模板实例化后的超长符号名

$ cuobjdump -sass 重建版_C.so       ← SASS：二进制、按精确架构精编
Fatbin elf code:
================
arch = sm_103
code version = [1,8]
host = linux
compile_size = 64bit
compressed                                         ← 产物可压缩存储
```

**上机复现**：`kubectl exec -it mn-1-dep-0 -- bash` 进入 Pod 后可直接粘贴——

```bash
# 案例 1/2：DeepEP 扩展（当前 Pod 里的重建版；stock 版在 dep8ep-0 同样可跑）
SO=$(find /usr/local/lib/python3*/dist-packages/deep_ep -name '_C*.so' | head -1)
cuobjdump --list-elf $SO      # → 一条 sm_103 cubin（案例 1 右侧）
cuobjdump --list-ptx $SO      # → No PTX file found（案例 2 同一信号）

# 案例 3：主流折中样本（cubin 列表 + PTX 兜底同场）
F=/usr/local/lib/python3.12/dist-packages/vllm/_C_stable_libtorch.abi3.so
cuobjdump --list-elf $F | grep -oE 'sm_[0-9a-z]+' | sort | uniq -c
cuobjdump --list-ptx $F | grep -oE 'sm_[0-9a-z]+' | sort | uniq -c

# 案例 4：两种文本形态（-ptx 开头是 fatbin 头，跳到 .target 才是 PTX 正文）
cuobjdump -ptx  $F | sed -n '/^\.target/,+6p' | head -8
cuobjdump -sass $SO | head -8

# 判断某架构是否可用（管道判退码即可）
cuobjdump --list-elf $SO | grep -q sm_103 && echo 可用 || echo 不可用
```

## 11. 实例链路
[↩ 目录](#目录)

**链路 A：DeepGEMM 运行期 JIT**（路线 B + 库级 JIT；机制细节见 `vllm-jit.md`、闭环见 §8.3）：

```
torch.cuda.get_device_capability() → (10, 3)
  → DeepGEMM 拼 --gpu-architecture=sm_103f
  → nvcc --cubin（.cu → PTX → ptxas → SASS，PTX 用完即弃）
  → kernel.cubin（sm_103 家族 SASS）
  → cuModuleLoadData 加载 → 精确 SASS 直接执行
  → kernel 缓存于 ~/.cache/vllm/deep_gemm
```

**链路 B：torch 系扩展构建期重打**（路线 A；实测于 mn-1-dep-0；arch 决定链见 §4）：

```
TORCH_CUDA_ARCH_LIST=10.3（显式压过镜像 ENV 与项目兜底）
  → torch cpp_extension → -gencode arch=compute_103,code=sm_103
  → nvcc -shared → _C.so（仅 sm_103，替换而非追加）
  → 打成 wheel（版本串不变）→ cuobjdump --list-elf 验证架构（唯一可靠校验）
```

**调用链速览**（谁在调用谁）：

```
torch（路线 A）：
Python → libtorch_cuda.so 宿主 C++ → cudaLaunchKernel
  → 驱动四级匹配取 SASS（首次已上传驻留）→ 发射启动命令 → SM 执行

DeepGEMM（路线 B）：
Python (vLLM) → 运行期 nvcc --cubin 现编 → cuModuleLoadData → SASS 执行
```

一句话总结：**PTX 是可移植的承诺，cubin 是性能的兑现；AOT 发行在两者间找平衡（cubin 列表 + PTX 兜底），JIT 库干脆把编译搬到你机器上现场兑现。**

## 12. 相关文档
[↩ 目录](#目录)

- `vllm-jit.md`——vLLM 运行时编译与启动运维手册（DeepGEMM / Triton / torch.compile；本手册在 DeepSeek/vLLM 上的落地细节）
