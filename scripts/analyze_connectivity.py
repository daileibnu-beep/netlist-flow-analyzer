#!/usr/bin/env python3
"""Narrow structural connectivity inspector for the attached generated netlist.

Python standard library only; this is not a Verilog elaborator. Expressions not
covered by bit selects, ranges, concatenations, constants and bare declared nets
are reported, never guessed. Leaf cells without definitions are not traversed.
"""
import collections
import argparse
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
ID = r'(?:\\[^\s]+|[A-Za-z_$][\w$]*)'
INDEX = {}
SOURCE = ROOT / 'mempool_group.v'
DECL = re.compile(r'^\s*(input|output|inout|wire)\s+(?:wire\s+)?(?:\[(-?\d+):(-?\d+)\]\s*)?(' + ID + r')\s*;', re.M)
INST = re.compile(r'^\s*(' + ID + r')\s+(' + ID + r')\s*\((.*?)\)\s*;', re.M | re.S)
PIN = re.compile(r'\.(' + ID + r')\s*\((.*?)\)(?:\s*,|\s*$)', re.S)
ASSIGN = re.compile(r'^\s*assign\s+(.*?)\s*=\s*(.*?);', re.M | re.S)
NET = re.compile(r'^(' + ID + r')\s*(?:\[(-?\d+)(?::(-?\d+))?\])?$')
CONST = re.compile(r"^(\d+)'([bhd])([0-9a-fxz_]+)$", re.I)


def name(s):
    return s[1:] if s.startswith('\\') else s


def span(a, b):
    return list(range(a, b + (1 if b >= a else -1), 1 if b >= a else -1))


def text_module(n):
    m = INDEX[n]
    with open(SOURCE, 'rb') as f:
        f.seek(m['start_byte'])
        return f.read(m['end_byte'] - m['start_byte']).decode()


def declarations(t):
    result = {}
    ports = {}
    for m in DECL.finditer(t):
        kind, hi, lo, n = m.groups()
        n = name(n)
        bits = [(n, None)] if hi is None else [(n, i) for i in span(int(hi), int(lo))]
        result[n] = bits
        if kind != 'wire':
            ports[n] = {'direction': kind, 'width': len(bits), 'bits': bits,
                        'msb': None if hi is None else int(hi), 'lsb': None if lo is None else int(lo)}
    return result, ports


def expr(s, decls):
    s = s.strip()
    if not s:
        return []
    if s.startswith('{') and s.endswith('}'):
        contents = s[1:-1]
        pieces = []
        depth = 0
        start = 0
        escaped = False
        for i, c in enumerate(contents):
            if escaped:
                if c.isspace():
                    escaped = False
                continue
            if c == '\\':
                escaped = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == ',' and depth == 0:
                pieces.extend(expr(contents[start:i], decls))
                start = i + 1
        pieces.extend(expr(contents[start:], decls))
        return pieces
    m = CONST.fullmatch(s)
    if m:
        width, base, val = m.groups()
        width = int(width)
        if 'x' in val.lower() or 'z' in val.lower():
            raise ValueError('x/z constant expansion is outside this prototype subset: ' + s)
        binary = format(int(val.replace('_', ''), {'b': 2, 'h': 16, 'd': 10}[base.lower()]), 'b').zfill(width)[-width:]
        return [('__CONST__', c) for c in binary]
    m = NET.fullmatch(s)
    if not m:
        raise ValueError('unsupported expression: ' + s[:150])
    n, hi, lo = m.groups()
    n = name(n)
    if n not in decls:
        raise ValueError('undeclared identifier: ' + n)
    if hi is None:
        return decls[n]
    if lo is None:
        result = [(n, int(hi))]
    else:
        result = [(n, i) for i in span(int(hi), int(lo))]
    if not all(bit in decls[n] for bit in result):
        raise ValueError('out-of-range bit select: ' + s)
    return result


