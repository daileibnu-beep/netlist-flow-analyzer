# 按实例展开的跨层次连接图

`analyze.py flow` 根据真实实例路径展开层次，把父层 net 与子模块 port 的逐位绑定接起来，生成不同深度实例之间的有向连接图。分析使用 Python 3.9+ 标准库；图形输出还需 Graphviz dot（可用 --dot 指定路径）。

## 只展开 AXI 实例，depth 为 1

在工具目录运行（下例假定网表位于相邻目录）：

```bash
python3 analyze.py flow ../04_MemPool_Group/mempool_group.v \
  --top mempool_group \
  --depth 1 \
  --expand-at 'mempool_group/i_axi_interco' 1 \
  --out axi_depth1
```

两个深度分别控制不同范围：

- `--depth 1`：分析根的直接子实例作为默认显示节点。
- `--expand-at PATH 1`：仅对 PATH 实例额外展开一层，显示它的直接子实例；这些子实例继续保持折叠。
- 所选实例的 depth 相对于该实例计算；祖先会为到达该实例而展开。
- 位于该实例内的默认深度由它的相对 depth 决定，其他分支沿用原来的设置。

例如原来显示 `TOP/u_axi → TOP/u_tile`，展开后显示 `TOP/u_axi/u_mux → TOP/u_tile`。
父层中连到 u_axi 的线，会按真实 port 位绑定接到 u_mux 等内部实例；不把 u_axi 当成透明功能单元。

本机 MemPool 中，i_axi_interco 的直接已定义子实例为：

```text
mempool_group/i_axi_interco/
├── gen_bottom_level.gen_ro_cache.i_snitch_read_only_cache
├── gen_bottom_level.i_axi_mux
└── gen_bottom_level.i_axi_id_remap
```

`depth=1` 后，这三个实例代替 i_axi_interco 成为图节点。其他顶层实例保持折叠。

同样的单实例展开，也可以写成：

```bash
python3 analyze.py flow ../04_MemPool_Group/mempool_group.v \
  --expand 'mempool_group/i_axi_interco' --expand-depth 1 \
  --out axi_depth1_alternative
```

## 不同实例分别设置不同 depth

```bash
python3 analyze.py flow ../04_MemPool_Group/mempool_group.v \
  --depth 1 \
  --expand-at 'mempool_group/i_axi_interco' 1 \
  --expand-at 'mempool_group/gen_tiles[0].i_tile' 2 \
  --out mixed_depth
```

`--expand-at PATH DEPTH` 可以重复，每个实例的 depth 独立。
多个 `--expand PATH` 使用同一个 `--expand-depth`，默认值为 1。
同一路径设置不同 depth 会报错，避免配置含义不明确。嵌套选择时，内部更具体的选择控制自己的子树。

## 分析根与停止边界

只看一个子树：

```bash
python3 analyze.py flow ../04_MemPool_Group/mempool_group.v \
  --root 'mempool_group/gen_tiles[0].i_tile' --depth 2 \
  --out tile0_subtree
```

此时根边界是 tile0 的 I/O，图中用 `mempool_group/gen_tiles[0].i_tile/$PORT` 表示。
分析不会到该根以外查找其他实例。要观察 AXI 内部与 tile 外部的跨层连接，分析根应保留为两者的共同祖先，例如顶层。

提前停止一个分支：

```bash
python3 analyze.py flow ../04_MemPool_Group/mempool_group.v \
  --expand-at 'mempool_group/i_axi_interco' 2 \
  --stop 'mempool_group/i_axi_interco/gen_bottom_level.gen_ro_cache.i_snitch_read_only_cache' \
  --out axi_cache_folded
```

`--stop` 的实例保留为完整边界节点，不继续展开。不能在 stop 的实例或其下面另加展开要求。
`--stop` 不负责主动展开祖先；目标未被到达时会报错，请相应设置 depth 或 expand-at。
默认遇到没有已定义子模块的模块会保留为最深的 module 边界；纯 assign 连线包装可继续穿越。
显式指定 expand-at 可以要求打开这种边界，但若内部全为未知 leaf，追踪会停在这些未知单元处，可能不再有可画的模块节点。

## 路径写法

- `--top` 是 module 类型名，其余路径参数是实例路径。
- 用 `/` 区分层次，不能用 `.` 拆分层次。
- `gen_tiles[0].i_tile` 是一个完整实例名，其中的点和方括号按字面匹配。
- 路径可以带顶层前缀，也可以相对顶层。建议始终用单引号包住路径，防止 shell 展开方括号。
- 不需要写 Verilog 转义标识符前面的反斜杠。
- 标识符自身若含 `/`，用 `~1` 表示；自身若含 `~`，用 `~0` 表示。

## 输出与读图

