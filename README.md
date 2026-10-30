# Netlist Flow Analyzer · 跨层次模块连接分析

从结构 Verilog 或已加载的 Innovus 数据库提取 module port 的连接关系。可以指定分析根、独立展开某个实例及展开深度，将父子层次的端口绑定拼接成同一张模块连接图。

图中每对模块的同向连接合并为一条箭头，标注**端口位配对数量**，连接越多线越粗；不逐条绘制信号线。`flow.html` 直接内嵌 Graphviz 从 `flow.dot` 生成的 SVG，支持缩放、平移、模块定位和按需查看连接证据。

这是一张结构连接图：折叠模块的输入输出之间不推断功能通路，不执行时序或物理签核。

## 环境准备

| 组件 | 要求 |
| --- | --- |
| Python | 3.9+，运行只依赖标准库，无须 pip 安装 |
| Graphviz | `dot` 在 PATH 中，或使用 `--dot /path/to/dot` |
| Innovus | 仅数据库导出需要；已实测 23.14-s088_1 的 Legacy `dbGet` |
| Node.js | 可选，仅用于运行 HTML 交互逻辑测试 |

将本目录完整放在同一位置，进入目录后运行：

```bash
python3 --version
dot -V
python3 analyze.py --help
```

EDA 机器可能将 `python3` 指向工具自带的旧版本。请先确认版本，必要时将下文的 `python3` 替换为 `/usr/bin/python3.11` 等实际路径。

## 快速开始：读取网表

以下例子使用仓库附带的自编小网表，可直接运行；每次 `--out` 必须使用不存在的新目录。

```bash
# 顶层直接子模块
python3 analyze.py flow tests/eda/fixtures/synthetic.v \
  --top fixture_top --depth 1 --out build/top

# 其他顶层模块保持折叠，只展开 gen[2].u_group 一层
python3 analyze.py flow tests/eda/fixtures/synthetic.v \
  --top fixture_top --depth 1 \
  --expand-at 'fixture_top/gen[2].u_group' 1 --out build/mixed

# 以指定实例为根，只看其内部
python3 analyze.py flow tests/eda/fixtures/synthetic.v \
  --top fixture_top --root 'fixture_top/gen[2].u_group' \
  --depth 1 --out build/subroot
```

在浏览器打开输出目录中的 `flow.html`。macOS 可运行 `open build/mixed/flow.html`。自己的网表用相同命令替换输入文件和 top；支持 `.v` 和 `.v.gz`。

解析器支持**已展开、非 ANSI 端口声明的结构 Verilog 子集**。不负责 RTL elaboration、宏预处理或行为逻辑综合。被展开作用域中不支持的语句、表达式或位宽错误会报错。需要直接使用工具数据库时采用下一种方式。

## 快速开始：读取 Innovus

设计须已在 Innovus 中加载。将 `innovus/export_flow.tcl` 放到 EDA 机器，在 Innovus 控制台执行：

```tcl
source /path/netlist-flow-analyzer/innovus/export_flow.tcl
::ivflow::export -help

# 只分析 top level
::ivflow::export -out /path/exports/chip_top -depth 1

# 只将 u_axi 展开一层，保留其他顶层实例
::ivflow::export -out /path/exports/axi_mixed -depth 1 \
    -expand {chip/u_axi} 1

# 只看指定实例内部
::ivflow::export -out /path/exports/axi_only \
    -root {chip/u_axi} -depth 1
```

`chip/u_axi` 请换成真实实例路径。含 `[]` 的 Tcl 路径必须用花括号包住。导出器只读取数据库，不改变网表、布局、约束或 GUI。

将**整个导出目录**复制到 Python 所在机器，运行：

```bash
python3 analyze.py innovus-flow /path/exports/axi_mixed \
  --out build/axi_graph
```

Python 导入阶段不依赖 Innovus，也不重新解析 Verilog。修改展开范围时，需要重新执行 Tcl 导出。

## 层次和 depth 如何设置

| 意图 | Verilog `flow` | Innovus Tcl |
| --- | --- | --- |
| 顶层直接子模块 | `--depth 1` | `-depth 1` |
| 所有分支再深入一层 | `--depth 2` | `-depth 2` |
| 仅展开 u_axi 一层 | `--expand-at 'chip/u_axi' 1` | `-expand {chip/u_axi} 1` |
| 多个实例用不同深度 | 重复 `--expand-at PATH N` | 重复 `-expand PATH N` |
| 将 u_axi 当作分析根 | `--root 'chip/u_axi'` | `-root {chip/u_axi}` |
| 在某实例保持折叠 | `--stop 'chip/u_axi/u_fifo'` | `-stop {chip/u_axi/u_fifo}` |

深度范围是 1–64。展开指定实例时，会打开到达它所需的祖先；内部更具体的展开设置控制自己的子树。`stop` 不能与其自身或内部的展开要求冲突，也不会主动展开祖先。

`root` 决定真正的分析边界。子树根的外部连接止于其 `$PORT`，不会再追到根外的其他模块。若想看 AXI 内部与其他顶层模块的连线，应保留共同祖先为根，只展开 AXI。

路径按 `/` 分层，`.` 和 `[]` 是实例名的字面字符。例如 `chip/gen_groups[2].i_group` 只有一个名为 `gen_groups[2].i_group` 的子层。标识符自身含 `/` 时写 `~1`，含 `~` 时写 `~0`；不写 Verilog 标识符前的转义反斜杠。Shell 路径用单引号，Tcl 路径用花括号，不作通配符匹配。

