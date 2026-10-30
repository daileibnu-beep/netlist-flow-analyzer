# 如何复测

## 1. 无 EDA 的自动测试

在项目目录执行，确保 Python 3.9+ 和 Graphviz 可用：

```bash
python3 -m unittest discover -s tests -v
```

33 项测试覆盖统一入口、普通/gzip 输入、失败不发布、实例隔离、方向桥接、重排和升降序总线、独立深度、stop、子树根、常量/未知/inout、反馈、快照校验、聚合与 SVG、HTML 明细逻辑。Node.js 可用时运行 HTML 内存 DOM 测试，否则该项明确跳过。此测试不等同浏览器排版或 Codex 宿主预览器验收。

## 2. 在真实 Innovus 中跑自编小网表

此脚本会调用 `init_design` 并在结束时 `exit`，**只在新开的空白批处理会话执行**，不要 source 到已有工程会话。测试只导入自编 LEF 和 Verilog，不需要真实工艺库，不修改现有设计。

```bash
# 输出目录须不存在；按机器实际安装路径选择 Innovus
FLOW_TEST_OUTPUT="$PWD/build/native_outputs" \
  innovus -no_gui -files tests/eda/run_innovus.tcl \
  -log build/native_test.log
```

运行前可 `mkdir -p build`，但不要预建 `build/native_outputs`。所有测试是否通过，以 `native_case_status.tsv` 每项 PASS 和下一步 verifier 的退出码为准，不能仅凭 Innovus 正常退出判断。

成功路径为 9 种：top、depth2、depth3、mixed、subroot、stop、special_root、nested、include_empty。另验证错误 root、越界展开、stop 冲突、非法 depth、三种资源超限、拒绝覆盖与导出前后实例/net 数不变，共 18 项检查。

输入刻意覆盖复用模块类型、转义点号/方括号、名称中的 `/` 和 `~`、总线方向、拼接重排、名称带 `[0]` 的真实标量、空模块、纯 assign 包装、常量、inout 和悬空端口。

## 3. 独立对照和完整输出链路

在 EDA Linux 或复制快照后的 Mac 上执行：

```bash
python3 tests/eda/verify_exports.py build/native_outputs \
  --out build/native_verification
```

验证器独立用 Verilog 引擎建立参考集合，对每个范围比较完整 `(源实例, 源端口, 源位, 目的实例, 目的端口, 目的位)` 集合，并实际执行 9 次 `analyze.py innovus-flow`，核对图形数量、Graphviz SVG 对象 ID、HTML 内嵌 SVG 一致性。结果在 `summary.json`，`all_pass` 必须为 true；差异写入各例 `*_differences.json`。

两引擎默认空模块策略不同。对照时明确将 assign-only `passthrough` 作为折叠边界，参考集合仅保留 native 可见节点；不跨越被保守排除的模块，不把两种不同边界的全图总数误作比较。`include_empty` 另行覆盖保留空边界的情况。

## 4. 在自己的大设计中复测

已有 Innovus 会话只 source `innovus/export_flow.tcl`，导出 top、一个子树根、top 中单实例展开三种范围，具体命令见主 README。不要运行上述自带 `init_design` 的测试驱动。

保留导出前后的实例与 net 数，检查 diagnostics；每个展开范围应具有相同的模块边界和空模块策略，再与对应 Verilog 参考逐位比对。不能只比较汇总条数，也不能把 physical-only 导入的成功称为时序/功能验证。大规模实例总数不代表全部标准单元均被展开追踪。

本次已执行的版本、数据规模与准确结果见 [EDA_TEST_REPORT.md](EDA_TEST_REPORT.md)。