def parse(n, only_hier=False):
    t = text_module(n)
    decls, ports = declarations(t)
    instances = []
    failures = []
    coverage = {'selected_instance_statements': 0, 'named_pin_bindings': 0, 'unmatched_binding_bodies': 0, 'empty_bindings': 0}
    for m in INST.finditer(t):
        typ, inst, body = m.groups()
        if typ == 'module':
            continue
        typ, inst = name(typ), name(inst)
        if only_hier and typ not in INDEX:
            continue
        coverage['selected_instance_statements'] += 1
        pins = {}
        previous = 0
        for pm in PIN.finditer(body):
            p, ex = pm.groups()
            coverage['named_pin_bindings'] += 1
            if body[previous:pm.start()].strip():
                failures.append({'inst': inst, 'error': 'unmatched named-binding text: ' + body[previous:pm.start()][:150]})
                coverage['unmatched_binding_bodies'] += 1
            previous = pm.end()
            if name(p) in pins:
                failures.append({'inst': inst, 'error': 'duplicate named pin: ' + name(p)})
            if not ex.strip():
                coverage['empty_bindings'] += 1
            try:
                pins[name(p)] = expr(ex, decls)
            except ValueError as e:
                failures.append({'inst': inst, 'pin': p, 'error': str(e)})
        if body[previous:].strip():
            failures.append({'inst': inst, 'error': 'unmatched named-binding text: ' + body[previous:][:150]})
            coverage['unmatched_binding_bodies'] += 1
        instances.append({'type': typ, 'name': inst, 'line': INDEX[n]['start_line'] + t.count('\n', 0, m.start(1)), 'pins': pins})
    assignments = []
    for m in ASSIGN.finditer(t):
        try:
            lhs, rhs = [expr(s, decls) for s in m.groups()]
            if len(lhs) != len(rhs):
                raise ValueError('assign width mismatch')
            assignments.extend(zip(lhs, rhs))
        except ValueError as e:
            failures.append({'assign': m.group()[:150], 'error': str(e)})
    return {'name': n, 'ports': ports, 'instances': instances, 'assignments': assignments, 'failures': failures, 'coverage': coverage}


