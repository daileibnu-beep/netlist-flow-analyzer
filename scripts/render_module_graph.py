"""Aggregate endpoint bit counts by directed module pair; render DOT once for HTML."""
import datetime
import hashlib
import json
import math
import shutil
import subprocess
import textwrap
from pathlib import Path


def graphviz_executable(program=None):
    value = str(program) if program else 'dot'
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError('未找到 Graphviz dot。请安装 Graphviz，或用 --dot 指定 dot 的路径。')
    return resolved


def aggregate_modules(graph, minimum=1):
    if minimum < 1:
        raise ValueError('--min-connections 必须至少为 1')
    known = {n['id'] for n in graph['nodes']}
    if len(known) != len(graph['nodes']):
        raise ValueError('Duplicate module node IDs')
    pairs = {}
    for edge in graph['edges']:
        key = (edge['source'], edge['sink'])
        if not set(key) <= known:
            raise ValueError('Edge refers to an unknown module node')
        count = edge['direct_bit_connections']
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError('Invalid source/sink bit connection count')
        if key not in pairs:
            pairs[key] = {'source': key[0], 'sink': key[1], 'bit_connections': 0, 'port_pairs': 0}
        pairs[key]['bit_connections'] += count
        pairs[key]['port_pairs'] += 1
    edges = [dict(value, svg_id='module_edge_{:04d}'.format(number))
             for number, (_, value) in enumerate(sorted(pairs.items()))
             if value['bit_connections'] >= minimum]
    maximum = max((e['bit_connections'] for e in edges), default=1)
    for edge in edges:
        ratio = math.sqrt((edge['bit_connections'] - 1) / (maximum - 1)) if maximum > 1 else 0
        edge['penwidth'] = round(1 + 6 * ratio, 3)
    nodes = [dict(node, svg_id='module_node_{:04d}'.format(i)) for i, node in enumerate(graph['nodes'])]
    original_total = sum(e['direct_bit_connections'] for e in graph['edges'])
    displayed_total = sum(e['bit_connections'] for e in edges)
    return {'schema_version': 1, 'unit': graph.get('connection_unit', '已核实端口位连接数'),
            'aggregation': 'one edge per directed source module / sink module pair',
            'minimum_connections': minimum, 'nodes': nodes, 'edges': edges,
            'summary': {'nodes': len(nodes), 'original_port_pair_edges': len(graph['edges']),
                        'all_module_pair_edges': len(pairs), 'displayed_module_pair_edges': len(edges),
                        'all_bit_connections': original_total, 'displayed_bit_connections': displayed_total,
                        'hidden_module_pair_edges': len(pairs) - len(edges),
                        'hidden_bit_connections': original_total - displayed_total},
            'metric_note': '各端口对的源位—目的位配对数之和；同一 net 可因不同端点重复计数。不是独立 net 数、带宽、流量或时序。',
            'width_note': '线宽按当前图数量的平方根相对缩放至 1–7 pt；数量越大，线越粗。双向关系分开累计并保留箭头。'}


def compact_label(node, root):
    if node.get('kind') == 'scope_boundary':
        return 'Top I/O' if len(node.get('path', [])) == 1 else 'Scope I/O'
    path = node.get('path') or node['id'].split('/')
    leaf = path[-1]
    lines = textwrap.wrap(leaf, width=30, break_long_words=True, break_on_hyphens=False) or [leaf]
    parent = node['id'].rsplit('/', 1)[0]
    if parent != root:
        relative = parent[len(root) + 1:] if parent.startswith(root + '/') else parent
        lines += textwrap.wrap('in ' + relative, width=30, break_long_words=True, break_on_hyphens=False)
    return '\n'.join(lines)


def module_dot(graph, aggregate, rankdir='LR'):
    if rankdir not in ('LR', 'TB'):
        raise ValueError('--rankdir 必须为 LR 或 TB')
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    lines = ['digraph flow {',
             'graph [rankdir=' + rankdir + ', bgcolor="#ffffff", pad="0.4", nodesep="0.5", ranksep="1.0", splines=polyline, overlap=false, outputorder=edgesfirst, fontname="Arial", fontsize=15, labelloc=t];',
             'node [shape=box, style="rounded,filled", fillcolor="#edf5fa", color="#71849a", fontcolor="#20364e", fontname="Arial", fontsize=13, margin="0.18,0.12", penwidth=1.1];',
             'edge [color="#456b85", fontcolor="#254b65", fontname="Arial", fontsize=11, arrowsize=0.8];',
             'label=' + quote('Module connections | edge labels: pin-bit connections | thicker = more') + ';']
    for node in aggregate['nodes']:
        tooltip = node['id'] + '\n' + node.get('module_type', '')
        attrs = ['id=' + quote(node['svg_id']), 'label=' + quote(compact_label(node, graph['root'])),
                 'tooltip=' + quote(tooltip)]
        if node.get('kind') == 'scope_boundary':
            attrs += ['fillcolor="#fff3db"', 'color="#b08b45"']
        lines.append('{} [{}];'.format(quote(node['id']), ', '.join(attrs)))
    for edge in aggregate['edges']:
        tooltip = '{} -> {}: {:,} pin-bit connections; {} port pairs'.format(
            edge['source'], edge['sink'], edge['bit_connections'], edge['port_pairs'])
        attrs = ['id=' + quote(edge['svg_id']), 'label=' + quote('{:,}'.format(edge['bit_connections'])),
                 'penwidth={:.3f}'.format(edge['penwidth']), 'tooltip=' + quote(tooltip)]
        lines.append('{} -> {} [{}];'.format(quote(edge['source']), quote(edge['sink']), ', '.join(attrs)))
    lines.append('}')
    return '\n'.join(lines) + '\n'


