"""Instance-aware, directed bit connectivity across structural module boundaries.

No leaf-cell function is inferred. A visible (unexpanded) module is a frontier:
its outputs are sources and inputs are sinks, with no input-to-output shortcut.
"""
import collections
import json
import re
from pathlib import Path

import analyze_connectivity as bits_parser
import scan_inventory as structural


def path_id(parts):
    return '/'.join(p.replace('~', '~0').replace('/', '~1') for p in parts)


def user_path(value, top):
    """Slash separates hierarchy; dots/brackets are literal identifier characters."""
    if not value or value == '/':
        return (top,)
    raw = value.strip('/').split('/')
    if any(not p or re.search(r'~(?![01])', p) for p in raw):
        raise ValueError('Invalid instance path: ' + value)
    parts = tuple(bits_parser.name(p.strip()).replace('~1', '/').replace('~0', '~') for p in raw)
    return parts if parts[0] == top else (top,) + parts


def beneath(path, ancestor):
    return path[:len(ancestor)] == ancestor


def vertex(scope, bit):
    return ('net', scope, bit[0], bit[1])


def endpoint(node, pin, bit):
    return ('pin', node, pin, bit)


def bit_record(key):
    return {'kind': key[0], 'scope': key[1], 'name': key[2], 'bit': key[3]}


class ModuleReader:
    """Cache syntax by module type; instantiate connections separately per path."""
    def __init__(self, source, index):
        self.source = Path(source)
        self.index = {}
        for item in index:
            name = bits_parser.name(item['name'])
            if name in self.index:
                raise ValueError('Duplicate normalized module name: ' + name)
            self.index[name] = item
        self.cache = {}

    def read(self, module):
        if module in self.cache:
            return self.cache[module]
        if module not in self.index:
            raise ValueError('Module definition not found: ' + module)
        entry = self.index[module]
        with self.source.open('rb') as stream:
            stream.seek(entry['start_byte'])
            text = stream.read(entry['end_byte'] - entry['start_byte']).decode('utf-8')
        clean = structural.LEX.sub(lambda m: re.sub(r'[^\r\n]', ' ', m.group(0))
                                   if m.group(0).startswith(('/*', '//')) else m.group(0), text)
        declarations, ports, instances, aliases, unsupported = {}, {}, [], [], []
        header_ports = None
        seen = set()
        # Line provenance follows original source, including comments/whitespace.
        line_cursor = 0
        for statement in structural.statements(clean):
            if not statement or statement == 'endmodule':
                continue
            offset = clean.find(statement, line_cursor)
            if offset < 0:
                raise ValueError('Cannot locate parsed statement in source: ' + module)
            line_cursor = offset + len(statement)
            line = entry['start_line'] + clean.count('\n', 0, offset)
            header = structural.HEADER.fullmatch(statement)
            if header:
                if bits_parser.name(header.group(1)) != module:
                    raise ValueError('Module index does not match source: ' + module)
                body = header.group(2)
                residue = re.sub(structural.IDENT, '', body)
                if residue.strip(' ,\t\r\n'):
                    unsupported.append('unsupported/ANSI module header')
                header_ports = [bits_parser.name(n) for n in re.findall(structural.IDENT, body)]
                continue
            declaration = structural.DECL.fullmatch(statement)
            if declaration:
                kind, body = declaration.groups()
                parsed = structural.parse_declaration(kind, body)
                if not parsed:
                    unsupported.append('declaration: ' + statement[:150])
                    continue
                for item in parsed:
                    name = bits_parser.name(item['name'])
                    labels = ([None] if item['msb'] is None else
                              bits_parser.span(item['msb'], item['lsb']))
                    values = [(name, label) for label in labels]
                    if name in declarations and declarations[name] != values:
                        unsupported.append('conflicting declaration: ' + name)
                    declarations[name] = values
                    if kind in ('input', 'output', 'inout'):
                        if name in ports:
                            unsupported.append('duplicate port: ' + name)
                        ports[name] = dict(item, name=name, bits=values)
                continue
            if statement.startswith('assign '):
                aliases.append({'statement': statement, 'source_line': line})
                continue
            match = structural.INST.match(statement)
            if match and structural.validate_instance_tail(statement, match.end() - 1):
                typ, name = map(bits_parser.name, match.groups())
                if name in seen:
                    unsupported.append('duplicate instance: ' + name)
                seen.add(name)
                instances.append({'type': typ, 'name': name, 'line': line,
                                  'body': statement[match.end():].rsplit(')', 1)[0]})
                continue
            unsupported.append(statement[:150])
        if header_ports is None or set(header_ports) != set(ports):
            unsupported.append('non-ANSI header/declared port mismatch')
        data = {'module': module, 'declarations': declarations, 'ports': ports,
                'instances': instances, 'assign_statements': aliases,
                'unsupported': unsupported, 'source_line': entry['start_line']}
        self.cache[module] = data
        return data

    def validate_interface(self, module):
        data = self.read(module)
        # Frontier internals are intentionally unexamined. Port declarations must
        # nevertheless be complete before their bit order can be trusted.
        interface_errors = [e for e in data['unsupported'] if any(
            tag in e for tag in ('header', 'declaration', 'duplicate port'))]
        if interface_errors:
            raise ValueError('{} interface: {}'.format(module, '; '.join(interface_errors[:4])))
        return data

    def bindings(self, module, instance):
        decls = self.read(module)['declarations']
        body = instance['body']
        pins, previous = {}, 0
        for match in bits_parser.PIN.finditer(body):
            pin, expression = match.groups()
            pin = bits_parser.name(pin)
            if body[previous:match.start()].strip() or pin in pins:
                raise ValueError('Unsupported/duplicate named binding: ' + instance['name'])
            previous = match.end()
            if re.search(r"\b0'[bhd]", expression, re.I):
                raise ValueError('Zero-width constant')
            pins[pin] = bits_parser.expr(expression, decls)
        if body[previous:].strip():
            raise ValueError('Unsupported positional/parameterized binding: ' + instance['name'])
        return pins


