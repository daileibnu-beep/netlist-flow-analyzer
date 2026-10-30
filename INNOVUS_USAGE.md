# 从 Innovus 数据库生成模块连接图

在已加载设计的 Innovus 中读取连接数据，再用现有 Python 入口生成同款 `flow.dot`、`flow.svg` 和 `flow.html`。独立导出器位于 `innovus/export_flow.tcl`，只调用 `dbGet` / `getVersion`，不改变网表、布局、约束或 GUI；不要求正在显示模块连线，也不依赖之前的 GUI 控制脚本。

适用接口：Innovus Legacy `dbGet`，已实测 23.14-s088_1。Python 端只用标准库，渲染需要 Graphviz `dot`。

## 1. 在 Innovus 中导出

先把 `innovus/export_flow.tcl` 放到运行 Innovus 的 Linux 上，然后在 Innovus 控制台执行。路径含 `[]` 时用 Tcl 花括号，避免命令替换。

```tcl
source /path/mempool_offline/innovus/export_flow.tcl
::ivflow::export -help

# 顶层直接子模块，不继续展开
::ivflow::export -out /tmp/chip_top -depth 1

# 以指定实例为分析根，只看其内部一层；不包含根外上下文
::ivflow::export -out /tmp/group2_only \
    -root {mempool_cluster/gen_groups[2].i_group} -depth 1

# 保留其他顶层模块，只将 group2 额外展开一层，拼接跨层连接
::ivflow::export -out /tmp/group2_mixed -depth 1 \
    -expand {mempool_cluster/gen_groups[2].i_group} 1

# 多个实例可以分别控制展开深度
::ivflow::export -out /tmp/chip_selected -depth 1 \
    -expand {top/u_axi} 1 -expand {top/u_dma} 2 \
    -stop {top/u_dma/u_queue}
```

- `-root`：分析根，默认设计顶层。路径可含或不含 top 前缀，须指向层次模块 hInst，不接受标准单元或 RAM 宏实例。
- `-depth N`：相对分析根的深度，1 表示根的直接子模块作为节点，范围 1–64。
- `-expand PATH N`：从指定实例起额外展开 N 层，可以重复；为到达该实例而展开所需祖先。嵌套选择以最近一项的深度为准。
- `-stop PATH`：在此实例停止，可以重复；不能是根，也不能与其下的 `-expand` 冲突。
- `/` 分隔实例层次，点号和方括号是实例名的一部分。名字中本身的 `/` 写成 `~1`，本身的 `~` 写成 `~0`。不进行通配符匹配。
- 默认保留没有子模块、但包含标准单元或宏的末级模块为节点，不推断其内部功能。
- `-empty_policy exclude`（默认）：保守排除没有任何子实例的模块，其可能是缺失定义的空模块，也可能是 assign-only 模块；数据库接口不能可靠判别其定义与方向来源，原因会写入诊断。
- `-empty_policy include`：经自行核实后允许保留空模块的边界端口，输出页面会提示方向需核实。该选项不会推断模块的输入输出功能。
- `-max_instances` 默认 200000；`-max_endpoints` 和 `-max_nets` 默认各 2000000。限额针对所选范围，避免误展开整个大设计。

输出目录必须不存在。导出先写临时目录，所有表写完后才生成 `manifest.json` 并发布最终目录；失败保留临时数据用于诊断，不冒充完整导出。

## 2. 接入 Python 主入口

将整个导出目录复制到运行 Python 的机器，保持目录内文件完整：

```bash
python3 analyze.py innovus-flow /path/group2_mixed \
  --out /path/group2_graph

# Mac 上可显式指定 Graphviz
python3 analyze.py innovus-flow /path/group2_mixed \
  --out /path/group2_graph --dot /opt/homebrew/bin/dot
```

路径、展开深度和空模块策略在 Tcl 导出时确定。Python 不重新连接 Innovus，也不重新解析 Verilog；如需改变范围，请重新导出。

图形参数与原有 `flow` / `redraw` 相同：`--rankdir LR|TB`、`--min-connections N`、`--render-timeout SECONDS`。默认保留全部连接；显式过滤时显示隐藏数量，完整逐位 JSON 保留全部连接。Python 限额为 `--max-pairs 200000`、`--max-bits 2000000`、`--max-rows 5000000`。

输出仍为：

- `flow.json`：完整端口位配对、桥接、示例路径与诊断。
- `flow_modules.json`：按有向模块对合并的连接数量。
- `flow.dot` / `flow.svg`：模块间一条同向连线，数量标注、按数量显示粗细。
- `flow.html`：轻量页面，直接嵌入相同 SVG，保留缩放、模块定位与连接数量。
- `flow_details.js`：只有展开端口 / 逐位证据时才读取。移动报告请保留同目录文件；预览若阻止读取，可在页面选择原 flow.json，SHA-256 校验通过后读取。
- `run_manifest.json` / `logs/`：输入文件 SHA-256、运行状态与失败信息。

原有 `analyze.py flow chip.v ...` 继续读取结构网表，二者共用渲染器，不共用连接数据解析。

## 数据与连接口径

快照由 `manifest.json` 和 `nodes.tsv`、`scopes.tsv`、`ports.tsv`、`bridges.tsv`、`hnets.tsv`、`diagnostics.tsv` 组成，格式版本为 `innovus-hier-flow` v1。表头是固定 TSV 字段；每个数据字段是 JSON 字符串，以支持中文、空格、引号、反斜杠和特殊字符。不要用普通 CSV 的双引号规则解析这些表。Python 会验证完成标志、版本、字节数、行数、对象引用、层次及位信息。

- 用 hInstTerm 的 `term.bus` 区分标量 / 向量位，避免把名称末尾含 `[0]` 的标量误作总线。
- 根输入和模块输出作为源，根输出和模块输入作为汇。
- 展开模块的输入桥为 `upHNet → downHNet`，输出桥反向。
- 使用各层的 hNet 和显式有向桥接；不穿越折叠模块、标准单元或寄存器，不把常量共用成一个信号网络。inout / unknown、P/G、常量和未连接端口跳过并记录。
- flat canonical net 仅作原始证据，不用来短接不同 hNet，防止透过折叠模块的内部 alias 造成假通路。如果某个版本把同层 assign 保留成不同 hNet，本实现不只凭 flat net 相同就推断连通，需要另行核实。
- 数量是源端口位—目的端口位的配对数，不是独立 net 数、带宽或流量。同一 net 多个源 / 汇会产生多个配对；多驱动会记录为诊断。
- 没有连线不证明不存在功能通路；没有执行功能等价、时序或物理签核验证。
