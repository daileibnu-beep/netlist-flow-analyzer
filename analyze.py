#!/usr/bin/env python3
"""统一入口：网表清点 → 边界连接 → 派生结果 → 离线图册（Python 3.9+ 标准库）。"""
import argparse
import contextlib
import datetime
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))


class Pipeline:
    def __init__(self, work, source, top):
        self.work = work
        self.manifest = {'schema_version': 1, 'status': 'running',
                         'started_at': datetime.datetime.now().astimezone().isoformat(),
                         'source': str(source), 'top': top, 'python': sys.version.split()[0],
                         'stages': [], 'limitations': [
                             '结构网表子集，非完整 Verilog elaboration。',
                             '无定义 leaf 不穿越；连接数是边界端口位配对数。',
                             '未读取 SDC/Liberty，无功能、时序或物理验证。']}
        (work / 'logs').mkdir()
        self.save()

    def save(self):
        from mempool_profile import write_json
        write_json(self.work / 'run_manifest.json', self.manifest)

    def step(self, name, function, *args, **kwargs):
        print('[{}] {}'.format(len(self.manifest['stages']) + 1, name), flush=True)
        stage = {'name': name, 'status': 'running',
                 'log': 'logs/{:02d}.log'.format(len(self.manifest['stages']) + 1)}
        self.manifest['stages'].append(stage)
        self.save()
        started = time.monotonic()
        try:
            with (self.work / stage['log']).open('w', encoding='utf-8') as stream:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    try:
                        value = function(*args, **kwargs)
                    except Exception:
                        traceback.print_exc()
                        raise
            stage['status'] = 'complete'
            return value
        except Exception as error:
            stage['status'] = 'failed'
            stage['error'] = str(error)
            self.manifest['status'] = 'failed'
            raise
        finally:
            stage['elapsed_seconds'] = round(time.monotonic() - started, 3)
            self.save()


def check_connectivity(bound, result, expected):
    problems = []
    if len(result['instances']) != expected:
        problems.append('selected hierarchy count differs from inventory')
    for key in ('parse_failures', 'width_errors'):
        if result[key]:
            problems.append('{}={}'.format(key, len(result[key])))
    if problems:
        print(json.dumps({key: result[key] for key in ('parse_failures', 'width_errors')},
                         ensure_ascii=False, indent=2))
        raise ValueError('边界分析覆盖检查失败：' + '; '.join(problems))
    return bound, result


def run(args):
    from scan_inventory import scan_inventory
    from analyze_connectivity import analyze_file
    from mempool_profile import TOP, TILE0, derive_inventory, derive_dma_groups, validate_profile
    from report_netlist import build_report

    source = args.source.expanduser().resolve()
    output = args.out.expanduser().resolve()
    if not source.is_file():
        raise ValueError('输入网表不存在：' + str(source))
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录：' + str(output))
    if not args.analysis_only and args.top != TOP:
        raise ValueError('完整图册仅支持 mempool_group；其他顶层请加 --analysis-only。')
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=output.name + '.working-', dir=str(output.parent)))
    pipeline = Pipeline(work, source, args.top)
    results = work / 'results'
    try:
        inventory = pipeline.step('清点网表并建立模块索引', scan_inventory, source,
                                  results / 'inventory.json', args.top,
                                  write_index=results / 'module_index.json')
        pipeline.manifest['sha256_uncompressed'] = inventory['summary']['uncompressed_source_sha256']
        summary = inventory['summary']
        if (summary['unsupported'] or summary['positional_instances'] or
                summary['header_mismatches'] or summary['duplicate_instance_names']):
            pipeline.manifest['inventory_diagnostics'] = {
                key: summary[key] for key in ('unsupported', 'positional_instances',
                                              'header_mismatches', 'duplicate_instance_names')}
            if not args.analysis_only:
                raise ValueError('存在结构清点异常，不能生成固定示例图册；请检查 inventory.json。')
        pipeline.step('生成摘要和顶层模块清单', derive_inventory, inventory, results)
        if not args.analysis_only:
            pipeline.step('核对 MemPool 报告适用的网表版本', validate_profile, results)
        netlist = results / source.stem if source.suffix == '.gz' else source

        def boundary(module, graph_name, bindings_name):
            bound, graph = analyze_file(netlist, results / 'module_index.json', module,
                                        results / graph_name, results / bindings_name)
            return check_connectivity(bound, graph, len(inventory['modules'][module]['children']))

        _, connectivity = pipeline.step('分析顶层直接边界连接', boundary, args.top,
                                         'connectivity.json', 'top_instances_connections.json')
        if not args.analysis_only:
            pipeline.step('分析 tile0 内部边界', boundary, TILE0,
                          'tile0_connectivity.json', 'tile0_bindings.json')
            pipeline.step('从实际握手边提取 DMA 分组', derive_dma_groups, connectivity, results)
        for number, module in enumerate(dict.fromkeys(args.inspect), 1):
            if module not in inventory['modules']:
                raise ValueError('额外分析的模块不存在：' + module)
            pipeline.step('额外边界：' + module, boundary, module,
                          'extra_{:02d}_connectivity.json'.format(number),
                          'extra_{:02d}_bindings.json'.format(number))
        if not args.analysis_only:
            pipeline.step('生成交互报告和全部 SVG', build_report, results, work / 'visual_report')
        pipeline.manifest.update(status='complete', output=str(output),
                                 finished_at=datetime.datetime.now().astimezone().isoformat())
        pipeline.save()
        # Logs record the final published paths, so they remain useful after rename.
        for log in (work / 'logs').glob('*.log'):
            log.write_text(log.read_text(encoding='utf-8').replace(str(work), str(output)), encoding='utf-8')
        if output.exists():
            raise ValueError('输出目录在运行期间被创建，保留临时结果，请另选目录。')
        work.rename(output)
    except Exception as error:
        pipeline.manifest.update(status='failed', error=str(error))
        pipeline.save()
        raise ValueError('{}\n诊断与中间结果已保留：{}'.format(error, work)) from error
    print('完成：' + str(output))
    if not args.analysis_only:
        print('打开报告：' + str(output / 'visual_report' / 'report.html'))
    else:
        print('连接结果：' + str(output / 'results' / 'connectivity.json'))