def inline_svg(svg):
    start = svg.find('<svg ')
    end = svg.rfind('</svg>')
    if start < 0 or end < start:
        raise ValueError('Graphviz 未生成有效 SVG')
    return svg[start:end + len('</svg>')]


def safe_json(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
            .replace('>', '\\u003e').replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def viewer_data(graph, flow_digest, details_digest):
    """Keep startup independent of potentially huge per-bit evidence arrays."""
    scopes = graph.get('expanded_scopes', [])
    data_source = graph.get('data_source', {})
    return {'top': graph.get('top', ''), 'root': graph['root'], 'depth': graph.get('depth', 1),
            'selection': graph.get('selection', {'expand': [], 'stop': []}),
            'coverage': graph.get('coverage', {}), 'limitations': graph.get('limitations', []),
            'expanded_scopes': scopes[:1000], 'expanded_scope_count': len(scopes),
            'diagnostics_counts': {k: len(v) if isinstance(v, (list, dict)) else v
                                   for k, v in graph.get('diagnostics', {}).items()},
            'data_source': {'kind': data_source.get('kind', 'netlist'), 'summary': data_source.get('summary', '')},
            'flow_json_sha256': flow_digest, 'details_fingerprint': details_digest,
            'details_url': 'flow_details.js?v=' + details_digest[:16]}


def write_outputs(graph, output, dot_program=None, minimum=1, rankdir='LR', render_timeout=60):
    """DOT and HTML share exactly the same Graphviz-generated SVG contents."""
    if render_timeout <= 0:
        raise ValueError('--render-timeout 必须大于 0')
    executable = graphviz_executable(dot_program)
    aggregate = aggregate_modules(graph, minimum)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    dot_source = module_dot(graph, aggregate, rankdir)
    dot_file, svg_file = output / 'flow.dot', output / 'flow.svg'
    dot_file.write_text(dot_source, encoding='utf-8')
    try:
        rendered = subprocess.run([executable, '-Tsvg', str(dot_file), '-o', str(svg_file)],
                                  capture_output=True, text=True, timeout=render_timeout)
    except subprocess.TimeoutExpired as error:
        raise ValueError('Graphviz 渲染超时；可提高 --render-timeout 或用 --min-connections 收窄显示。') from error
    (output / 'flow_graphviz.log').write_text(rendered.stderr, encoding='utf-8')
    if rendered.returncode:
        raise ValueError('Graphviz 渲染失败：' + rendered.stderr.strip()[:1000])
    svg = inline_svg(svg_file.read_text(encoding='utf-8'))
    analysis_text = json.dumps(graph, ensure_ascii=False, indent=2)
    flow_digest = hashlib.sha256(analysis_text.encode('utf-8')).hexdigest()
    details_digest = hashlib.sha256(safe_json(graph['edges']).encode('utf-8')).hexdigest()
    details = {'top': graph.get('top', ''), 'root': graph['root'],
               'fingerprint': details_digest, 'edges': graph['edges']}
    details_file = 'window.__FLOW_DETAILS__=' + safe_json(details) + ';\n'
    startup = viewer_data(graph, flow_digest, details_digest)
    template = Path(__file__).with_name('flow_template.html').read_text(encoding='utf-8')
    for token in ('__FLOW_DATA__', '__MODULE_DATA__', '__MODULE_SVG__'):
        if template.count(token) != 1:
            raise ValueError('Invalid flow template token: ' + token)
    document = (template.replace('__MODULE_SVG__', svg).replace('__MODULE_DATA__', safe_json(aggregate))
                .replace('__FLOW_DATA__', safe_json(startup)))
    # Preserve the detailed analysis exactly; summary generation does not reanalyze nets.
    (output / 'flow.json').write_text(analysis_text, encoding='utf-8')
    (output / 'flow_details.js').write_text(details_file, encoding='utf-8')
    (output / 'flow_modules.json').write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'flow.html').write_text(document, encoding='utf-8')
    version = subprocess.run([executable, '-V'], capture_output=True, text=True, timeout=10)
    metadata = {'rendered_at': datetime.datetime.now().astimezone().isoformat(),
                'graphviz': (version.stderr or version.stdout).strip(), 'dot_executable': executable,
                'rankdir': rankdir, 'module_summary': aggregate['summary'],
                'html_graph': 'inline copy of flow.svg generated from flow.dot',
                'detail_loading': 'flow_details.js loaded only when details are opened; full flow.json retained',
                'html_bytes': len(document.encode('utf-8')), 'details_bytes': len(details_file.encode('utf-8')),
                'metric': aggregate['unit'], 'minimum_connections': minimum}
    (output / 'flow_render.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False))
    return aggregate
