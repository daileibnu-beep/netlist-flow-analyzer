#!/usr/bin/env python3
"""Structural inventory for the uploaded, elaborated Verilog subset.

Python standard library only. This is NOT a Verilog compiler or timing engine.
Escaped identifiers, comments and statement boundaries are lexed explicitly.
Anything outside the supported structural subset is counted and reported.
"""
import argparse
import collections
import gzip
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

IDENT = r'(?:\\[^\s]+|[A-Za-z_$][A-Za-z0-9_$]*)'
MODULE_START = re.compile(rb'^\s*module\s+(\\[^\s]+|[A-Za-z_$][A-Za-z0-9_$]*)')
MODULE_END = re.compile(rb'^\s*endmodule\b')
LEX = re.compile(r'\\[^\s]+|/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|;')
DECL = re.compile(r'^(input|output|inout|wire|tri|reg)\b\s*(.*?)\s*$', re.S)
RANGE = re.compile(r'^\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]\s*')
INST = re.compile(r'^(' + IDENT + r')\s+(' + IDENT + r')\s*\(', re.S)
PIN = re.compile(r'(?:^|,)\s*\.(' + IDENT + r')\s*\(')
HEADER = re.compile(r'^module\s+(' + IDENT + r')\s*\((.*)\)\s*$', re.S)


def statements(text):
    """Yield semicolon-delimited code, preserving escaped names verbatim."""
    pieces = []
    begin = 0
    for match in LEX.finditer(text):
        token = match.group(0)
        if token.startswith('/*') or token.startswith('//'):
            pieces.extend((text[begin:match.start()], '\n' * token.count('\n')))
            begin = match.end()
        elif token == ';':
            pieces.append(text[begin:match.start()])
            yield ''.join(pieces).strip()
            pieces = []
            begin = match.end()
    pieces.append(text[begin:])
    tail = ''.join(pieces).strip()
    if tail:
        yield tail


def parse_declaration(kind, body):
    # This file uses numeric, one-dimensional packed ranges. Reject expressions.
    for qualifier in ('wire', 'reg', 'tri', 'logic', 'signed', 'unsigned'):
        body = re.sub(r'^' + qualifier + r'\b\s*', '', body)
    msb = lsb = None
    m = RANGE.match(body)
    if m:
        msb, lsb = int(m.group(1)), int(m.group(2))
        body = body[m.end():]
    if body.startswith('['):
        return None
    names = re.findall(IDENT, body)
    # Verify the complete residue, avoiding comma splits inside escaped names.
    residue = re.sub(IDENT, '', body)
    if residue.strip(' ,\t\r\n'):
        return None
    width = abs(msb - lsb) + 1 if msb is not None else 1
    return [dict(name=n, direction=kind, msb=msb, lsb=lsb, width=width) for n in names]


def validate_instance_tail(text, opening):
    """Find the outer closing parenthesis; no multi-instance statements allowed."""
    # Tokenizing only parentheses and escaped identifiers avoids confusing a
    # parenthesis that is part of an escaped identifier with syntax.
    depth = 0
    for m in re.finditer(r'\\[^\s]+|[()]', text[opening:]):
        t = m.group(0)
        if t == '(':
            depth += 1
        elif t == ')':
            depth -= 1
            if depth == 0:
                return not text[opening + m.end():].strip()
    return False


def index_modules(path):
    index = []
    offset = 0
    active = None
    line_number = 0
    with path.open('rb') as stream:
        for line_number, line in enumerate(stream, 1):
            m = MODULE_START.match(line)
            if m:
                if active:
                    raise ValueError('Nested module declaration')
                active = dict(name=m.group(1).decode('utf-8'),
                              start_byte=offset, start_line=line_number)
            if MODULE_END.match(line):
                if not active:
                    raise ValueError('Unmatched endmodule')
                active.update(end_byte=offset + len(line), end_line=line_number)
                index.append(active)
                active = None
            offset += len(line)
    if active:
        raise ValueError('Unterminated module')
    if len({m['name'] for m in index}) != len(index):
        raise ValueError('Duplicate module definitions')
    return index, offset, line_number