def run_flow(args):
    """Build a generic cross-hierarchy graph from a fresh source-derived index."""
    from hierarchical_flow import ModuleReader, HierarchyFlow, write_outputs
    from scan_inventory import index_modules
    import gzip
    import hashlib
    import shutil

    from render_module_graph import graphviz_executable
    graphviz_executable(args.dot)
    source, output = args.source.expanduser().resolve(), args.out.expanduser().resolve()
    if not source.is_file():
        raise ValueError('输入网表不存在：' + str(source))
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=output.name + '.working-', dir=str(output.parent)))
    pipeline = Pipeline(work, source, args.top)
    pipeline.manifest.update(command='flow', root=args.root, depth=args.depth,
                             expand=args.expand, expand_depth=args.expand_depth, expand_at=args.expand_at, stop=args.stop)
    try:
        netlist = source
        if source.suffix == '.gz':
            netlist = work / 'source' / source.stem
            netlist.parent.mkdir()
            def decompress():
                with gzip.open(source, 'rb') as compressed, netlist.open('wb') as dest:
                    shutil.copyfileobj(compressed, dest, 1024 * 1024)
            pipeline.step('解压原网表', decompress)
        def index_source():
            index, byte_count, line_count = index_modules(netlist)
            if not index:
                raise ValueError('No supported module definitions found in netlist')
            digest = hashlib.sha256()
            with netlist.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            from mempool_profile import write_json
            write_json(work / 'module_index.json', index)
            pipeline.manifest['sha256_uncompressed'] = digest.hexdigest()
            return index, byte_count, line_count
        index, byte_count, line_count = pipeline.step('重新建立模块索引并计算来源校验值', index_source)
        reader = ModuleReader(netlist, index)
        engine = HierarchyFlow(reader, args.top, root=args.root, depth=args.depth,
                                expand=args.expand, stop=args.stop,
                                max_instances=args.max_instances, max_bits=args.max_bits,
                                max_pairs=args.max_pairs, expand_depth=args.expand_depth,
                                expand_at=args.expand_at)
        graph = pipeline.step('按实例路径展开并拼接跨层端口位', engine.graph)
        graph['source'] = {'path': str(source), 'sha256_uncompressed': pipeline.manifest['sha256_uncompressed'],
                           'source_bytes': byte_count, 'source_lines': line_count}
        pipeline.step('生成模块汇总 DOT、SVG 和离线连接图', write_outputs, graph, work,
                      dot_program=args.dot, minimum=args.min_connections, rankdir=args.rankdir,
                      render_timeout=args.render_timeout)
        pipeline.manifest.update(status='complete', output=str(output), coverage=graph['coverage'],
                                 finished_at=datetime.datetime.now().astimezone().isoformat())
        pipeline.save()
        if output.exists():
            raise ValueError('输出目录在运行期间被创建，保留临时结果。')
        work.rename(output)
    except Exception as error:
        pipeline.manifest.update(status='failed', error=str(error))
        pipeline.save()
        raise ValueError('{}\n诊断与中间结果已保留：{}'.format(error, work)) from error
    print('完成：' + str(output / 'flow.html'))
    print(json.dumps(graph['coverage'], ensure_ascii=False))
    return 0


