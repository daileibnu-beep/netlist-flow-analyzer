#!/usr/bin/env python3
"""Prepare auditable chart data from the existing offline netlist results.

Python 3.9+ standard library only. This file does not parse a netlist, call an
EDA tool, fetch data, or infer missing leaf-cell semantics. Matrix units are
boundary source-pin/sink-pin bit connections, NOT unique net bits.
"""
import argparse
import collections
import csv
import json
import re
from pathlib import Path


METRIC = '已核实端口位连接数'
METRIC_NOTE = ('按源端口位—目的端口位累计；同一 net 可因多个端点重复计数。'
               '不是独立 net 数、完整接口宽度、带宽、流量或时序关键性。')
NAME_FILTER = re.compile(r'(?:clk|clock|rst|reset|scan|testmode|test_mode)', re.I)


def load(root, name):
    return json.loads((root / name).read_text(encoding='utf-8'))


def load_inventory(root):
    """Use the lightweight summary, or derive it from the main scanner output."""
    if (root / 'inventory_summary.json').exists():
        return load(root, 'inventory_summary.json'), None
    full = load(root, 'inventory.json')
    modules = full['modules']
    top = modules[full['summary']['top']]
    children = []
    for child in top['children']:
        item = dict(child)
        item['expanded_leaf_count'] = modules[item['type']]['expanded_leaf_count']
        item['definition_start_line'] = modules[item['type']]['start_line']
        children.append(item)
    summary = {'summary': full['summary'], 'top_ports': top['ports'],
               'undefined_leaf_pins': full['undefined_leaf_pins'], 'top_children': children}
    return summary, modules


def inventory_rows(root, inv, modules):
    if (root / 'top_inventory.csv').exists():
        with (root / 'top_inventory.csv').open(encoding='utf-8') as stream:
            return list(csv.DictReader(stream))
    if modules is None:
        modules = load(root, 'inventory.json')['modules']
    rows = []
    for child in inv['top_children']:
        m = modules[child['type']]
        rows.append({'instance': child['name'], 'module_type': child['type'],
                     'expanded_leaf_instances': m['expanded_leaf_count'],
                     'direct_leaf_instances': m['instance_count'] - len(m['children']),
                     'direct_hierarchy_instances': len(m['children']),
                     'definition_start_line': m['start_line'],
                     'definition_end_line': m['end_line']})
    return rows


def clean_name(name):
    return name[1:] if name.startswith('\\') else name


