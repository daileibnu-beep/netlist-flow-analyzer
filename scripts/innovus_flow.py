"""Import a read-only Innovus snapshot into the existing directed flow schema.

Only hierarchical hNet IDs and explicitly directed expanded-port bridges connect
vertices. Flat canonical nets are retained as evidence, never used to bypass a
folded module's boundary.
"""
import collections
import hashlib
import json
import re
from pathlib import Path

TABLES = {
    'nodes': ['id', 'path', 'module_type', 'kind', 'status'],
    'scopes': ['id', 'path', 'module_type', 'direct_leaf_count'],
    'ports': ['owner', 'port', 'bit', 'pin', 'direction', 'hnet', 'tie_hi', 'tie_lo'],
    'bridges': ['scope', 'pin', 'direction', 'up_hnet', 'down_hnet', 'tie_hi', 'tie_lo'],
    'hnets': ['hnet', 'scope', 'name', 'canonical_net', 'power_ground'],
    'diagnostics': ['kind', 'id', 'pin', 'detail'],
}
NULL = {'', '0', '0x0'}


def path_id(parts):
    return '/'.join(p.replace('~', '~0').replace('/', '~1') for p in parts)


def positive(value, name, maximum=None):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or (maximum and value > maximum):
        raise ValueError('Invalid ' + name)
    return value


def read_export(folder, max_rows=5000000):
    folder = Path(folder)
    positive(max_rows, '--max-rows')
    manifest_path = folder / 'manifest.json'
    raw = manifest_path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('Export manifest is too large')
    m = json.loads(raw)
    if (not isinstance(m, dict) or m.get('schema_version') != 1 or m.get('format') != 'innovus-hier-flow' or
            m.get('field_encoding') != 'json-string-tsv-v1' or m.get('status') != 'complete'):
        raise ValueError('Unsupported or incomplete Innovus export')
    if m.get('empty_policy') not in ('exclude', 'include'):
        raise ValueError('Invalid empty module policy')
    positive(m.get('depth'), 'export depth', 64)
    root = m.get('root_path')
    if (not isinstance(root, list) or not root or
            any(not isinstance(v, str) or not v for v in root) or
            root[0] != m.get('top') or path_id(root) != m.get('root')):
        raise ValueError('Invalid export root path')
    if not isinstance(m.get('files'), dict) or set(m['files']) != {n + '.tsv' for n in TABLES}:
        raise ValueError('Export table list differs from schema')
    if not isinstance(m.get('expand', []), list):
        raise ValueError('Invalid expansion metadata')
    for selected in m.get('expand', []):
        if not isinstance(selected, dict) or not isinstance(selected.get('path'), str):
            raise ValueError('Invalid expansion metadata')
        positive(selected.get('depth'), 'instance expansion depth', 64)
    if not isinstance(m.get('stop', []), list) or any(not isinstance(v, str) for v in m.get('stop', [])):
        raise ValueError('Invalid stop metadata')
    total, result = 0, {}
    hashes = {'manifest.json': hashlib.sha256(raw).hexdigest()}
    for name, columns in TABLES.items():
        file = folder / (name + '.tsv')
        meta = m['files'][file.name]
        if not isinstance(meta, dict):
            raise ValueError('Invalid table metadata: ' + file.name)
        expected = meta.get('rows')
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError('Invalid row count: ' + file.name)
        total += expected
        if total > max_rows:
            raise ValueError('Exceeded --max-rows')
        if file.is_symlink() or file.stat().st_size != meta.get('bytes'):
            raise ValueError('Export file size/link mismatch: ' + file.name)
        digest, records = hashlib.sha256(), []
        with file.open('rb') as stream:
            for line_no, data in enumerate(iter(lambda: stream.readline(1024 * 1024 + 1), b''), 1):
                if len(data) > 1024 * 1024:
                    raise ValueError('Oversized export row: ' + file.name)
                digest.update(data)
                cells = data.decode('utf-8').rstrip('\r\n').split('\t')
                if line_no == 1:
                    if cells != columns:
                        raise ValueError('Unexpected table columns: ' + file.name)
                    continue
                if len(cells) != len(columns):
                    raise ValueError('Invalid TSV width: {}:{}'.format(file.name, line_no))
                try:
                    values = [json.loads(c) for c in cells]
                except ValueError as error:
                    raise ValueError('Invalid TSV JSON field: {}:{}'.format(file.name, line_no)) from error
                if not all(isinstance(v, str) for v in values):
                    raise ValueError('Non-string export field: ' + file.name)
                records.append(dict(zip(columns, values)))
                if len(records) > expected:
                    raise ValueError('Too many rows: ' + file.name)
        if len(records) != expected or file.stat().st_size == 0:
            raise ValueError('Incomplete export table: ' + file.name)
        hashes[file.name] = digest.hexdigest()
        result[name] = records
    return m, result, hashes