def redraw_flow(args):
    """Rebuild the module graph from saved analysis without reopening the netlist."""
    from render_module_graph import write_outputs
    source, output = args.data.expanduser().resolve(), args.out.expanduser().resolve()
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录：' + str(output))
    graph = json.loads(source.read_text(encoding='utf-8'))
    summary = write_outputs(graph, output, dot_program=args.dot, minimum=args.min_connections,
                            rankdir=args.rankdir, render_timeout=args.render_timeout)
    print('完成：' + str(output / 'flow.html'))
    return 0


def run_innovus_flow(args):
    """Read a completed Innovus snapshot and use the existing flow renderer."""
    from innovus_flow import build_graph
    from render_module_graph import graphviz_executable, write_outputs
    graphviz_executable(args.dot)
    source, output = args.export.expanduser().resolve(), args.out.expanduser().resolve()
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=output.name + '.working-', dir=str(output.parent)))
    pipeline = Pipeline(work, source, None)
    pipeline.manifest.update(command='innovus-flow', limitations=[
        '读取已完成的 Innovus 数据库导出；不重新解析 Verilog。',
        '连接采用 hNet 和有向父子端口桥接；不穿越折叠模块或标准单元。'])
    try:
        graph = pipeline.step('校验 Innovus 导出并拼接跨层端口位', build_graph, source,
                              max_pairs=args.max_pairs, max_bits=args.max_bits, max_rows=args.max_rows)
        pipeline.manifest.update(top=graph['top'], root=graph['root'], depth=graph['depth'],
                                 source_hashes=graph['data_source']['files_sha256'],
                                 limitations=graph['limitations'])
        pipeline.step('生成模块汇总 DOT、SVG 和离线连接图', write_outputs, graph, work,
                      dot_program=args.dot, minimum=args.min_connections, rankdir=args.rankdir,
                      render_timeout=args.render_timeout)
        pipeline.manifest.update(status='complete', output=str(output), coverage=graph['coverage'],
                                 finished_at=datetime.datetime.now().astimezone().isoformat())
        pipeline.save()
        if output.exists():
            raise ValueError('输出目录在运行期间被创建，保留临时结果。')
        work.rename(output)
    except Exception as error:
        pipeline.manifest.update(status='failed', error=str(error)); pipeline.save()
        raise ValueError('{}\n诊断与中间结果已保留：{}'.format(error, work)) from error
    print('完成：' + str(output / 'flow.html'))
    print(json.dumps(graph['coverage'], ensure_ascii=False))
    return 0