def identity(name):
    match = re.fullmatch(r'gen_tiles\[(\d+)\]\.i_tile', name)
    if match:
        n = int(match.group(1))
        return {'short': 'T{}'.format(n), 'label': 'Tile {}'.format(n),
                'family': 'tile', 'group': 'tile', 'tile': n,
                'dma_group_candidate': n // 4, 'sort': [0, n]}
    if name == 'i_local_interco':
        return {'short': 'Local', 'label': 'Local interco',
                'family': 'tcdm_interco', 'group': 'local', 'sort': [1, 0]}
    match = re.fullmatch(r'gen_remote_interco\[(\d+)\]\.i_remote_interco', name)
    if match:
        n = int(match.group(1))
        return {'short': 'R{}'.format(n), 'label': 'Remote {}'.format(n),
                'family': 'tcdm_interco', 'group': 'remote', 'remote': n, 'sort': [1, n]}
    if name == 'i_axi_interco':
        return {'short': 'AXI', 'label': 'AXI interco',
                'family': 'axi', 'group': 'axi', 'sort': [2, 0]}
    match = re.fullmatch(r'gen_dmas\[(\d+)\]\.(.*)', name)
    if match:
        n, tail = int(match.group(1)), match.group(2)
        roles = {'i_axi_dma_backend': ('back', 'backend', 0),
                 'i_dma_axi_xbar': ('xbar', 'AXI xbar', 1),
                 'i_axi_to_reqrsp': ('bridge', 'AXI-to-reqrsp', 2),
                 'gen_dma_reqrsp_demux.i_reqrsp_demux': ('demux', 'reqrsp demux', 3)}
        short, label, order = roles[tail]
        return {'short': 'D{}.{}'.format(n, short), 'label': 'DMA {} {}'.format(n, label),
                'family': 'dma', 'group': 'dma{}'.format(n), 'dma_group': n,
                'role': short, 'sort': [3, n, order]}
    other = {
        'i_idma_distributed_midend': ('D.mid', 'DMA midend', 'dma_shared', 0),
        'i_dma_req_register': ('D.reg', 'DMA request register', 'dma_shared', 1),
        'gen_axi_group_cuts[0].i_axi_cut': ('AXI.cut', 'AXI group cut', 'axi', 2),
    }
    short, label, family, order = other[name]
    return {'short': short, 'label': label, 'family': family,
            'group': family, 'sort': [4, order]}


def bit_map(analysis, bound, instance, port):
    metadata = next(x for x in analysis['instances'] if x['name'] == instance)['ports'][port]
    values = next(x for x in bound['instances'] if x['name'] == instance)['pins'][port]
    if metadata['msb'] is None:
        bits = [None]
    else:
        a, b = metadata['msb'], metadata['lsb']
        bits = range(a, b + (1 if b >= a else -1), 1 if b >= a else -1)
    answer = dict(zip(bits, map(tuple, values)))
    assert len(answer) == len(values) == metadata['width']
    return answer


def build_data(results, out):
    """Build the audited MemPool visualization dataset."""
    results, out = Path(results), Path(out)
    from mempool_profile import validate_profile
    validate_profile(results)
    inv, modules = load_inventory(results)
    conn = load(results, 'connectivity.json')
    bindings = load(results, 'top_instances_connections.json')
    tile_conn = load(results, 'tile0_connectivity.json')
    dma = load(results, 'dma_tile_groups.json')
    csv_rows = inventory_rows(results, inv, modules)
    instance_meta = {x['name']: x for x in conn['instances']}
    nodes = []
    for row in csv_rows:
        name = clean_name(row['instance'])
        node = {'id': name, 'module_type': row['module_type'],
                'expanded_leaf_count': int(row['expanded_leaf_instances']),
                'direct_leaf_count': int(row['direct_leaf_instances']),
                'direct_hierarchy_count': int(row['direct_hierarchy_instances']),
                'definition_start_line': int(row['definition_start_line']),
                'definition_end_line': int(row['definition_end_line']),
                'instance_line': instance_meta[name]['line']}
        node.update(identity(name))
        nodes.append(node)
    nodes.sort(key=lambda n: n['sort'])
    assert len(nodes) == len(set(x['id'] for x in nodes)) == 40
    total = inv['summary']['top_expanded_undefined_leaf_instances']
    defined_subtree_total = sum(n['expanded_leaf_count'] for n in nodes)
    direct_top = total - defined_subtree_total
    assert direct_top == 10205
    families = collections.Counter()
    for node in nodes:
        families[node['family']] += node['expanded_leaf_count']
    families['top_direct_leaf'] = direct_top
    assert sum(families.values()) == total

    # $PORT is a logical boundary node, not a cell or an area-bearing block.
    matrix_nodes = nodes + [{'id': '$PORT', 'short': 'I/O', 'label': 'Top ports',
                            'family': 'boundary', 'group': 'boundary',
                            'expanded_leaf_count': None}]
    positions = {node['id']: i for i, node in enumerate(matrix_nodes)}
    size = len(matrix_nodes)
    raw = [[0] * size for _ in range(size)]
    filtered = [[0] * size for _ in range(size)]
    excluded = [[0] * size for _ in range(size)]
    edge_details = []
    for edge in conn['direct_connections']:
        source, sink = positions[edge['source']], positions[edge['sink']]
        count = edge['direct_bit_connections']
        raw[source][sink] += count
        excluded_by_name = bool(NAME_FILTER.search(edge['source_pin']) or NAME_FILTER.search(edge['sink_pin']))
        target = excluded if excluded_by_name else filtered
        target[source][sink] += count
        edge_details.append({'source': source, 'sink': sink,
                             'source_pin': edge['source_pin'], 'sink_pin': edge['sink_pin'],
                             'count': count, 'excluded_by_clock_reset_test_name': excluded_by_name,
                             'examples': edge['examples']})
    assert sum(map(sum, raw)) == sum(e['direct_bit_connections'] for e in conn['direct_connections'])
    assert sum(map(sum, raw)) == sum(map(sum, filtered)) + sum(map(sum, excluded))
    ranked_pairs = []
    for i in range(size):
        for j in range(size):
            if filtered[i][j]:
                ranked_pairs.append({'source': i, 'sink': j, 'count': filtered[i][j]})
    ranked_pairs.sort(key=lambda x: (-x['count'], x['source'], x['sink']))

    groups = []
    for group in range(4):
        items = sorted([x for x in dma if x['dma'] == group], key=lambda x: x['tile'])
        assert [x['tile'] for x in items] == list(range(group * 4, group * 4 + 4))
        assert all(x['known_direct_handshake_bits'] == 4 for x in items)
        groups.append({'dma': group,
                       'demux_id': 'gen_dmas[{}].gen_dma_reqrsp_demux.i_reqrsp_demux'.format(group),
                       'tiles': [x['tile'] for x in items],
                       'verified_handshake_bits_per_tile': 4,
                       'response_direct_bits': [x['known_direct_response_bus_bits'] for x in items],
                       'basis': '四条 valid/ready 位的实际连接，加上已核实的直接响应位。'})
    for node in nodes:
        if node['family'] == 'tile':
            matched_group = next(g['dma'] for g in groups if node['tile'] in g['tiles'])
            assert matched_group == node.pop('dma_group_candidate')
            node['dma_group'] = matched_group

    tile0_name = 'gen_tiles[0].i_tile'
    request = bit_map(conn, bindings, tile0_name, 'tcdm_master_req_o')
    assert len(request) == 248
    channels = []
    for channel in range(4):
        dst = 'i_local_interco' if channel == 0 else 'gen_remote_interco[{}].i_remote_interco'.format(channel)
        lower = channel * 62
        upper = lower + 61
        fields = [
            ('meta_id', lower + 61, lower + 59, 'req_wdata_i', 40, 38, '网名字段'),
            ('core_id', lower + 58, lower + 57, 'req_wdata_i', 37, 36, '网名字段'),
            ('amo', lower + 56, lower + 53, 'req_wdata_i', 35, 32, '网名字段'),
            ('data', lower + 52, lower + 21, 'req_wdata_i', 31, 0, '网名字段'),
            ('wen', lower + 20, lower + 20, 'req_wen_i', 0, 0, '端口名称'),
            ('be', lower + 19, lower + 16, 'req_be_i', 3, 0, '端口名称'),
            ('addr', lower + 15, lower, 'req_tgt_addr_i', 15, 0, '端口名称'),
        ]
        details = []
        count = 0
        for name, hi, lo, sink_pin, dhi, dlo, naming_basis in fields:
            sink_map = bit_map(conn, bindings, dst, sink_pin)
            mapping = []
            for s, d in zip(range(hi, lo - 1, -1), range(dhi, dlo - 1, -1)):
                same = request[s] == sink_map[d]
                count += int(same)
                mapping.append({'source_bit': s, 'sink_bit': d, 'direct_equal': same,
                                'source_net': list(request[s]), 'sink_net': list(sink_map[d])})
            verified = sum(x['direct_equal'] for x in mapping)
            if name in ('meta_id', 'core_id', 'amo', 'data'):
                assert all(request[x][0].endswith('_' + name) for x in range(lo, hi + 1))
            details.append({'field': name, 'width': hi - lo + 1, 'source_msb': hi,
                            'source_lsb': lo, 'sink_pin': sink_pin,
                            'sink_msb': dhi, 'sink_lsb': dlo,
                            'verified_direct_bits': verified,
                            'unknown_direct_bits': hi - lo + 1 - verified,
                            'naming_basis': naming_basis, 'mapping': mapping})
        assert count == (58 if channel == 0 else 62)
        channels.append({'channel': channel, 'source_msb': upper, 'source_lsb': lower,
                         'width': 62, 'sink': dst, 'label': 'Local' if channel == 0 else 'Remote {}'.format(channel),
                         'verified_direct_bits': count, 'unknown_direct_bits': 62 - count,
                         'fields': details})

    bank = []
    for source, source_pin, sink, sink_pin, width in [
        ('gen_banks[0].mem_bank', 'rdata_o', 'gen_banks[0].i_tcdm_adapter', 'out_rdata_i', 32),
        ('gen_banks[0].i_tcdm_adapter', 'out_wdata_o', 'gen_banks[0].mem_bank', 'wdata_i', 32),
        ('gen_banks[0].i_tcdm_adapter', 'out_add_o', 'gen_banks[0].mem_bank', 'addr_i', 8),
    ]:
        candidates = [e for e in tile_conn['direct_connections']
                      if (e['source'],e['source_pin'],e['sink'],e['sink_pin']) == (source,source_pin,sink,sink_pin)]
        assert len(candidates) == 1
        count = candidates[0]['direct_bit_connections']
        bank.append({'source': source, 'source_pin': source_pin,
                     'sink': sink, 'sink_pin': sink_pin,
                     'port_width': width, 'verified_direct_bits': count,
                     'unknown_direct_bits': width - count,
                     'examples': candidates[0]['examples'][:2]})
    assert [x['verified_direct_bits'] for x in bank] == [32, 31, 7]

    result = {
        'schema_version': 1,
        'title': 'mempool_group 离线结构视图',
        'source': {'file': 'mempool_group.v.gz', 'top': 'mempool_group',
                   'sha256_uncompressed': inv['summary'].get('uncompressed_source_sha256'),
                   'source_bytes': inv['summary']['source_bytes'],
                   'source_lines': inv['summary']['source_lines'], 'sdc_available': False},
        'summary': {'module_definitions': inv['summary']['module_definitions'],
                    'expanded_leaf_instances': total, 'undefined_leaf_types': inv['summary']['undefined_leaf_types'],
                    'top_defined_children': len(nodes), 'top_direct_leaf_instances': direct_top,
                    'tile_count': 16, 'tile_leaf_total': families['tile'],
                    'tile_leaf_percentage': 100.0 * families['tile'] / total,
                    'tile_leaf_min': min(n['expanded_leaf_count'] for n in nodes if n['family'] == 'tile'),
                    'tile_leaf_max': max(n['expanded_leaf_count'] for n in nodes if n['family'] == 'tile'),
                    'axi_interco_leaf_count': next(n['expanded_leaf_count'] for n in nodes if n['id'] == 'i_axi_interco')},
        'nodes': nodes,
        'composition': [{'family': k, 'leaf_instances': v, 'percentage': 100.0 * v / total}
                        for k, v in families.items()],
        'matrix': {'nodes': matrix_nodes, 'unit': METRIC, 'unit_note': METRIC_NOTE,
                   'row_axis': '源实例', 'column_axis': '目的实例', 'direction': 'row -> column',
                   'raw': raw, 'name_filtered': filtered, 'excluded_by_name': excluded,
                   'filter_label': '按名称剔除 clock/reset/test 接口',
                   'filter_regex': NAME_FILTER.pattern,
                   'filter_note': '仅按端口名称筛选；保留项不等于已证明为功能数据，未验证时钟、复位或测试语义。',
                   'zero_note': '0 表示所选边界未核实到直接连接，不证明两模块没有跨 leaf 的连接。',
                   'examples_note': '每个端口对保留原连接结果中的全部examples（原结果最多8条）；总数使用direct_bit_connections，不从examples重算。',
                   'raw_total': sum(map(sum, raw)), 'name_filtered_total': sum(map(sum, filtered)),
                   'excluded_total': sum(map(sum, excluded)),
                   'ranked_name_filtered_pairs': ranked_pairs, 'edge_details': edge_details},
        'dma_groups': groups,
        'request_bus': {'instance': tile0_name, 'port': 'tcdm_master_req_o', 'width': 248,
                        'display_order_msb_to_lsb': [3, 2, 1, 0], 'channels': channels,
                        'default_channel': 1,
                        'note': '位映射由实际绑定逐位比较。字段名称来自网名或端口名，未证明完整协议语义。'},
        'bank0': {'instance_parent': tile0_name, 'connections': bank,
                  'unknown_note': '写数据 bit14、地址 bit2 在中间经过无定义的 BUF_X4。当前不把它假定为透明，未知不表示缺线。'},
        'coverage': {'top': conn['coverage'], 'tile0': tile_conn['coverage'],
                     'top_parse_failures': len(conn['parse_failures']),
                     'top_width_errors': len(conn['width_errors']),
                     'tile0_parse_failures': len(tile_conn['parse_failures']),
                     'tile0_width_errors': len(tile_conn['width_errors'])},
        'limitations': [
            '模块图仅表示相邻边界连接，不证明跨模块的端到端功能数据流。',
            '未知leaf不穿越，直连统计是当前已证明部分；不同模块的覆盖率可能不同。',
            '所有规模标注是叶实例数，不是面积；布局分组是候选邻接关系，不是实际物理坐标。',
            '连接热力图单位是端点位连接计数；不能当独立net数、完整接口宽度、带宽或流量。',
            '没有SDC，未计算时钟频率、时序关键性、延迟或slack。',
        ],
        'evidence': {'inventory': ['inventory_summary.json', 'top_inventory.csv'],
                     'connectivity': ['connectivity.json', 'top_instances_connections.json'],
                     'top_module_line': conn['source_line'],
                     'tile0_instance_line': instance_meta[tile0_name]['line'],
                     'local_interco_instance_line': instance_meta['i_local_interco']['line'],
                     'dma0_demux_instance_line': instance_meta['gen_dmas[0].gen_dma_reqrsp_demux.i_reqrsp_demux']['line'],
                     'bank0_adapter_instance_line': 885570, 'bank0_instance_line': 885696,
                     'bank0_unknown_write_buffer_line': 867805},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'output': str(out), 'bytes': out.stat().st_size,
                      'defined_nodes': len(nodes), 'matrix_nodes': len(matrix_nodes),
                      'raw_pair_bit_count': sum(map(sum, raw)), 'name_filtered_pair_bit_count': sum(map(sum, filtered)),
                      'excluded_pair_bit_count': sum(map(sum, excluded)),
                      'request_verified': [x['verified_direct_bits'] for x in channels],
                      'family_leaf_counts': dict(families)}, ensure_ascii=False))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent / 'viz_data.json')
    args = parser.parse_args(argv)
    return build_data(args.results, args.out)


if __name__ == '__main__':
    main()