def scan_inventory(source, out, top='mempool_group', index_path=None, write_index=None):
    """Scan a structural netlist, write artifacts and return the full inventory."""
    source, out = Path(source), Path(out)
    t0 = time.monotonic()
    out.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == '.gz':
        uncompressed = out.parent / source.stem
        with gzip.open(source, 'rb') as compressed_stream, uncompressed.open('wb') as dest:
            shutil.copyfileobj(compressed_stream, dest, 1024 * 1024)
        source = uncompressed
    if index_path and index_path.exists():
        index = json.loads(index_path.read_text())
        byte_count = source.stat().st_size
        with source.open('rb') as stream:
            stream.seek(index[-1]['end_byte'])
            suffix = stream.read()
        line_count = index[-1]['end_line'] + suffix.count(b'\n')
        if suffix and not suffix.endswith(b'\n'):
            line_count += 1
    else:
        index, byte_count, line_count = index_modules(source)
    if write_index:
        write_index.parent.mkdir(parents=True, exist_ok=True)
        write_index.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    if not index:
        raise ValueError('No supported module definitions found in netlist')
    module_names = {m['name'] for m in index}
    if len(module_names) != len(index):
        raise ValueError('Duplicate module definitions or a stale/truncated index')
    modules = {}
    all_types = collections.Counter()
    cell_pins = collections.defaultdict(set)
    global_unsupported = collections.Counter()
    examples = []
    with source.open('rb') as stream:
        for mi, entry in enumerate(index):
            stream.seek(entry['start_byte'])
            text = stream.read(entry['end_byte'] - entry['start_byte']).decode('utf-8')
            info = dict(entry)
            ports, children = [], []
            counts = collections.Counter()
            unsupported = collections.Counter()
            assigns = wires = wire_bits = 0
            positional = 0
            instance_names = set()
            duplicates = []
            header_ports = []
            for si, statement in enumerate(statements(text)):
                if not statement or statement == 'endmodule':
                    continue
                h = HEADER.fullmatch(statement)
                if h:
                    header_ports = re.findall(IDENT, h.group(2))
                    continue
                d = DECL.fullmatch(statement)
                if d:
                    parsed = parse_declaration(d.group(1), d.group(2))
                    if parsed is None:
                        unsupported['declaration'] += 1
                    elif d.group(1) in ('input', 'output', 'inout'):
                        ports.extend(parsed)
                    else:
                        wires += len(parsed)
                        wire_bits += sum(p['width'] for p in parsed)
                    continue
                if statement.startswith('assign '):
                    assigns += 1
                    continue
                im = INST.match(statement)
                if im and validate_instance_tail(statement, im.end() - 1):
                    cell, name = im.groups()
                    counts[cell] += 1
                    if name in instance_names:
                        duplicates.append(name)
                    instance_names.add(name)
                    if cell in module_names:
                        children.append(dict(type=cell, name=name))
                    pins = PIN.findall(statement[im.end():])
                    if not pins:
                        positional += 1
                    cell_pins[cell].update(pins)
                    continue
                keyword = statement.split(None, 1)[0][:80] if statement else '<empty>'
                unsupported[keyword] += 1
                if len(examples) < 40:
                    examples.append(dict(module=entry['name'], statement=statement[:250]))
            declared = [p['name'] for p in ports]
            info.update(ports=ports, header_ports=header_ports,
                        header_port_mismatch=sorted(set(header_ports) ^ set(declared)),
                        instance_counts=dict(counts), children=children,
                        instance_count=sum(counts.values()), assign_count=assigns,
                        wire_count=wires, declared_wire_bits=wire_bits,
                        unsupported=dict(unsupported), positional_instances=positional,
                        duplicate_instance_names=duplicates)
            modules[entry['name']] = info
            all_types.update(counts)
            global_unsupported.update(unsupported)
            if (mi + 1) % 1000 == 0:
                print(f'parsed {mi + 1}/{len(index)} modules', flush=True)
    leaf_types = set(all_types) - module_names
    referenced = {c['type'] for m in modules.values() for c in m['children']}
    roots = sorted(module_names - referenced)
    memo = {}
    visiting = set()
    def expand(name):
        if name in memo:
            return memo[name]
        if name in visiting:
            raise ValueError('Hierarchy cycle at ' + name)
        visiting.add(name)
        leaves = collections.Counter()
        levels = 0
        hierarchical_instances = 0
        for cell, n in modules[name]['instance_counts'].items():
            if cell in modules:
                subleaf, subhier, sublevel = expand(cell)
                leaves.update({k: v * n for k, v in subleaf.items()})
                hierarchical_instances += n * (1 + subhier)
                levels = max(levels, 1 + sublevel)
            else:
                leaves[cell] += n
        visiting.remove(name)
        memo[name] = (leaves, hierarchical_instances, levels)
        return memo[name]
    if top not in modules:
        raise ValueError('Top module not found: ' + top)
    top_leaves, top_hier, max_levels = expand(top)
    reachable = set(memo)
    for name in list(modules):
        leaf, h, lev = expand(name)
        modules[name]['expanded_leaf_count'] = sum(leaf.values())
        modules[name]['expanded_hierarchical_instances'] = h
        modules[name]['hierarchical_levels_below'] = lev
    summary = dict(module_definitions=len(modules), source_bytes=byte_count,
                   source_lines=line_count, top=top, roots=roots,
                   instance_declarations_all_definitions=sum(all_types.values()),
                   undefined_type_declarations_all_definitions=sum(all_types[t] for t in leaf_types),
                   undefined_leaf_types=len(leaf_types),
                   top_reachable_definitions=len(reachable),
                   top_unreachable_definitions=sorted(module_names - reachable),
                   top_expanded_undefined_leaf_instances=sum(top_leaves.values()),
                   top_expanded_hierarchical_instances=top_hier,
                   top_expanded_undefined_leaf_type_counts=dict(top_leaves),
                   max_hierarchy_edges_below_top=max_levels,
                   empty_module_definitions=[n for n,m in modules.items() if not m['instance_count'] and not m['assign_count']],
                   unsupported=dict(global_unsupported),
                   unsupported_examples=examples,
                   positional_instances=sum(m['positional_instances'] for m in modules.values()),
                   header_mismatches=[n for n,m in modules.items() if m['header_port_mismatch']],
                   duplicate_instance_names=[n for n,m in modules.items() if m['duplicate_instance_names']],
                   elapsed_seconds=round(time.monotonic()-t0,3))
    digest = hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    summary['uncompressed_source_sha256'] = digest.hexdigest()
    result = dict(scope='Uploaded netlist only; no external cell semantics or timing data',
                  parser_scope='Elaborated structural Verilog; explicit coverage diagnostics',
                  summary=summary,
                  undefined_leaf_pins={n:sorted(cell_pins[n]) for n in sorted(leaf_types)},
                  modules=modules)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('source', type=Path)
    ap.add_argument('--out', type=Path, default=Path('inventory.json'))
    ap.add_argument('--top', default='mempool_group')
    ap.add_argument('--index', type=Path)
    ap.add_argument('--write-index', type=Path,
                    help='Write the module byte/line index to this JSON path')
    args = ap.parse_args(argv)
    return scan_inventory(args.source, args.out, args.top, args.index, args.write_index)


if __name__ == '__main__':
    main()