class HierarchyFlow:
    def __init__(self, reader, top, root=None, depth=1, expand=(), stop=(),
                 max_instances=200000, max_bits=2000000, max_pairs=200000,
                 expand_depth=1, expand_at=()):
        if depth < 1 or depth > 64:
            raise ValueError('--depth must be between 1 and 64')
        if min(max_instances, max_bits, max_pairs) < 1:
            raise ValueError('Resource limits must be positive')
        top = bits_parser.name(top)
        self.reader, self.top = reader, top
        self.root = user_path(root, top)
        self.depth = depth
        if expand_depth < 1 or expand_depth > 64:
            raise ValueError('--expand-depth must be between 1 and 64')
        self.expand = {user_path(p, top): expand_depth for p in expand}
        for value, levels in expand_at:
            path, levels = user_path(value, top), int(levels)
            if levels < 1 or levels > 64:
                raise ValueError('Instance expansion depth must be between 1 and 64')
            if path in self.expand and self.expand[path] != levels:
                raise ValueError('Conflicting expansion depths: ' + path_id(path))
            self.expand[path] = levels
        self.stop = {user_path(p, top) for p in stop}
        for path in set(self.expand) | self.stop:
            if not beneath(path, self.root):
                raise ValueError('Selection is outside --root: ' + path_id(path))
        if self.root in self.stop:
            raise ValueError('--stop cannot be the analysis root')
        for stopped in self.stop:
            if any(beneath(expanded, stopped) for expanded in self.expand):
                raise ValueError('--expand is at/below a stopped path: ' + path_id(stopped))
        self.max_instances, self.max_bits, self.max_pairs = max_instances, max_bits, max_pairs
        self.adj = collections.defaultdict(list)
        self.vertices = set()
        self.sources, self.sinks = set(), set()
        self.nodes, self.expanded, self.port_bridges = {}, [], []
        self.opaque, self.inouts, self.empty, self.constants = [], [], [], []
        self.visited_paths = set()
        self.instance_count = 0

    def resolve(self, path):
        module = self.top
        if module not in self.reader.index:
            raise ValueError('Top module not found: ' + module)
        for index, part in enumerate(path[1:], 1):
            data = self.reader.read(module)
            matches = [i for i in data['instances'] if i['name'] == part]
            if len(matches) != 1:
                raise ValueError('Instance path not found/ambiguous: ' + path_id(path[:index+1]))
            module = matches[0]['type']
            if module not in self.reader.index:
                raise ValueError('Cannot descend into undefined leaf: ' + path_id(path[:index+1]))
        return module

    def add_edge(self, source, sink, evidence):
        self.vertices.update((source, sink))
        if len(self.vertices) > self.max_bits:
            raise ValueError('Exceeded --max-bits; narrow the root/depth or raise the limit')
        self.adj[source].append((sink, evidence))

    def frontier(self, path, module, reason):
        name = path_id(path)
        self.nodes[name] = {'id': name, 'path': list(path), 'module_type': module,
                            'kind': 'module', 'stop_reason': reason,
                            'source_line': self.reader.index[module]['start_line']}
        return name

    def expansion(self, path, depth, child):
        if path in self.stop:
            return False, 'explicit_stop'
        if any(beneath(selected, path) for selected in self.expand):
            # Expand the selected instance itself and the ancestors needed to reach it.
            return True, 'explicit_expand'
        relative = [(selected, levels) for selected, levels in self.expand.items()
                    if beneath(path, selected)]
        if relative:
            selected, levels = max(relative, key=lambda item: len(item[0]))
            if len(path) - len(selected) >= levels:
                return False, 'instance_depth_limit'
        elif depth >= self.depth:
            return False, 'depth_limit'
        # Keep the deepest defined module as a visible boundary by default.
        # Pure wire/assign wrappers can still be traversed without a cell model.
        has_children = any(i['type'] in self.reader.index for i in child['instances'])
        pure_alias = not child['instances'] and bool(child['assign_statements'])
        if not has_children and not pure_alias:
            return False, 'no_defined_children'
        return True, 'depth'


    def visit(self, path, module, depth, ancestors=()):
        if module in ancestors:
            raise ValueError('Recursive module hierarchy: ' + path_id(path))
        if len(path) - len(self.root) > 64:
            raise ValueError('Hierarchy expansion exceeds 64 levels')
        data = self.reader.validate_interface(module)
        if data['unsupported']:
            raise ValueError('{} has unsupported structural statements: {}'.format(
                path_id(path), '; '.join(data['unsupported'][:4])))
        scope = path_id(path)
        self.visited_paths.add(path)
        self.expanded.append({'id': scope, 'module_type': module,
                              'source_line': data['source_line']})
        for assignment in data['assign_statements']:
            statement = assignment['statement']
            match = re.fullmatch(r'assign\s+(.*?)\s*=\s*(.*)', statement, re.S)
            if not match:
                raise ValueError('Unsupported assign in ' + scope)
            lhs, rhs = [bits_parser.expr(e, data['declarations']) for e in match.groups()]
            if len(lhs) != len(rhs) or not lhs:
                raise ValueError('Assign width mismatch/empty assign in ' + scope)
            for left, right in zip(lhs, rhs):
                if left[0] == '__CONST__':
                    raise ValueError('Constant on assign lhs in ' + scope)
                ev = {'kind': 'assign', 'scope': scope, 'module_type': module,
                      'lhs': list(left), 'rhs': list(right), 'expression': statement,
                      'source_line': assignment['source_line']}
                if right[0] == '__CONST__':
                    self.constants.append(ev)
                else:
                    # Preserve assign direction: no undirected alias union.
                    self.add_edge(vertex(scope, right), vertex(scope, left), ev)
        for instance in data['instances']:
            child_path = path + (instance['name'],)
            child_scope = path_id(child_path)
            self.visited_paths.add(child_path)
            self.instance_count += 1
            if self.instance_count > self.max_instances:
                raise ValueError('Exceeded --max-instances; narrow the selected hierarchy')
            typ = instance['type']
            if typ not in self.reader.index:
                self.opaque.append({'id': child_scope, 'module_type': typ, 'line': instance['line'],
                                    'reason': 'undefined leaf; pin directions/function unknown'})
                continue
            child = self.reader.validate_interface(typ)
            pins = self.reader.bindings(module, instance)
            expand, reason = self.expansion(child_path, depth + 1, child)
            if expand:
                target_node = None
            else:
                target_node = self.frontier(child_path, typ, reason)
            missing = set(child['ports']) - set(pins)
            for pin in sorted(missing):
                self.empty.append({'instance': child_scope, 'pin': pin, 'reason': 'omitted port'})
            bridges = []
            for pin, actual in pins.items():
                if pin not in child['ports']:
                    raise ValueError('Formal port not found: ' + child_scope + '.' + pin)
                formal = child['ports'][pin]
                if not actual:
                    self.empty.append({'instance': child_scope, 'pin': pin, 'reason': 'empty binding'})
                    continue
                if len(actual) != formal['width']:
                    raise ValueError('Port width mismatch: {}.{} actual={} formal={}'.format(
                        child_scope, pin, len(actual), formal['width']))
                if formal['direction'] == 'inout':
                    self.inouts.append({'instance': child_scope, 'pin': pin})
                    continue
                if target_node is not None:
                    target_set = self.sources if formal['direction'] == 'output' else self.sinks
                    target_set.update(endpoint(target_node, pin, f[1]) for f in formal['bits'])
                for f, a in zip(formal['bits'], actual):
                    ev = {'kind': 'port_binding', 'parent_scope': scope, 'instance': child_scope,
                          'module_type': typ, 'port': pin, 'port_bit': f[1],
                          'direction': formal['direction'], 'parent_net_bit': list(a),
                          'source_line': instance['line']}
                    bridges.append(ev)
                    if a[0] == '__CONST__':
                        if formal['direction'] != 'input':
                            raise ValueError('Output port bound to constant: ' + child_scope + '.' + pin)
                        self.constants.append(ev)
                        continue
                    parent_key = vertex(scope, a)
                    child_key = vertex(child_scope, f) if expand else endpoint(target_node, pin, f[1])
                    if formal['direction'] == 'input':
                        self.add_edge(parent_key, child_key, ev)
                    else:
                        self.add_edge(child_key, parent_key, ev)
            if expand:
                # Export every parent-child mapping for audit, not only examples.
                self.port_bridges.extend(bridges)
                self.visit(child_path, typ, depth + 1, ancestors + (module,))

    def graph(self):
        # Resolve all user-supplied paths, including branches hidden by an ancestor stop.
        root_module = self.resolve(self.root)
        for path in set(self.expand) | self.stop:
            self.resolve(path)
            if any(path != stopped and beneath(path, stopped) for stopped in self.stop):
                raise ValueError('Selection hidden by ancestor stop: ' + path_id(path))
        scope = path_id(self.root)
        root_data = self.reader.validate_interface(root_module)
        boundary_node = scope + '/$PORT'
        if '$PORT' in [i['name'] for i in root_data['instances']]:
            raise ValueError('Reserved boundary name $PORT collides with a root child')
        self.nodes[boundary_node] = {'id': boundary_node, 'path': list(self.root),
                                     'module_type': root_module, 'kind': 'scope_boundary'}
        for pin, formal in root_data['ports'].items():
            if formal['direction'] == 'inout':
                self.inouts.append({'instance': scope, 'pin': pin})
                continue
            target_set = self.sources if formal['direction'] == 'input' else self.sinks
            for bit in formal['bits']:
                end = endpoint(boundary_node, pin, bit[1])
                target_set.add(end)
                ev = {'kind': 'scope_port', 'scope': scope, 'port': pin,
                      'port_bit': bit[1], 'direction': formal['direction']}
                if formal['direction'] == 'input':
                    self.add_edge(end, vertex(scope, bit), ev)
                else:
                    self.add_edge(vertex(scope, bit), end, ev)
        self.visit(self.root, root_module, 0)
        for selected in set(self.expand) | self.stop:
            if selected not in self.visited_paths:
                raise ValueError('Selected path was not reached: ' + path_id(selected))
        groups = {}
        pairs = 0
        sink_drivers = collections.defaultdict(set)
        # Each source-bit/sink-bit pair is counted once even if aliases give
        # multiple routes. BFS retains a shortest complete witness per sample.
        for source in sorted(self.sources, key=repr):
            queue = collections.deque([source])
            previous = {source: None}
            while queue:
                here = queue.popleft()
                if here in self.sinks:
                    pairs += 1
                    if pairs > self.max_pairs:
                        raise ValueError('Exceeded --max-pairs; narrow hierarchy or raise the limit')
                    key = (source[1], source[2], here[1], here[2])
                    if key not in groups:
                        groups[key] = {'source': key[0], 'source_pin': key[1], 'sink': key[2],
                                       'sink_pin': key[3], 'bit_connections': [], 'examples': []}
                    group = groups[key]
                    group['bit_connections'].append({'source_bit': source[3], 'sink_bit': here[3]})
                    sink_drivers[here].add(source)
                    if len(group['examples']) < 8:
                        hops, cursor = [], here
                        while previous[cursor] is not None:
                            prev, evidence = previous[cursor]
                            hops.append({'from': bit_record(prev), 'to': bit_record(cursor), **evidence})
                            cursor = prev
                        group['examples'].append({'source_bit': source[3], 'sink_bit': here[3],
                                                  'hops': list(reversed(hops))})
                    continue
                for target, evidence in self.adj.get(here, ()):
                    if target not in previous:
                        previous[target] = (here, evidence)
                        queue.append(target)
        edges = sorted(groups.values(), key=lambda e: (e['source'], e['source_pin'], e['sink'], e['sink_pin']))
        for edge in edges:
            edge['direct_bit_connections'] = len(edge['bit_connections'])
        matched_sources = {(e['source'], e['source_pin'], p['source_bit'])
                           for e in edges for p in e['bit_connections']}
        matched_sinks = {(e['sink'], e['sink_pin'], p['sink_bit'])
                         for e in edges for p in e['bit_connections']}
        return {'schema_version': 1, 'graph_kind': 'cross_hierarchy_structural_connectivity',
                'top': self.top, 'root': scope, 'depth': self.depth,
                'selection': {'expand': [{'path': path_id(path), 'depth': levels}
                                         for path, levels in sorted(self.expand.items())],
                              'stop': sorted(map(path_id, self.stop))},
                'nodes': list(self.nodes.values()), 'expanded_scopes': self.expanded,
                'edges': edges, 'expanded_port_bridges': self.port_bridges,
                'coverage': {'visible_nodes': len(self.nodes), 'expanded_scopes': len(self.expanded),
                    'encountered_instances': self.instance_count, 'bit_graph_vertices': len(self.vertices),
                    'source_pin_bits': len(self.sources), 'sink_pin_bits': len(self.sinks),
                    'connected_source_pin_bits': len(matched_sources), 'connected_sink_pin_bits': len(matched_sinks),
                    'unconnected_source_pin_bits': len(self.sources) - len(matched_sources),
                    'unconnected_sink_pin_bits': len(self.sinks) - len(matched_sinks),
                    'port_pair_edges': len(edges), 'source_sink_bit_pairs': pairs,
                    'undefined_leaf_instances': len(self.opaque), 'skipped_inout_ports': len(self.inouts),
                    'empty_or_omitted_ports': len(self.empty), 'constant_bindings': len(self.constants)},
                'diagnostics': {'undefined_leaves': self.opaque, 'skipped_inout_ports': self.inouts,
                    'empty_or_omitted_ports': self.empty, 'constant_bindings': self.constants,
                    'multiple_known_sources': [{'sink': bit_record(sink), 'sources': [bit_record(s) for s in sorted(sources, key=repr)]}
                                               for sink, sources in sink_drivers.items() if len(sources) > 1]},
                'limitations': [
                    'Directed connectivity only; not functional reachability, traffic, latency or timing.',
                    'Expanded module ports are stitched bit by bit; frontier inputs/outputs are never connected through inferred cell function.',
                    'Undefined leaf pins/functions and inout are not traversed; missing connections remain unknown.',
                    'Constants are recorded separately and never short different nets together.',
                    'Every bit mapping is retained; at most 8 shortest path witnesses per port pair are retained.',
                    'Graph may contain self-edges and multiple known sources; not an electrical signoff check.']}


def write_outputs(graph, output, **render_options):
    """Render one counted arrow per directed module pair, with SVG embedded in HTML."""
    from render_module_graph import write_outputs as render_outputs
    return render_outputs(graph, output, **render_options)