def analyze(module):
    top = parse(module, only_hier=True)
    top_hier = top['instances']
    child_ports = {i['type']: declarations(text_module(i['type']))[1] for i in top_hier}
    endpoints = collections.defaultdict(lambda: {'out': [], 'in': []})
    parent = {}

    def find(x):
        while x in parent and parent[x] != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            if a[0] == '__CONST__':
                parent[b] = a
            else:
                parent[a] = b

    for a, b in top['assignments']:
        union(a, b)
    width_errors = []
    skipped_inout = []
    empty_bindings = []
    for inst in top_hier:
        for pin, bits in inst['pins'].items():
            if pin not in child_ports[inst['type']]:
                width_errors.append({'instance':inst['name'], 'pin':pin, 'error':'formal port declaration not found'})
                continue
            p = child_ports[inst['type']][pin]
            if not bits:
                empty_bindings.append({'instance':inst['name'], 'pin':pin})
                continue
            if len(bits) != p['width']:
                width_errors.append({'instance': inst['name'], 'pin': pin, 'actual': len(bits), 'formal': p['width']})
                continue
            direction = {'output': 'out', 'input': 'in', 'inout': 'both'}[p['direction']]
            if direction == 'both':
                skipped_inout.append({'instance':inst['name'], 'pin':pin})
            for formal, bit in zip(p['bits'], bits):
                bit = find(bit)
                if bit[0] == '__CONST__':
                    continue
                if direction == 'both':
                    continue
                endpoints[bit][direction].append((inst['name'], pin, formal[1]))
    for p in top['ports'].values():
        direction = 'out' if p['direction'] == 'input' else 'in'
        for bit in p['bits']:
            if find(bit)[0] != '__CONST__':
                endpoints[find(bit)][direction].append(('$PORT', bit[0], bit[1]))
    groups = collections.defaultdict(list)
    multidriver = []
    for bit, ends in endpoints.items():
        if len(set(ends['out'])) > 1:
            multidriver.append({'net':bit, 'known_boundary_sources':ends['out']})
        for src in ends['out']:
            for dst in ends['in']:
                if src[0] != dst[0]:
                    groups[(src[0], src[1], dst[0], dst[1])].append({'source_bit': src[2], 'sink_bit': dst[2], 'net': bit})
    grouped = [{'source': key[0], 'source_pin': key[1], 'sink': key[2], 'sink_pin': key[3], 'direct_bit_connections': len(v), 'examples': v[:8]} for key, v in groups.items()]
    result = {
        'scope': 'Top-level hierarchy ports and explicit assign aliases; no undefined leaf traversal.',
        'module': module, 'source_line': INDEX[module]['start_line'],
        'top_ports': {n: {k: v for k, v in p.items() if k != 'bits'} for n, p in top['ports'].items()},
        'instances': [{'name': i['name'], 'type': i['type'], 'line': i['line'], 'ports': {n: {k: v for k, v in p.items() if k != 'bits'} for n, p in child_ports[i['type']].items()}} for i in top_hier],
        'direct_connections': sorted(grouped, key=lambda e: -e['direct_bit_connections']),
        'parse_failures': top['failures'], 'width_errors': width_errors,
        'coverage': top['coverage'], 'skipped_inout':skipped_inout, 'empty_bindings':empty_bindings,
        'multiple_known_boundary_sources':multidriver,
        'limitations':['Only selected structural hierarchy boundary pins and simple assigns are traversed. Undefined leaf cells are not traversed.', 'Zero selected-binding errors does not establish full Verilog semantic coverage.', 'No unsupported positional or parameterized instances are elaborated; compare selected instance counts with full inventory.', 'Multiple boundary sources can include alias ports and are not a signoff multiple-driver diagnosis.'],
    }
    return top, result


def analyze_file(netlist, index_path, module, out, bindings):
    """Analyze one boundary; calls are sequential because parser context is global."""
    global INDEX, SOURCE
    SOURCE = pathlib.Path(netlist)
    modules = json.loads(pathlib.Path(index_path).read_text(encoding='utf-8'))
    INDEX = {x['name']: x for x in modules}
    if len(INDEX) != len(modules):
        raise ValueError('Duplicate module names in index; cannot pick a definition safely.')
    if module not in INDEX:
        raise ValueError('Module not found: ' + module)
    for target in (out, bindings):
        pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    top, result = analyze(module)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(bindings, 'w', encoding='utf-8') as f:
        json.dump(top, f, ensure_ascii=False)
    return top, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--netlist', '--source', type=pathlib.Path, default=SOURCE)
    parser.add_argument('--index', type=pathlib.Path, default=ROOT / 'module_index.json')
    parser.add_argument('--module', default='mempool_group')
    parser.add_argument('--out', type=pathlib.Path, default=ROOT / 'connectivity.json')
    parser.add_argument('--bindings', type=pathlib.Path, default=ROOT / 'top_instances_connections.json')
    args = parser.parse_args(argv)
    top, result = analyze_file(args.netlist, args.index, args.module, args.out, args.bindings)
    print(json.dumps({'module':args.module, 'top_instances': len(top['instances']), 'failures': len(top['failures']), 'width_errors': len(result['width_errors']), 'direct_pin_group_edges': len(result['direct_connections'])}, indent=2))
    print('Tile 0/local interco:')
    for e in result['direct_connections']:
        if e['source'].startswith('gen_tiles[0]') and e['sink'] == 'i_local_interco' or e['sink'].startswith('gen_tiles[0]') and e['source'] == 'i_local_interco':
            print(json.dumps(e, ensure_ascii=False))


if __name__ == '__main__':
    main()
