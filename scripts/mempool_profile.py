"""Provenance guard and reproducible derivatives for the audited MemPool example."""
import csv
import json
import re
from pathlib import Path

AUDITED_SHA256 = 'cf40747bcd1343e2edf17448f1fb5f04be5133ec87bf59f38b928ef59fe99d30'
TOP = 'mempool_group'
TILE0 = 'mempool_tile_TCDMBaseAddr0_BootAddr4096'


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def validate_profile(results):
    """Fixed sizes, field names and evidence line numbers belong to one netlist."""
    results = Path(results)
    summary_path = results / 'inventory_summary.json'
    if not summary_path.exists():
        summary_path = results / 'inventory.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))['summary']
    if (summary.get('top') != TOP or
            summary.get('uncompressed_source_sha256') != AUDITED_SHA256):
        raise ValueError('完整图册仅适用于已核实的 MemPool 示例网表（SHA256 不匹配）。'
                         '其他网表请使用 run --analysis-only，查看结构与连接 JSON。')


def derive_inventory(inventory, output):
    """Generate the two previously hand-extracted inventory artifacts."""
    output = Path(output)
    modules = inventory['modules']
    top = modules[inventory['summary']['top']]
    children, rows = [], []
    for child in top['children']:
        info = modules[child['type']]
        children.append(dict(child, expanded_leaf_count=info['expanded_leaf_count'],
                             definition_start_line=info['start_line']))
        rows.append({'instance': child['name'], 'module_type': child['type'],
                     'expanded_leaf_instances': info['expanded_leaf_count'],
                     'direct_leaf_instances': info['instance_count'] - len(info['children']),
                     'direct_hierarchy_instances': len(info['children']),
                     'definition_start_line': info['start_line'],
                     'definition_end_line': info['end_line']})
    write_json(output / 'inventory_summary.json',
               {'summary': inventory['summary'], 'top_ports': top['ports'],
                'undefined_leaf_pins': inventory['undefined_leaf_pins'], 'top_children': children})
    columns = ['instance', 'module_type', 'expanded_leaf_instances', 'direct_leaf_instances',
               'direct_hierarchy_instances', 'definition_start_line', 'definition_end_line']
    with (output / 'top_inventory.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def derive_dma_groups(connectivity, output):
    """Find groups from directed handshake edges, never from tile-number division."""
    groups = []
    edges = connectivity['direct_connections']
    tiles = [(i['name'], int(m.group(1))) for i in connectivity['instances']
             for m in [re.fullmatch(r'gen_tiles\[(\d+)\]\.i_tile', i['name'])] if m]
    demuxes = [(i['name'], int(m.group(1))) for i in connectivity['instances']
               for m in [re.fullmatch(r'gen_dmas\[(\d+)\]\.gen_dma_reqrsp_demux\.i_reqrsp_demux', i['name'])] if m]
    for demux, dma in sorted(demuxes, key=lambda x: x[1]):
        for tile_name, tile in sorted(tiles, key=lambda x: x[1]):
            def edge(source, source_pin, sink, sink_pin):
                matches = [e for e in edges if (e['source'], e['source_pin'], e['sink'], e['sink_pin'])
                           == (source, source_pin, sink, sink_pin)]
                if len(matches) > 1:
                    raise ValueError('Duplicate directed port pair')
                return matches[0] if matches else None
            request = edge(demux, 'mst_req_o', tile_name, 'tcdm_dma_req_valid_i')
            if request is None:
                continue
            handshake = [request,
                         edge(tile_name, 'tcdm_dma_req_ready_o', demux, 'mst_rsp_i'),
                         edge(tile_name, 'tcdm_dma_resp_valid_o', demux, 'mst_rsp_i'),
                         edge(demux, 'mst_req_o', tile_name, 'tcdm_dma_resp_ready_i')]
            if any(e is None or e['direct_bit_connections'] != 1 for e in handshake):
                raise ValueError('Incomplete DMA handshake for tile {}'.format(tile))
            response = edge(tile_name, 'tcdm_dma_resp_o', demux, 'mst_rsp_i')
            groups.append({'dma': dma, 'tile': tile, 'known_direct_handshake_bits': 4,
                           'known_direct_response_bus_bits': response['direct_bit_connections'] if response else 0,
                           'req_valid_binding': request['examples']})
    if len(groups) != 16 or sorted(g['tile'] for g in groups) != list(range(16)):
        raise ValueError('Expected one observed DMA group for each of the 16 tiles')
    write_json(Path(output) / 'dma_tile_groups.json', groups)
    return groups