def build_graph(folder, max_pairs=200000, max_bits=2000000, max_rows=5000000):
    positive(max_pairs, '--max-pairs'); positive(max_bits, '--max-bits')
    manifest, data, hashes = read_export(folder, max_rows)
    root_parts, root = manifest['root_path'], manifest['root']
    nodes, scopes = {}, {}
    for table, dest in [('nodes', nodes), ('scopes', scopes)]:
        for row in data[table]:
            parts = json.loads(row['path'])
            if (not isinstance(parts, list) or any(not isinstance(v, str) or not v for v in parts) or
                    parts[:len(root_parts)] != root_parts or len(parts) > len(root_parts) + 64):
                raise ValueError('Node/scope is outside export root')
            boundary = table == 'nodes' and row['kind'] == 'scope_boundary'
            expected = path_id(parts) + ('/$PORT' if boundary else '')
            if row['id'] != expected or row['id'] in dest:
                raise ValueError('Duplicate/invalid node or scope ID: ' + row['id'])
            if table == 'nodes' and row['kind'] not in ('module', 'scope_boundary'):
                raise ValueError('Unknown node kind')
            dest[row['id']] = dict(row, path=parts)
            if table == 'scopes' and (not re.fullmatch(r'\d+', row['direct_leaf_count'])):
                raise ValueError('Invalid direct leaf count')
    boundary_id = root + '/$PORT'
    if root not in scopes or boundary_id not in nodes or sum(n['kind'] == 'scope_boundary' for n in nodes.values()) != 1:
        raise ValueError('Missing/ambiguous export root boundary')
    if nodes[boundary_id]['path'] != root_parts:
        raise ValueError('Invalid boundary path')
    overlap = set(nodes) & set(scopes)
    for item in list(nodes.values()) + list(scopes.values()):
        if item['id'] in (root, boundary_id):
            continue
        parent = path_id(item['path'][:-1])
        if parent not in scopes or item['id'] in overlap:
            raise ValueError('Frontier/expanded scope hierarchy is inconsistent: ' + item['id'])
    # A visible boundary cannot have a visible descendant.
    module_ids = {n['id'] for n in nodes.values() if n['kind'] == 'module'}
    for node in nodes.values():
        for length in range(len(root_parts), len(node['path'])):
            if path_id(node['path'][:length]) in module_ids:
                raise ValueError('A folded module contains another visible node')
    hnets = {}
    for row in data['hnets']:
        if row['hnet'] in NULL or row['hnet'] in hnets or row['scope'] not in scopes:
            raise ValueError('Duplicate/invalid hierarchical net')
        if row['power_ground'] not in ('0', '1'):
            raise ValueError('Invalid P/G flag')
        hnets[row['hnet']] = row
    if len(hnets) > max_bits:
        raise ValueError('Exceeded --max-bits')
    diagnostics = {k: [] for k in ('undefined_leaves', 'skipped_inout_ports', 'empty_or_omitted_ports',
                                  'constant_bindings', 'multiple_known_sources')}
    diagnostics['export_diagnostics'] = data['diagnostics']
    diagnostics['power_ground_endpoints'] = []
    diagnostics['undefined_leaves'] = [r for r in data['diagnostics'] if r['kind'] == 'empty_module']
    adj = collections.defaultdict(list)
    bridges = []

    def checked_flags(row):
        if row['tie_hi'] not in ('0', '1') or row['tie_lo'] not in ('0', '1'):
            raise ValueError('Invalid tie flag')
        return row['tie_hi'] == '1' or row['tie_lo'] == '1'

    def net_record(key):
        row = hnets[key]
        return {'kind': 'net', 'scope': row['scope'], 'name': row['name'], 'bit': None,
                'database_id': key}

    for b in data['bridges']:
        if b['scope'] not in scopes or b['scope'] == root:
            raise ValueError('Bridge is outside expanded child scopes')
        up, down = b['up_hnet'], b['down_hnet']
        tied = checked_flags(b)
        if up in NULL or down in NULL:
            diagnostics['empty_or_omitted_ports'].append(dict(b, reason='unconnected expanded port'))
            continue
        if up not in hnets or down not in hnets:
            raise ValueError('Bridge refers to absent hNet')
        parent = path_id(scopes[b['scope']]['path'][:-1])
        if hnets[up]['scope'] != parent or hnets[down]['scope'] != b['scope']:
            raise ValueError('Bridge does not link its immediate parent and child scopes')
        if tied:
            diagnostics['constant_bindings'].append(b); continue
        if hnets[up]['power_ground'] == '1' or hnets[down]['power_ground'] == '1':
            continue
        direction = b['direction']
        if direction not in ('input', 'output', 'outputTriState'):
            diagnostics['skipped_inout_ports'].append(b); continue
        source, sink = (up, down) if direction == 'input' else (down, up)
        ev = {'kind': 'expanded_input' if direction == 'input' else 'expanded_output',
              'scope': b['scope'], 'pin': b['pin'], 'direction': direction,
              'from': net_record(source), 'to': net_record(sink)}
        adj[source].append((sink, ev)); bridges.append(dict(b, from_net=source, to_net=sink))
    sources, sinks = collections.defaultdict(list), collections.defaultdict(list)
    all_endpoints = set()
    endpoint_nets, endpoint_kinds = {}, {}
    for row in data['ports']:
        owner = row['owner']
        if owner not in nodes:
            raise ValueError('Port owner is not a visible boundary: ' + owner)
        bit = None
        if row['bit']:
            if not re.fullmatch(r'-?\d+', row['bit']):
                raise ValueError('Invalid bit index')
            bit = int(row['bit'])
        if row['pin'] != row['port'] + (('[{}]'.format(bit)) if bit is not None else ''):
            raise ValueError('Port bit metadata differs from raw pin name')
        ep = (owner, row['port'], bit)
        if ep in all_endpoints:
            raise ValueError('Duplicate exported endpoint: ' + repr(ep))
        all_endpoints.add(ep)
        tied = checked_flags(row)
        net = row['hnet']
        if net in NULL:
            diagnostics['empty_or_omitted_ports'].append(row); continue
        if net not in hnets:
            raise ValueError('Endpoint refers to absent hNet')
        expected_scope = root if owner == boundary_id else path_id(nodes[owner]['path'][:-1])
        if hnets[net]['scope'] != expected_scope:
            raise ValueError('Endpoint is attached outside its immediate enclosing scope')
        if tied:
            diagnostics['constant_bindings'].append(row); continue
        if hnets[net]['power_ground'] == '1':
            diagnostics['power_ground_endpoints'].append(row); continue
        if row['direction'] not in ('input', 'output', 'outputTriState'):
            diagnostics['skipped_inout_ports'].append(row); continue
        is_output = row['direction'] in ('output', 'outputTriState')
        is_source = is_output != (owner == boundary_id)
        (sources if is_source else sinks)[net].append(ep)
        endpoint_nets[ep] = net
        endpoint_kinds[ep] = ('root_input' if is_source else 'root_output') if owner == boundary_id else (
            'frontier_output' if is_source else 'frontier_input')

    def pin_record(ep):
        return {'kind': 'pin', 'scope': ep[0], 'name': ep[1], 'bit': ep[2]}

    groups, matched_sources, matched_sinks = {}, set(), set()
    drivers, pair_count = collections.defaultdict(set), 0
    for start, eps in sorted(sources.items()):
        queue, previous = [start], {start: None}
        targets = {}
        for here in queue:
            for sink in sinks.get(here, ()):
                targets[sink] = here
            for nxt, evidence in adj.get(here, ()):
                if nxt not in previous:
                    previous[nxt] = (here, evidence); queue.append(nxt)
        for source in eps:
            for sink, finish in sorted(targets.items(), key=lambda item: repr(item[0])):
                pair_count += 1
                if pair_count > max_pairs:
                    raise ValueError('Exceeded --max-pairs; narrow the exported hierarchy')
                key = (source[0], source[1], sink[0], sink[1])
                if key not in groups:
                    groups[key] = dict(source=key[0], source_pin=key[1], sink=key[2], sink_pin=key[3],
                                       bit_connections=[], examples=[])
                group = groups[key]
                group['bit_connections'].append({'source_bit': source[2], 'sink_bit': sink[2]})
                matched_sources.add(source); matched_sinks.add(sink); drivers[sink].add(source)
                if len(group['examples']) < 8:
                    hops, cursor = [], finish
                    while previous[cursor] is not None:
                        prev, evidence = previous[cursor]; hops.append(evidence); cursor = prev
                    hops.reverse()
                    hops.insert(0, {'kind': endpoint_kinds[source], 'from': pin_record(source), 'to': net_record(start)})
                    hops.append({'kind': endpoint_kinds[sink], 'from': net_record(finish), 'to': pin_record(sink)})
                    group['examples'].append({'source_bit': source[2], 'sink_bit': sink[2], 'hops': hops})
    edges = [dict(value, direct_bit_connections=len(value['bit_connections'])) for key, value in sorted(groups.items())]
    diagnostics['multiple_known_sources'] = [dict(sink=pin_record(k), sources=[pin_record(s) for s in sorted(v, key=repr)])
                                             for k, v in sorted(drivers.items(), key=repr) if len(v) > 1]
    ns, nt = sum(map(len, sources.values())), sum(map(len, sinks.values()))
    uncertain = manifest['empty_policy'] == 'include' and bool(diagnostics['undefined_leaves'])
    limitations = [
        'Innovus 数据库快照的结构连接；不表示功能、时序、带宽或流量。',
        '仅使用各层 hNet 和显式有向端口桥接；不穿越折叠模块、标准单元或寄存器。',
        'Canonical flat net 仅作证据保留，不用于短接不同 hNet，避免穿越折叠模块的内部 alias。',
        '若某版本把同层 assign 保留为不同 hNet，本导出不会仅凭 flat net 相同推断其连通。',
        'Inout/unknown 方向、P/G、常量及未连接端口被跳过并记录。',
        '数据库没有可靠区分无定义空模块与无实例的 assign-only 模块；默认保守排除两者。',
        '完整逐位映射保留；每个端口对最多保留 8 个最短路径证据。']
    if uncertain:
        limitations.append('本次包含空模块端口，方向可能由 Innovus 推断，相关连接需要核实。')
    coverage = {'visible_nodes': len(nodes), 'expanded_scopes': len(scopes),
                'encountered_instances': manifest.get('encountered_instances'), 'bit_graph_vertices': len(hnets),
                'source_pin_bits': ns, 'sink_pin_bits': nt, 'connected_source_pin_bits': len(matched_sources),
                'connected_sink_pin_bits': len(matched_sinks), 'unconnected_source_pin_bits': ns - len(matched_sources),
                'unconnected_sink_pin_bits': nt - len(matched_sinks), 'port_pair_edges': len(edges),
                'source_sink_bit_pairs': pair_count, 'undefined_leaf_instances': len(diagnostics['undefined_leaves']),
                'skipped_inout_ports': len(diagnostics['skipped_inout_ports']),
                'empty_or_omitted_ports': len(diagnostics['empty_or_omitted_ports']),
                'constant_bindings': len(diagnostics['constant_bindings']),
                'leaf_instances_not_traversed': sum(int(s['direct_leaf_count']) for s in scopes.values())}
    return {'schema_version': 1, 'graph_kind': 'cross_hierarchy_database_connectivity',
            'top': manifest['top'], 'root': root, 'depth': manifest['depth'],
            'selection': {'expand': manifest.get('expand', []), 'stop': manifest.get('stop', [])},
            'nodes': [dict(n, stop_reason=n['status']) for n in nodes.values()],
            'expanded_scopes': list(scopes.values()), 'expanded_port_bridges': bridges,
            'edges': edges, 'coverage': coverage, 'diagnostics': diagnostics, 'limitations': limitations,
            'connection_unit': '端口位连接数（含空模块方向，待核实）' if uncertain else '数据库端口位连接数',
            'data_source': {'kind': 'innovus', 'summary': '来源：Innovus {} 数据库快照；空模块策略：{}。{}'.format(
                manifest.get('innovus_version', 'unknown'), manifest['empty_policy'],
                '包含空模块方向，连接需核实。' if uncertain else '空模块及 assign-only 无实例模块默认排除。'),
                'snapshot': manifest, 'files_sha256': hashes},
            'source': {'path': str(Path(folder).resolve()), 'type': 'innovus_database_export'}}