- `flow.dot`：模块级有向图，每对模块的同向端口连接合并为一条边；边上只标位连接数量，线宽随数量增大。
- `flow.svg`：由 Graphviz 直接渲染 flow.dot 得到的矢量图。
- `flow.html`：直接内嵌同一张 flow.svg 的内容，支持平移、缩放、定位模块和点击连线查看汇总。具体信号线不在图中展开，端口对及逐位证据在明细中按需查看。
- `flow_modules.json`：模块间汇总计数、对应端口对数量、显示阈值及线宽。
- `flow.json`：完整节点、全部端口对、源位—目的位映射、展开边界绑定及诊断。汇总绘图不修改这些分析数据。
- `flow_render.json`、`flow_graphviz.log`：Graphviz 版本、绘图设置和渲染诊断。
- `module_index.json`：flow 模式本次重新扫描的原文字节/行号索引。
- `logs/`、`run_manifest.json`：flow 模式阶段日志、选择参数、输入 SHA256、耗时和状态。
- `source/`：仅 .gz 输入时保留解压后的网表。

数量为端口位配对数之和，不是独立 net 数。双向关系分别保留箭头和数量；线宽在当前图中按数量的平方根相对缩放为 1–7 pt。
模块标签用较短实例名，完整实例路径和 module 类型放在悬停提示及明细中。

只更新旧图，无需重新解析大网表：

```bash
python3 analyze.py redraw --data /path/to/flow.json --out module_graph
```

flow 和 redraw 都支持下列图形参数：

- `--dot /path/to/dot`：指定 Graphviz 程序；缺少 dot 时明确报错。
- `--rankdir LR` 或 `--rankdir TB`：左右或上下布局。
- `--render-timeout 60`：渲染时间上限，单位为秒。
- `--min-connections 10`：明确筛去连接数较少的模块边。默认 1，保留全部；筛去的边数和位连接数会在页面标明，完整 flow.json 不丢弃明细。

新一次层次展开仍需运行 flow；redraw 仅改变显示，不改变分析层次或连接计数。
`--out` 必须是不存在的新目录。flow 失败时保留 *.working-* 目录与日志。

## 拼接算法和边界

1. 以 module 类型缓存解析结果，以完整实例路径实例化线网，防止同类型不同实例串线。
2. 将端口声明、实际连接中的 range、bit select、concat 展开为按位序排列的列表。
3. 父子接口按方向建立边：父层 net → 子模块 input，子模块 output → 父层 net。
4. 简单 `assign lhs = rhs` 建立 rhs → lhs 的有向逐位边，不用无向合并反向传播。
5. 从可见边界的输出位及分析根的输入位出发，沿图找到可见输入位，汇总端口对。
6. 同一个源位—目的位只计一次；保存全部位映射，每个端口对最多保存 8 条最短路径证据。

折叠模块的 input 和 output 之间没有推断的功能边。常量独立记录，不把不同的常量绑定线网短接在一起。
未知 leaf 方向和逻辑不推断；inout 跳过并记录。未匹配到连接不证明不存在经过未知 leaf 的通路。
图中保留自身反馈边和多个已知源诊断，不能用作电气规则签核。

当前解析器支持已展开的非 ANSI 结构 Verilog 子集。被展开作用域中的不支持语句、表达式、位宽错误会使该次分析失败。
没有 SDC/Liberty、单元功能或时序模型；输出是跨层次结构连接图。功能数据流、组合逻辑穿越、寄存器级数、时序和真实项目效果均待验证。

资源保护默认值：`--max-instances 200000`（含遇到的未知 leaf）、`--max-bits 2000000`、`--max-pairs 200000`。
超限时会报错，可收窄分析根、减少 depth，或明确提高相应上限。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖实例隔离、父子接口拼接、升降序及拼接重排、独立相对深度、停止边界、子树根、源代码行号、常量、未知 leaf、inout、反馈和普通/压缩输入。
发布前的真实 Innovus 回归、独立逐位对照和版本范围见 [EDA 测试记录](docs/EDA_TEST_REPORT.md)，复测方法见 [TESTING.md](docs/TESTING.md)。

## 直接读取 Innovus 数据库

可在已加载的 Innovus 中使用 `innovus/export_flow.tcl` 按实例根、depth、独立展开和停止路径导出，再运行 `analyze.py innovus-flow 导出目录 --out 新目录`，复用相同 DOT/SVG/HTML 渲染。详见 [INNOVUS_USAGE.md](INNOVUS_USAGE.md)。

轻量页面：flow.html 首屏仅含图形和统计；逐位证据改存 flow_details.js，展开时按需读取。完整 flow.json 保持不变。移动报告请保留这些同目录文件；若预览无法读取明细，可在页面选择本图的 flow.json 并校验加载。