## 输出文件和读图方式

| 文件 | 内容 |
| --- | --- |
| `flow.dot` | 同向模块连接聚合后的 Graphviz 源码 |
| `flow.svg` | 直接渲染 DOT 得到的矢量图 |
| `flow.html` | 内嵌同一份 SVG 的交互页面 |
| `flow_details.js` | 按需读取的端口对及逐位证据 |
| `flow.json` | 完整结构分析结果、位映射、桥接和诊断 |
| `flow_modules.json` | 模块对汇总、数量和显示设置 |
| `flow_render.json` / `flow_graphviz.log` | 渲染版本、设置和诊断 |
| `run_manifest.json` / `logs/` | 运行状态、输入指纹与阶段日志 |

`flow` 还生成原文模块索引；gzip 输入会保留解压副本。Innovus 原始快照另有 `manifest.json` 和六张 TSV 表，格式说明见 [INNOVUS_USAGE.md](INNOVUS_USAGE.md)。

边上的数量是**唯一源端口位—目的端口位配对的数量**，不是独立 net 数、总线条数、带宽或流量。一个 net 驱动两个目的端口会分别计数；相反方向分别画箭头。`$PORT` 表示分析根的端口边界。线宽按当前图的连接数量缩放到 1–7 pt。

HTML 首屏只加载图形和统计，点击明细时再读取 `flow_details.js`。复制报告时保留同目录文件；若预览器阻止读取，可在页面选择该图原始 `flow.json`，页面校验 SHA-256 后加载。页面逻辑有自动测试，宿主预览器的兼容性需以实际打开结果为准。

## 重画、过滤和资源控制

```bash
# 不重新分析网表，只改变图形布局
python3 analyze.py redraw --data build/mixed/flow.json \
  --rankdir TB --out build/mixed_vertical

# 只显示至少 10 个位配对的模块边
python3 analyze.py redraw --data build/mixed/flow.json \
  --min-connections 10 --out build/mixed_filtered
```

`flow`、`innovus-flow`、`redraw` 共用 `--dot`、`--rankdir LR|TB`、`--render-timeout SECONDS` 和 `--min-connections N`。默认保留全部连接；显式过滤时页面显示隐藏数量，完整 `flow.json` 保留原数据。重画不能改变分析层次。

默认保护上限：Python 层次分析实例 200000、位对象 2000000、端口对 200000；Innovus 导出实例 200000、端点 2000000、hNet 2000000；快照导入行数 5000000。超限会报错。先收窄 root 或 depth，确有需要再提高相应参数，详见子命令 `--help` 或 Tcl `-help`。

成功结果才发布到最终目录。失败保留 `*.working-*` 临时目录与日志；不要把失败目录当作完整结果导入。已有输出目录不会被覆盖。

## 其他入口及目录

通用跨层次图请用 `flow` 或 `innovus-flow`。旧的 `run` 完整图册是指定 MemPool 样例的专用报告，受输入 SHA-256 检查保护，不能套用到任意网表；`run --analysis-only` 保留旧边界分析流程。

| 位置 / 命令 | 用途 |
| --- | --- |
| `analyze.py` | 统一入口 |
| `scripts/hierarchical_flow.py` | Verilog 层次选择、按位绑定及追踪 |
| `innovus/export_flow.tcl` | 独立只读数据库导出器 |
| `scripts/innovus_flow.py` | 快照校验和有向 hNet 追踪 |
| `scripts/render_module_graph.py`、`flow_template.html` | 共用聚合、Graphviz 和 HTML |
| `tests/` | 单元与集成测试；`tests/eda/` 为真实 Innovus 回归 |
| `inventory`、`connect` | 原始清点和单模块边界分析 |
| `report`、`figures`、`viz`、`matrix` | 既有 MemPool 报告的派生输出 |

进一步阅读：[Verilog 详细用法](FLOW_USAGE.md)、[Innovus 详细用法](INNOVUS_USAGE.md)、[连接算法](docs/ARCHITECTURE.md)、[复测步骤](docs/TESTING.md)、[本次 EDA 测试结果](docs/EDA_TEST_REPORT.md)。

## 已知边界

- 不推断标准单元、寄存器或折叠模块内部的功能通路；未知 leaf、inout、常量等会记录诊断。因此“图中无边”不能证明无功能连接。
- Innovus 默认保守排除无子实例模块，其中可能包含缺失定义或 assign-only 包装。确认方向后可用 `-empty_policy include` 保留边界，但仍不推断输入到输出的功能关系。
- Innovus 只沿各层 hNet 和显式方向桥接追踪，不凭同一个 flat canonical net 就短接不同 hNet。若工具版本把同层 assign 保留成不同 hNet，可能缺少这部分连通，需要独立核实。
- 很大的扁平网表不一定有可读的 module 层次。此前约 117 万实例的扁平样例在 120 秒内未完成 `flow`，不宣称任意百万级设计都能完成。大规模验证的范围及结果见测试报告。
- 未附带 EDA 软件、授权、真实工艺库或大型公开网表。`tests/eda/fixtures` 是本项目自编的最小回归输入，不能用于物理实现或工艺验证。
