#!/usr/bin/env python3
"""Compare native exports with independent Verilog pin-bit sets and render them.

Run after run_innovus.tcl. No EDA installation is required for this verification
step. The native conservative empty-module policy is matched explicitly.
"""
import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from hierarchical_flow import HierarchyFlow, ModuleReader
from innovus_flow import build_graph
from render_module_graph import aggregate_modules
from scan_inventory import index_modules

CASES = {
    'top': {},
    'depth2': {'depth': 2},
    'depth3': {'depth': 3},
    'mixed': {'expand_at': [('fixture_top/gen[2].u_group', 1)]},
    'subroot': {'root': 'fixture_top/gen[2].u_group'},
    'stop': {'depth': 2, 'stop': ['fixture_top/gen[2].u_group']},
    'special_root': {'root': 'fixture_top/path~1with~0mark'},
    'nested': {
        'expand_at': [('fixture_top/gen[2].u_group', 2)],
        'stop': ['fixture_top/gen[2].u_group/consumer'],
    },
    'include_empty': {},
}
NEGATIVE_CHECKS = {
    'invalid_root', 'outside_expand', 'stop_conflict', 'invalid_depth',
    'endpoint_limit', 'instance_limit', 'net_limit', 'overwrite_protection',
    'read_only_counts',
}


def pairs(graph):
    """Return identity-preserving source/sink instance-port-bit tuples."""
    return {
        (edge['source'], edge['source_pin'], bit['source_bit'],
         edge['sink'], edge['sink_pin'], bit['sink_bit'])
        for edge in graph['edges'] for bit in edge['bit_connections']
    }


def check_native_status(directory):
    lines = (directory / 'native_case_status.tsv').read_text().splitlines()
    if not lines or lines[0] != 'case\tstatus\tdetail':
        raise ValueError('Missing or invalid native test status header')
    rows = [line.split('\t', 2) for line in lines[1:]]
    expected = set(CASES) | NEGATIVE_CHECKS
    if (len(rows) != len(expected) or
            any(len(row) != 3 or row[1] != 'PASS' for row in rows) or
            {row[0] for row in rows} != expected):
        raise ValueError('Native test matrix incomplete or a native check failed')
    return len(rows)


def verify_case(name, options, reader, native_outputs, output):
    started = time.monotonic()
    options = dict(options)
    options['stop'] = list(options.get('stop', []))
    # Native default excludes zero-instance assign-only wrappers. Keep this
    # wrapper folded in the reference, rather than comparing different scopes.
    if not options.get('root'):
        options['stop'].append('fixture_top/passthrough')
    reference = HierarchyFlow(reader, 'fixture_top', **options).graph()
    graph = build_graph(native_outputs / name)
    native_nodes = {node['id'] for node in graph['nodes']}
    ref_nodes = {node['id'] for node in reference['nodes']}
    if not native_nodes <= ref_nodes:
        raise ValueError('{}: unexpected native nodes {}'.format(
            name, native_nodes - ref_nodes))
    expected = {pair for pair in pairs(reference)
                if pair[0] in native_nodes and pair[3] in native_nodes}
    actual = pairs(graph)
    missing, extra = expected - actual, actual - expected
    outcome = {
        'case': name, 'nodes': len(native_nodes),
        'module_edges': aggregate_modules(graph)['summary']['all_module_pair_edges'],
        'native_pairs': len(actual), 'reference_pairs': len(expected),
        'missing': len(missing), 'extra': len(extra),
        'exact_match': actual == expected,
    }
    differences = {'missing': sorted(missing, key=str),
                   'extra': sorted(extra, key=str)}
    (output / (name + '_differences.json')).write_text(
        json.dumps(differences, indent=2), encoding='utf-8')
    render = output / (name + '_graph')
    command = [sys.executable, str(ROOT / 'analyze.py'), 'innovus-flow',
               str(native_outputs / name), '--out', str(render)]
    run = subprocess.run(command, capture_output=True, text=True)
    (output / (name + '_cli.log')).write_text(
        run.stdout + run.stderr, encoding='utf-8')
    outcome['cli_returncode'] = run.returncode
    if run.returncode == 0:
        modules = json.loads((render / 'flow_modules.json').read_text())
        outcome['render_counts_match'] = (
            modules['summary']['all_bit_connections'] == len(actual))
        svg = (render / 'flow.svg').read_text()
        inline = svg[svg.index('<svg'):svg.rindex('</svg>') + 6]
        outcome['inline_svg_identical'] = (
            inline in (render / 'flow.html').read_text())
        ids = {element.get('id') for element in ET.fromstring(svg).iter()}
        outcome['svg_ids_present'] = all(
            item['svg_id'] in ids for item in modules['nodes'] + modules['edges'])
    outcome['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('native_outputs', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    checks = check_native_status(args.native_outputs)
    if args.out.exists():
        raise ValueError('Use a new --out directory')
    args.out.mkdir(parents=True)
    fixture = ROOT / 'tests/eda/fixtures/synthetic.v'
    index, *_ = index_modules(fixture)
    reader = ModuleReader(fixture, index)
    results = [verify_case(name, options, reader, args.native_outputs, args.out)
               for name, options in CASES.items()]
    report = {
        'schema_version': 1, 'native_status_checks': checks, 'cases': results,
        'all_pass': all(
            case['exact_match'] and case['cli_returncode'] == 0 and
            case.get('svg_ids_present') and case.get('inline_svg_identical') and
            case.get('render_counts_match') for case in results),
    }
    (args.out / 'summary.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['all_pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