def add_graph_render_arguments(parser):
    parser.add_argument('--dot', type=Path, help='Graphviz dot 路径，默认从 PATH 查找')
    parser.add_argument('--min-connections', type=int, default=1, help='只画位连接数达到该值的模块边，默认全部保留')
    parser.add_argument('--rankdir', choices=('LR', 'TB'), default='LR', help='图的布局方向')
    parser.add_argument('--render-timeout', type=float, default=60, help='Graphviz 渲染时间上限，单位秒')


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='例：\n  python3 analyze.py run /path/mempool_group.v.gz --out build\n'
                                            '  python3 analyze.py report --results results --out visual_report\n'
                                            '  python3 analyze.py run /path/chip.v --top chip --analysis-only --out chip_analysis')
    commands = parser.add_subparsers(dest='command', required=True)
    full = commands.add_parser('run', help='从 .v/.v.gz 一次完成分析与报告')
    full.add_argument('source', type=Path)
    full.add_argument('--out', type=Path, default=Path('analysis_output'), help='新建输出目录，拒绝覆盖已有目录')
    full.add_argument('--top', default='mempool_group')
    full.add_argument('--analysis-only', action='store_true', help='只做结构清点和边界连接，可用于其他顶层')
    full.add_argument('--inspect', action='append', default=[], metavar='MODULE', help='追加一个内部模块边界分析，可重复')
    full.set_defaults(handler=run)
    flow = commands.add_parser('flow', help='按实例路径拼接跨层次结构连接图')
    flow.add_argument('source', type=Path)
    flow.add_argument('--top', default='mempool_group', help='设计顶层 module 类型名')
    flow.add_argument('--root', help='分析根实例路径，默认顶层；路径以 / 分层')
    flow.add_argument('--depth', type=int, default=1, help='默认展开深度；1=根的直接子实例作为图节点')
    flow.add_argument('--expand', action='append', default=[], metavar='INSTANCE_PATH', help='展开指定实例，深度由 --expand-depth 决定，可重复')
    flow.add_argument('--expand-depth', type=int, default=1, help='每个 --expand 实例的相对展开深度，1=显示其直接子实例')
    flow.add_argument('--expand-at', nargs=2, action='append', default=[], metavar=('INSTANCE_PATH', 'DEPTH'), help='分别指定一个实例及其相对深度，可重复')
    flow.add_argument('--stop', action='append', default=[], metavar='INSTANCE_PATH', help='在指定实例停止展开，可重复')
    flow.add_argument('--max-instances', type=int, default=200000, help='遇到的实例数量上限，含未知 leaf')
    flow.add_argument('--max-bits', type=int, default=2000000, help='内部位连接图顶点数量上限')
    flow.add_argument('--max-pairs', type=int, default=200000, help='输出的源位—目的位配对数量上限')
    flow.add_argument('--out', type=Path, default=Path('flow_output'), help='新建输出目录，拒绝覆盖')
    add_graph_render_arguments(flow)
    ivflow = commands.add_parser('innovus-flow', help='读取独立 Tcl 导出的 Innovus 数据库连接，生成同款 DOT/SVG/HTML')
    ivflow.add_argument('export', type=Path, help='含 manifest.json 的完整 Tcl 导出目录')
    ivflow.add_argument('--out', type=Path, required=True, help='新的图形输出目录，拒绝覆盖')
    ivflow.add_argument('--max-pairs', type=int, default=200000)
    ivflow.add_argument('--max-bits', type=int, default=2000000)
    ivflow.add_argument('--max-rows', type=int, default=5000000, help='导出表累计行数上限')
    add_graph_render_arguments(ivflow)
    redraw = commands.add_parser('redraw', help='从已有 flow.json 重画模块汇总 DOT/SVG/HTML，无需重新解析网表')
    redraw.add_argument('--data', type=Path, required=True)
    redraw.add_argument('--out', type=Path, required=True, help='新的输出目录')
    add_graph_render_arguments(redraw)
    for command, help_text in [('report', '从已有结果重建完整离线报告'),
                                ('figures', '从已有结果重画 MemPool SVG'),
                                ('viz', '生成报告数据 JSON')]:
        item = commands.add_parser(command, help=help_text)
        item.add_argument('--results', type=Path, default=ROOT / 'results')
        item.add_argument('--out', type=Path, required=True)
    matrix = commands.add_parser('matrix', help='从可视化数据画有向连接矩阵 SVG')
    matrix.add_argument('--data', type=Path, required=True)
    matrix.add_argument('--out', type=Path, required=True)
    inventory = commands.add_parser('inventory', help='单独清点网表、写模块索引')
    inventory.add_argument('source', type=Path)
    inventory.add_argument('--top', default='mempool_group')
    inventory.add_argument('--out', type=Path, required=True)
    inventory.add_argument('--write-index', type=Path)
    connect = commands.add_parser('connect', help='单独分析一个模块边界；索引须对应同一原文')
    connect.add_argument('--netlist', '--source', type=Path, required=True)
    connect.add_argument('--index', type=Path, required=True)
    connect.add_argument('--module', default='mempool_group')
    connect.add_argument('--out', type=Path, required=True)
    connect.add_argument('--bindings', type=Path, required=True)
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return run(args)
        if args.command == 'flow':
            return run_flow(args)
        if args.command == 'redraw':
            return redraw_flow(args)
        if args.command == 'innovus-flow':
            return run_innovus_flow(args)
        if args.command == 'inventory':
            from scan_inventory import scan_inventory
            scan_inventory(args.source, args.out, args.top, write_index=args.write_index)
        elif args.command == 'connect':
            from analyze_connectivity import analyze_file
            _, result = analyze_file(args.netlist, args.index, args.module, args.out, args.bindings)
            print(json.dumps({'module': args.module, 'parse_failures': len(result['parse_failures']),
                              'width_errors': len(result['width_errors'])}, ensure_ascii=False))
        elif args.command == 'report':
            from report_netlist import build_report
            build_report(args.results, args.out)
        elif args.command == 'figures':
            from render_netlist_figures import build_figures
            build_figures(args.results, args.out)
        elif args.command == 'viz':
            from viz_data import build_data
            build_data(args.results, args.out)
        elif args.command == 'matrix':
            from render_matrix_svg import render
            render(json.loads(args.data.read_text(encoding='utf-8')), args.out)
    except (OSError, ValueError, KeyError, AssertionError, RecursionError) as error:
        print('错误：' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
