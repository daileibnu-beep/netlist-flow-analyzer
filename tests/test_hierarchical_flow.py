"""Cross-level instance identity, bit order, evidence and conservative boundaries."""
import collections
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from hierarchical_flow import HierarchyFlow, ModuleReader, path_id, user_path
from scan_inventory import index_modules

BASE = '''module tx (q);
output [3:0] q;
UNKNOWN driver (.Y(q));
endmodule
module rx (d);
input [3:0] d;
UNKNOWN receiver (.A(d));
endmodule
module wrap_tx (o);
output [3:0] o;
tx u_tx (.q(o));
endmodule
module wrap_rx (i);
input [0:3] i;
rx u_rx (.d({i[0], i[1], i[2], i[3]}));
endmodule
module chip ();
wire [3:0] n;
wire [3:0] m;
wire [3:0] alias;
assign alias = n;
wrap_tx A (.o(n));
wrap_rx B (.i({alias[0], alias[1], alias[2], alias[3]}));
wrap_tx A2 (.o(m));
wrap_rx B2 (.i(m));
endmodule
'''


class HierarchicalFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='flow 中文 ')
        self.root = Path(self.temp.name)
        self.source = self.root / 'chip.v'

    def tearDown(self):
        self.temp.cleanup()

    def graph(self, text=BASE, **options):
        self.source.write_text(text)
        index, _, _ = index_modules(self.source)
        return HierarchyFlow(ModuleReader(self.source, index), 'chip', **options).graph()

    def pair_edges(self, graph):
        return {(e['source'], e['sink']): e for e in graph['edges']}

    def test_depth_two_stitches_ports_preserves_instances_and_reversed_bits(self):
        graph = self.graph(depth=2)
        edges = self.pair_edges(graph)
        self.assertEqual(set(edges), {('chip/A/u_tx', 'chip/B/u_rx'), ('chip/A2/u_tx', 'chip/B2/u_rx')})
        edge = edges['chip/A/u_tx', 'chip/B/u_rx']
        self.assertEqual(edge['direct_bit_connections'], 4)
        self.assertEqual({(p['source_bit'], p['sink_bit']) for p in edge['bit_connections']},
                         {(0, 3), (1, 2), (2, 1), (3, 0)})
        for example in edge['examples']:
            hops = example['hops']
            self.assertEqual(hops[0]['from']['scope'], 'chip/A/u_tx')
            self.assertEqual(hops[-1]['to']['scope'], 'chip/B/u_rx')
            self.assertTrue(any(h['kind'] == 'assign' for h in hops))
            self.assertEqual({h['instance'] for h in hops if h['kind'] == 'port_binding'},
                             {'chip/A', 'chip/A/u_tx', 'chip/B', 'chip/B/u_rx'})
            self.assertTrue(all(a['to'] == b['from'] for a, b in zip(hops, hops[1:])))
        self.assertEqual(len(graph['expanded_port_bridges']), 16)

    def test_mixed_depth_expand_and_stop_are_instance_specific(self):
        graph = self.graph(depth=1, expand=['chip/A'])
        self.assertEqual(set(self.pair_edges(graph)), {('chip/A/u_tx', 'chip/B'), ('chip/A2', 'chip/B2')})
        graph = self.graph(depth=2, stop=['chip/A'])
        self.assertEqual(set(self.pair_edges(graph)), {('chip/A', 'chip/B/u_rx'), ('chip/A2/u_tx', 'chip/B2/u_rx')})
        node = next(n for n in graph['nodes'] if n['id'] == 'chip/A')
        self.assertEqual(node['stop_reason'], 'explicit_stop')

    def test_per_instance_relative_depths_and_override_global_depth(self):
        middle = """module middle (q);
output [3:0] q;
tx u_inner (.q(q));
endmodule
"""
        text = BASE.replace('module wrap_tx', middle + 'module wrap_tx')
        text = text.replace('tx u_tx (.q(o));', 'middle u_tx (.q(o));')
        graph = self.graph(text, depth=1, expand_at=[('A', 1), ('A2', 2)])
        self.assertEqual(set(self.pair_edges(graph)),
                         {('chip/A/u_tx', 'chip/B'), ('chip/A2/u_tx/u_inner', 'chip/B2')})
        graph = self.graph(text, depth=4, expand=['A'], expand_depth=1)
        self.assertIn(('chip/A/u_tx', 'chip/B/u_rx'), self.pair_edges(graph))
        self.assertNotIn('chip/A/u_tx', {s['id'] for s in graph['expanded_scopes']})
        graph = self.graph(text, depth=4, expand_at=[('chip', 1)])
        self.assertEqual(set(self.pair_edges(graph)), {('chip/A', 'chip/B'), ('chip/A2', 'chip/B2')})
        with self.assertRaisesRegex(ValueError, 'Conflicting expansion depths'):
            self.graph(expand=['A'], expand_at=[('A', 2)])
        self.source.write_text(text)
        out = self.root / 'relative depth cli'
        result = subprocess.run([sys.executable, str(ROOT / 'analyze.py'), 'flow', str(self.source),
            '--top', 'chip', '--depth', '1', '--expand-at', 'A', '1', '--expand-at', 'A2', '2',
            '--out', str(out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.pair_edges(json.loads((out / 'flow.json').read_text()))),
                         {('chip/A/u_tx', 'chip/B'), ('chip/A2/u_tx/u_inner', 'chip/B2')})

    def test_root_is_subtree_boundary_not_global_context(self):
        graph = self.graph(root='B', depth=1)
        self.assertEqual(graph['root'], 'chip/B')
        self.assertEqual(set(self.pair_edges(graph)), {('chip/B/$PORT', 'chip/B/u_rx')})
        edge = graph['edges'][0]
        self.assertEqual({(p['source_bit'], p['sink_bit']) for p in edge['bit_connections']},
                         {(0, 3), (1, 2), (2, 1), (3, 0)})

    def test_undefined_leaf_and_frontier_have_no_inferred_function(self):
        default = self.graph(depth=3)
        self.assertEqual(len(default['edges']), 2)
        self.assertTrue(all(n['stop_reason'] == 'no_defined_children'
                            for n in default['nodes'] if n['kind'] == 'module'))
        graph = self.graph(depth=3, expand=['A/u_tx', 'B/u_rx', 'A2/u_tx', 'B2/u_rx'])
        self.assertEqual(graph['edges'], [])
        self.assertEqual(graph['coverage']['undefined_leaf_instances'], 4)
        self.assertEqual(graph['coverage']['visible_nodes'], 1)
        text = BASE.replace('assign alias = n;', 'UNKNOWN buffer (.A(n), .Y(alias));')
        graph = self.graph(text, depth=2)
        self.assertEqual(set(self.pair_edges(graph)), {('chip/A2/u_tx', 'chip/B2/u_rx')})
        self.assertEqual(graph['coverage']['undefined_leaf_instances'], 1)

    def test_assign_direction_and_alternate_routes(self):
        backwards = BASE.replace('assign alias = n;', 'assign n = alias;')
        graph = self.graph(backwards, depth=2)
        self.assertNotIn(('chip/A/u_tx', 'chip/B/u_rx'), self.pair_edges(graph))
        alternate = BASE.replace('assign alias = n;',
            'wire [3:0] other;\nassign alias = n;\nassign other = n;\nassign alias = other;')
        graph = self.graph(alternate, depth=2)
        self.assertEqual(self.pair_edges(graph)['chip/A/u_tx', 'chip/B/u_rx']['direct_bit_connections'], 4)

    def test_constants_do_not_short_distinct_branches_and_inout_is_unknown(self):
        text = '''module pass (i, o);
input [3:0] i;
output [3:0] o;
assign o = i;
endmodule
module rx (d);
input [3:0] d;
endmodule
module chip (pad);
inout pad;
wire [3:0] a;
wire [3:0] b;
pass A (.i(4'b0000), .o(a));
pass B (.i(4'b0000), .o(b));
rx C (.d(a));
rx D (.d(b));
endmodule
'''
        graph = self.graph(text, depth=2, stop=['chip/C', 'chip/D'])
        self.assertEqual(graph['edges'], [])
        self.assertEqual(graph['coverage']['constant_bindings'], 8)
        self.assertEqual(graph['coverage']['skipped_inout_ports'], 1)

    def test_full_bit_mappings_not_reconstructed_from_examples(self):
        text = '''module tx (q);
output [15:0] q;
endmodule
module rx (d);
input [15:0] d;
endmodule
module chip ();
wire [15:0] n;
tx A (.q(n));
rx B (.d(n));
rx C (.d(n));
endmodule
'''
        graph = self.graph(text)
        self.assertEqual(graph['coverage']['source_sink_bit_pairs'], 32)
        self.assertEqual(len(graph['edges']), 2)
        self.assertTrue(all(len(e['bit_connections']) == 16 and len(e['examples']) == 8 for e in graph['edges']))

    def test_conflicting_selection_bad_width_and_unsupported_fail(self):
        cases = [({'root': 'missing'}, 'not found'),
                 ({'expand': ['chip/A'], 'stop': ['chip/A']}, 'stopped'),
                 ({'root': 'A', 'expand': ['chip/B']}, 'outside'),
                 ({'stop': ['chip/A', 'chip/A/u_tx'], 'depth': 2}, 'ancestor stop'),
                 ({'depth': 0}, 'depth'),
                 ({'max_pairs': 1}, 'max-pairs')]
        for options, message in cases:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                self.graph(**options)
        with self.assertRaisesRegex(ValueError, 'width mismatch'):
            self.graph(BASE.replace('wrap_tx A (.o(n))', 'wrap_tx A (.o(n[1:0]))'))
        with self.assertRaisesRegex(ValueError, 'unsupported expression'):
            self.graph(BASE.replace('assign alias = n;', 'assign alias = n ^ m;'))

    def test_escaped_names_are_literal_and_slashes_can_be_encoded(self):
        text = BASE.replace('wrap_tx A (.o(n))', 'wrap_tx \\gen.u[0]/A (.o(n))')
        graph = self.graph(text, expand=['chip/gen.u[0]~1A'])
        self.assertIn(('chip/gen.u[0]~1A/u_tx', 'chip/B'), self.pair_edges(graph))
        self.assertEqual(user_path('chip/gen.u[0]~1A', 'chip'), ('chip', 'gen.u[0]/A'))
        self.assertEqual(path_id(('chip', 'x~y/z')), 'chip/x~0y~1z')

    def test_cli_plain_and_gzip_outputs_and_no_overwrite(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                source = self.root / ('chip.v.gz' if compressed else 'chip.v')
                if compressed:
                    with gzip.open(source, 'wt') as stream:
                        stream.write(BASE)
                else:
                    source.write_text(BASE)
                out = self.root / ('out_gz' if compressed else 'out')
                cmd = [sys.executable, str(ROOT / 'analyze.py'), 'flow', str(source), '--top', 'chip',
                       '--depth', '2', '--out', str(out)]
                result = subprocess.run(cmd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for filename in ['flow.json', 'flow.dot', 'flow.html', 'run_manifest.json', 'module_index.json']:
                    self.assertTrue((out / filename).exists(), filename)
                graph = json.loads((out / 'flow.json').read_text())
                self.assertEqual(len(graph['edges']), 2)
                self.assertEqual(json.loads((out / 'run_manifest.json').read_text())['status'], 'complete')
                self.assertNotIn('__FLOW_DATA__', (out / 'flow.html').read_text())
                again = subprocess.run(cmd, capture_output=True, text=True)
                self.assertNotEqual(again.returncode, 0)

    def test_witness_source_lines_ignore_comments(self):
        text = BASE.replace('assign alias = n;', '// assign alias = n;\nassign alias = n;')
        text = text.replace('wrap_tx A (.o(n));', '// wrap_tx A (.o(n));\nwrap_tx A (.o(n));')
        graph = self.graph(text, depth=2)
        lines = text.splitlines()
        edge = self.pair_edges(graph)['chip/A/u_tx', 'chip/B/u_rx']
        for hop in edge['examples'][0]['hops']:
            if hop['kind'] == 'assign':
                self.assertEqual(lines[hop['source_line'] - 1], 'assign alias = n;')
            if hop['kind'] == 'port_binding' and hop['instance'] == 'chip/A':
                self.assertEqual(lines[hop['source_line'] - 1], 'wrap_tx A (.o(n));')

    def test_multiple_known_sources_and_self_feedback_are_retained(self):
        text = '''module node (i, o);
input i;
output o;
endmodule
module chip ();
wire n;
node A (.i(n), .o(n));
node B (.i(n), .o(n));
endmodule
'''
        graph = self.graph(text)
        self.assertEqual(graph['coverage']['source_sink_bit_pairs'], 4)
        self.assertEqual(len(graph['diagnostics']['multiple_known_sources']), 2)
        self.assertIn(('chip/A', 'chip/A'), self.pair_edges(graph))


if __name__ == '__main__':
    unittest.main()
