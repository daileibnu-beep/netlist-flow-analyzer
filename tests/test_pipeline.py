"""End-to-end contract tests for the unified entry; stdlib only."""
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from mempool_profile import derive_dma_groups

NETLIST = '''module child (a, y);
input [1:0] a;
output [1:0] y;
assign y = a;
endmodule
module chip (a, y);
input [1:0] a;
output [1:0] y;
wire [1:0] mid;
child u0 (.a(a), .y(mid));
child u1 (.a(mid), .y(y));
endmodule
'''


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='netlist test 中文 ')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'analyze.py'), *map(str, arguments)],
                              cwd=str(self.root), capture_output=True, text=True)

    def test_plain_and_gzip_preserve_directed_bits_and_legacy_cli(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                source = self.root / ('chip.v.gz' if compressed else 'chip.v')
                if compressed:
                    with gzip.open(source, 'wt') as stream:
                        stream.write(NETLIST)
                else:
                    source.write_text(NETLIST)
                output = self.root / ('gzip output' if compressed else 'plain output')
                result = self.cli('run', source, '--top', 'chip', '--analysis-only',
                                  '--inspect', 'child', '--out', output)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads((output / 'run_manifest.json').read_text())
                self.assertEqual(manifest['status'], 'complete')
                self.assertTrue(all(s['status'] == 'complete' for s in manifest['stages']))
                graph = json.loads((output / 'results/connectivity.json').read_text())
                edges = {(e['source'], e['sink']): e['direct_bit_connections']
                         for e in graph['direct_connections']}
                self.assertEqual(edges, {('u0', 'u1'): 2, ('$PORT', 'u0'): 2, ('u1', '$PORT'): 2})
                self.assertTrue((output / 'results/extra_01_connectivity.json').exists())
                netlist = output / 'results/chip.v' if compressed else source
                legacy = subprocess.run([sys.executable, str(ROOT / 'scripts/analyze_connectivity.py'),
                    '--netlist', str(netlist), '--index', str(output / 'results/module_index.json'),
                    '--module', 'chip', '--out', str(self.root / 'legacy/graph.json'),
                    '--bindings', str(self.root / 'legacy/bindings.json')], capture_output=True, text=True)
                self.assertEqual(legacy.returncode, 0, legacy.stderr)
                self.assertEqual(graph, json.loads((self.root / 'legacy/graph.json').read_text()))
                second = self.cli('run', source, '--top', 'chip', '--analysis-only', '--out', output)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn('已存在', second.stderr)

    def test_failed_stage_keeps_diagnostics_without_publishing(self):
        source = self.root / 'chip.v'
        source.write_text(NETLIST.replace('.a(mid)', '.a(unknown)'))
        output = self.root / 'failed'
        result = self.cli('run', source, '--top', 'chip', '--analysis-only', '--out', output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        work = next(self.root.glob('failed.working-*'))
        manifest = json.loads((work / 'run_manifest.json').read_text())
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['stages'][-1]['status'], 'failed')
        self.assertIn('覆盖检查失败', result.stderr)
        self.assertIn('undeclared identifier', (work / 'logs/03.log').read_text())

    def test_wrong_profile_and_empty_netlist_have_clear_errors(self):
        source = self.root / 'wrong.v'
        source.write_text('module mempool_group ();\nendmodule\n')
        result = self.cli('run', source, '--out', self.root / 'wrong')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHA256', result.stderr)
        source.write_text('')
        result = self.cli('run', source, '--analysis-only', '--out', self.root / 'empty')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No supported module', result.stderr)

    def test_dma_group_is_derived_from_edges(self):
        instances, edges = [], []
        for dma in range(4):
            demux = 'gen_dmas[{}].gen_dma_reqrsp_demux.i_reqrsp_demux'.format(dma)
            instances.append({'name': demux})
            for n in range(4):
                # Deliberately cross the assumed tile-number grouping.
                tile = (dma * 4 + n + 4) % 16
                tile_name = 'gen_tiles[{}].i_tile'.format(tile)
                instances.append({'name': tile_name})
                for src, sp, dst, dp, count in [
                    (demux, 'mst_req_o', tile_name, 'tcdm_dma_req_valid_i', 1),
                    (tile_name, 'tcdm_dma_req_ready_o', demux, 'mst_rsp_i', 1),
                    (tile_name, 'tcdm_dma_resp_valid_o', demux, 'mst_rsp_i', 1),
                    (demux, 'mst_req_o', tile_name, 'tcdm_dma_resp_ready_i', 1),
                    (tile_name, 'tcdm_dma_resp_o', demux, 'mst_rsp_i', 123)]:
                    edges.append(dict(source=src, source_pin=sp, sink=dst, sink_pin=dp,
                                      direct_bit_connections=count, examples=[]))
        graph = {'instances': instances, 'direct_connections': edges}
        groups = derive_dma_groups(graph, self.root)
        self.assertEqual([g['tile'] for g in groups if g['dma'] == 0], [4, 5, 6, 7])
        self.assertTrue(all(g['known_direct_response_bus_bits'] == 123 for g in groups))
        graph['direct_connections'] = edges[1:]
        with self.assertRaises(ValueError):
            derive_dma_groups(graph, self.root)


if __name__ == '__main__':
    unittest.main()
