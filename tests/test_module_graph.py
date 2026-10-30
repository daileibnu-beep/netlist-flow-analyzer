"""Module-level rendering preserves counts, direction, stroke widths and SVG identity."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from render_module_graph import aggregate_modules, inline_svg, module_dot, write_outputs


def fixture():
    result = {'schema_version': 1, 'root': 'top', 'top': 'top', 'depth': 1,
              'selection': {'expand': [], 'stop': []}, 'expanded_scopes': [], 'coverage': {},
              'diagnostics': {key: [] for key in ['undefined_leaves','skipped_inout_ports',
                  'empty_or_omitted_ports','constant_bindings','multiple_known_sources']},
              'nodes': [{'id':'top/A','path':['top','A'],'module_type':'producer','kind':'module'},
                        {'id':'top/B','path':['top','B'],'module_type':'consumer','kind':'module'}],
              'edges': []}
    for source, sink, pin, count in [('A','B','secret_valid',1),('A','B','secret_bus',15),
                                    ('B','A','secret_return',4),('A','A','secret_feedback',9)]:
        result['edges'].append({'source':'top/'+source,'source_pin':pin,'sink':'top/'+sink,
                               'sink_pin':'input_'+pin,'direct_bit_connections':count,
                               'bit_connections':[],'examples':[]})
    return result


class ModuleGraphTests(unittest.TestCase):
    def test_merges_ports_preserves_direction_totals_and_weight_order(self):
        graph=fixture();before=copy.deepcopy(graph)
        result=aggregate_modules(graph)
        pairs={(e['source'],e['sink']):e for e in result['edges']}
        self.assertEqual(len(pairs),3)
        self.assertEqual(pairs['top/A','top/B']['bit_connections'],16)
        self.assertEqual(pairs['top/A','top/B']['port_pairs'],2)
        self.assertEqual(pairs['top/B','top/A']['bit_connections'],4)
        self.assertEqual(result['summary']['displayed_bit_connections'],29)
        self.assertLess(pairs['top/B','top/A']['penwidth'],pairs['top/A','top/A']['penwidth'])
        self.assertLess(pairs['top/A','top/A']['penwidth'],pairs['top/A','top/B']['penwidth'])
        dot=module_dot(graph,result)
        self.assertEqual(dot.count('"top/A" -> "top/B"'),1)
        self.assertNotIn('secret_valid',dot)
        self.assertNotIn('secret_bus',dot)
        self.assertEqual(graph,before)

    def test_explicit_threshold_reports_hidden_counts(self):
        result=aggregate_modules(fixture(),minimum=10)
        self.assertEqual(len(result['edges']),1)
        self.assertEqual(result['summary']['hidden_module_pair_edges'],2)
        self.assertEqual(result['summary']['hidden_bit_connections'],13)
        with self.assertRaises(ValueError):aggregate_modules(fixture(),minimum=0)

    def test_one_bit_and_empty_graphs_have_valid_widths(self):
        graph=fixture();graph['edges']=graph['edges'][:1]
        self.assertEqual(aggregate_modules(graph)['edges'][0]['penwidth'],1)
        graph['edges']=[]
        self.assertEqual(aggregate_modules(graph)['summary']['all_bit_connections'],0)

    def test_svg_is_embedded_exactly_and_css_does_not_override_strokes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);graph=fixture();write_outputs(graph,folder)
            svg=(folder/'flow.svg').read_text();html=(folder/'flow.html').read_text()
            self.assertIn(inline_svg(svg),html)
            self.assertNotIn('svg path{',html)
            self.assertNotIn('__MODULE_SVG__',html)
            root=ET.fromstring(svg);ns={'s':'http://www.w3.org/2000/svg'}
            widths=[float(p.get('stroke-width','1')) for p in root.findall('.//s:g[@class="edge"]//s:path',ns)]
            self.assertEqual(len(widths),3)
            self.assertGreater(max(widths),min(widths))
            self.assertEqual(json.loads((folder/'flow.json').read_text()),graph)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for viewer runtime checks")
    def test_large_evidence_is_lazy_and_blocked_preview_recovers(self):
        graph=fixture();graph['edges'][0]['examples']=[{'proof':'LAZY_PROOF_'+('x'*2000000)}]
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);write_outputs(graph,folder)
            html=(folder/'flow.html').read_text();sidecar=(folder/'flow_details.js').read_text()
            self.assertLess(len(html.encode()),60000)
            self.assertNotIn('LAZY_PROOF_',html)
            self.assertNotIn('secret_valid',html)
            payload=json.loads(sidecar.removeprefix('window.__FLOW_DETAILS__=').removesuffix(';\n'))
            self.assertEqual(payload['edges'],graph['edges'])
            self.assertEqual(json.loads((folder/'flow.json').read_text()),graph)
            run=subprocess.run(['node',str(ROOT/'tests/check_flow_viewer.js'),str(folder/'flow.html')],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertIn('blocked_details_recovery',run.stdout)

    def test_cli_redraw_retains_full_analysis_and_outputs_svg(self):
        with tempfile.TemporaryDirectory(prefix='module graph 中文 ') as temp:
            folder=Path(temp);data=folder/'analysis.json';data.write_text(json.dumps(fixture()))
            out=folder/'rendered'
            result=subprocess.run([sys.executable,str(ROOT/'analyze.py'),'redraw','--data',str(data),
                                   '--out',str(out),'--rankdir','TB'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((out/'flow.svg').exists())
            self.assertEqual(json.loads((out/'flow.json').read_text()),fixture())
            self.assertEqual(json.loads((out/'flow_modules.json').read_text())['summary']['displayed_module_pair_edges'],3)

    def test_missing_graphviz_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temp,patch('render_module_graph.shutil.which',return_value=None):
            with self.assertRaisesRegex(ValueError,'Graphviz dot'):
                write_outputs(fixture(),temp)


if __name__=='__main__':unittest.main()
