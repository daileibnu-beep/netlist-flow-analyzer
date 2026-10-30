"""Boundary traversal and strict snapshot import, independent of a Cadence install."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from innovus_flow import TABLES, build_graph, path_id


def fixture():
    def node(parts, kind='module'):
        return [path_id(parts) + ('/$PORT' if kind=='scope_boundary' else ''), json.dumps(parts), 'cell', kind, 'frontier']
    data={name:[] for name in TABLES}
    data['nodes']=[node(['top'],'scope_boundary'),node(['top','A']),node(['top','U','B']),node(['top','C'])]
    data['scopes']=[['top','["top"]','top','0'],['top/U','["top","U"]','wrapper','0']]
    # Same flat ID does NOT authorize joining r and s through folded cells.
    data['hnets']=[['r','top','r','flat1','0'],['s','top','s','flat1','0'],
                   ['i','top/U','i','flat2','0'],['o','top/U','o','flat2','0']]
    data['ports']=[['top/A','p','','p','output','r','0','0'],
                   ['top/U/B','y','0','y[0]','input','i','0','0'],
                   ['top/U/B','z','','z','output','o','0','0'],
                   ['top/C','q','','q','input','s','0','0'],
                   ['top/$PORT','out','','out','output','s','0','0']]
    data['bridges']=[['top/U','in','input','r','i','0','0'],['top/U','out','output','s','o','0','0']]
    return data


def write_export(folder, data, **changes):
    folder.mkdir(parents=True,exist_ok=True)
    files={}
    for name,columns in TABLES.items():
        file=folder/(name+'.tsv')
        text='\t'.join(columns)+'\n'
        text+=''.join('\t'.join(json.dumps(str(v),ensure_ascii=False) for v in row)+'\n' for row in data[name])
        file.write_text(text,encoding='utf-8');files[file.name]={'rows':len(data[name]),'bytes':file.stat().st_size}
    m=dict(schema_version=1,format='innovus-hier-flow',field_encoding='json-string-tsv-v1',status='complete',
           top='top',root='top',root_path=['top'],depth=1,expand=[{'path':'top/U','depth':1}],stop=[],
           empty_policy='exclude',innovus_version='fixture',encountered_instances=4,files=files)
    m.update(changes);(folder/'manifest.json').write_text(json.dumps(m),encoding='utf-8')
    return folder


def pairs(graph):
    return {(e['source'],e['source_pin'],b['source_bit'],e['sink'],e['sink_pin'],b['sink_bit'])
            for e in graph['edges'] for b in e['bit_connections']}


class InnovusFlowTests(unittest.TestCase):
    def test_directed_bridges_do_not_join_flat_aliases_or_cross_folded_cells(self):
        with tempfile.TemporaryDirectory() as t:
            g=build_graph(write_export(Path(t),fixture()))
        self.assertEqual(pairs(g),{('top/A','p',None,'top/U/B','y',0),
            ('top/U/B','z',None,'top/C','q',None),('top/U/B','z',None,'top/$PORT','out',None)})
        self.assertEqual(g['coverage']['source_sink_bit_pairs'],3)
        self.assertEqual([h['kind'] for h in g['edges'][0]['examples'][0]['hops']],
                         ['frontier_output','expanded_input','frontier_input'])

    def test_root_is_a_real_boundary_without_parent_context(self):
        data=fixture();data['nodes']=[['top/U/$PORT','["top","U"]','wrapper','scope_boundary','boundary'],data['nodes'][2]]
        data['scopes']=data['scopes'][1:];data['hnets']=data['hnets'][2:];data['bridges']=[]
        data['ports']=data['ports'][1:3]+[['top/U/$PORT','in','','in','input','i','0','0'],['top/U/$PORT','out','','out','output','o','0','0']]
        with tempfile.TemporaryDirectory() as t:
            g=build_graph(write_export(Path(t),data,root='top/U',root_path=['top','U'],expand=[]))
        self.assertEqual(len(pairs(g)),2)
        self.assertTrue(all(a.startswith('top/U/') and b.startswith('top/U/') for a,_,_,b,_,_ in pairs(g)))
        # The 64-level expansion limit is relative to the chosen root.
        deep_parts=['top']+['u']*70
        deep_root=path_id(deep_parts)
        for row in data['nodes']:
            parts=deep_parts+json.loads(row[1])[2:]
            row[0]=path_id(parts)+('/$PORT' if row[3]=='scope_boundary' else '')
            row[1]=json.dumps(parts)
        data['scopes'][0][0]=deep_root;data['scopes'][0][1]=json.dumps(deep_parts)
        for row in data['hnets']:row[1]=deep_root
        for row in data['ports']:row[0]=row[0].replace('top/U',deep_root,1)
        with tempfile.TemporaryDirectory() as t:
            g=build_graph(write_export(Path(t),data,root=deep_root,root_path=deep_parts,expand=[]))
        self.assertEqual(len(pairs(g)),2)

    def test_constants_null_and_inout_are_diagnostic_not_shortcuts(self):
        data=fixture();data['ports'] += [['top/C','tie','','tie','input','r','1','0'],
            ['top/C','float','','float','input','0x0','0','0'],['top/C','io','','io','bidi','r','0','0']]
        with tempfile.TemporaryDirectory() as t:g=build_graph(write_export(Path(t),data))
        self.assertEqual(g['coverage']['source_sink_bit_pairs'],3)
        self.assertEqual(g['coverage']['constant_bindings'],1)
        self.assertEqual(g['coverage']['skipped_inout_ports'],1)
        self.assertEqual(g['coverage']['empty_or_omitted_ports'],1)

    def test_paths_and_literal_scalar_brackets_round_trip(self):
        data=fixture();data['ports'][0][1]=data['ports'][0][3]='scalar[0]'
        data['diagnostics']=[['empty_module','top/空~1块','', 'quote" slash\\ tab\t newline\n']]
        with tempfile.TemporaryDirectory() as t:g=build_graph(write_export(Path(t),data,empty_policy='include'))
        self.assertIn(('top/A','scalar[0]',None,'top/U/B','y',0),pairs(g))
        self.assertEqual(path_id(['top','a/b~c']),'top/a~1b~0c')
        self.assertIn('待核实',g['connection_unit'])
        self.assertIn('tab\t',g['diagnostics']['undefined_leaves'][0]['detail'])

    def test_integrity_and_invalid_references_are_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)
            write_export(p,fixture(),status='running')
            with self.assertRaisesRegex(ValueError,'incomplete'):build_graph(p)
            write_export(p,fixture());(p/'ports.tsv').write_text('damaged')
            with self.assertRaisesRegex(ValueError,'size'):build_graph(p)
            data=fixture();data['ports'][0][5]='missing';write_export(p,data)
            with self.assertRaisesRegex(ValueError,'absent'):build_graph(p)
            data=fixture();data['bridges'][0][3]='i';write_export(p,data)
            with self.assertRaisesRegex(ValueError,'parent'):build_graph(p)
            data=fixture();data['ports'].append(copy.copy(data['ports'][0]));write_export(p,data)
            with self.assertRaisesRegex(ValueError,'Duplicate'):build_graph(p)

    def test_limits_and_cli_shared_renderer(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);source=write_export(p/'db',fixture())
            for kwargs in [dict(max_pairs=1),dict(max_rows=1),dict(max_bits=1)]:
                with self.assertRaisesRegex(ValueError,'Exceeded'):build_graph(source,**kwargs)
            out=p/'rendered'
            r=subprocess.run([sys.executable,str(ROOT/'analyze.py'),'innovus-flow',str(source),'--out',str(out)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads((out/'flow_modules.json').read_text())['summary']['all_bit_connections'],3)
            self.assertIn('数据库快照',(out/'flow.html').read_text())
            self.assertIn((out/'flow.svg').read_text().split('<svg',1)[1].split('</svg>',1)[0],(out/'flow.html').read_text())
            r=subprocess.run([sys.executable,str(ROOT/'analyze.py'),'innovus-flow',str(source),'--out',str(out)],capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0)

    def test_tcl_selection_budgets_and_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'empty'
            script='source {'+str(ROOT/'innovus/export_flow.tcl')+'}\n'
            script+=r'''
namespace eval ::ivflow {
 variable state
 set state(depth) 1
 set state(expands) [dict create {top U} 2 {top U B} 1]
 set state(stops) [dict create {top U C} 1]
 if {![want_expand {top U} 1 {child}]} {error {ancestor not expanded}}
 if {[want_expand {top V} 1 {child}]} {error {unselected sibling expanded}}
 if {[want_expand {top U B leaf} 3 {child}]} {error {nested budget exceeded}}
 if {[want_expand {top U C} 2 {child}]} {error {stop ignored}}
 if {[port_parts {scalar[0]} 0x0] ne [list {scalar[0]} {}]} {error {scalar was split}}
 if {[port_parts {vector[0]} 0xBUS] ne [list vector 0]} {error {bus not split}}
}
proc dbGet {args} {
 set key [lindex $args end]
 switch -- $key {
  top.name {return [list top]}
  top.hInst {return [list 0xR]}
  0xR.hInsts - 0xR.insts - 0xR.hNets - 0xR.hNets.hTerms {return [list 0x0]}
  default {error "Unexpected database query: $key"}
 }
}
proc getVersion {} {return fixture}
'''
            script+='if {![catch {::ivflow::export -out {'+str(folder)+'}} msg]} {error {empty root accepted without opt-in}}\n'
            script+='::ivflow::export -out {'+str(folder)+'} -empty_policy include\n'
            r=subprocess.run(['tclsh'],input=script,text=True,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(r.stderr,'')
            self.assertTrue((folder/'manifest.json').exists(),r.stderr)
            g=build_graph(folder)
            self.assertEqual(g['coverage']['visible_nodes'],1)
            self.assertEqual(g['coverage']['source_sink_bit_pairs'],0)
            self.assertIn('待核实',g['connection_unit'])

    def test_tcl_exports_depth_two_and_stop_with_mock_database(self):
        with tempfile.TemporaryDirectory() as t:
            script='source {'+str(ROOT/'innovus/export_flow.tcl')+'}\n'
            script+=r'''
proc getVersion {} {return fixture}
proc dbGet {args} {
 set key [lindex $args end]
 if {$key eq "top.name"} {return [list top]}
 if {$key eq "top.hInst"} {return [list R]}
 set ptr [lindex [split $key .] 0]
 set attr [string range $key [expr {[string length $ptr]+1}] end]
 switch -- $attr {
  name {return [list [dict get {R top U {U[2].x} A A V {U[2].x/V}} $ptr]]}
  cell.name {return [list module]}
  hInsts {return [dict get {R {U A} U V A 0x0 V 0x0} $ptr]}
  insts {return [dict get {R 0x0 U 0x0 A leaf V leaf} $ptr]}
  hNets - hNets.hTerms {return [list 0x0]}
  default {if {[string match {hInstTerms*} $attr]} {return [list 0x0]};error "Unexpected query $key"}
 }
}
'''
            folder=Path(t)
            script+='::ivflow::export -out {'+str(folder/'depth2')+'} -depth 2\n'
            script+='::ivflow::export -out {'+str(folder/'expand')+'} -depth 1 -expand {top/U[2].x} 1\n'
            script+='::ivflow::export -out {'+str(folder/'stop')+'} -depth 2 -stop {top/U[2].x}\n'
            script+='if {![catch {::ivflow::export -out {'+str(folder/'invalid')+'} -stop {top/U[2].x} -expand {top/U[2].x/V} 1} msg]} {error {conflicting selectors accepted}}\n'
            r=subprocess.run(['tclsh'],input=script,text=True,capture_output=True)
            self.assertEqual(r.stderr,'');self.assertEqual(r.returncode,0)
            a=build_graph(folder/'depth2');b=build_graph(folder/'expand');c=build_graph(folder/'stop')
            self.assertEqual({n['id'] for n in a['nodes']},{'top/$PORT','top/U[2].x/V','top/A'})
            self.assertEqual(a['nodes'],b['nodes'])
            self.assertEqual({n['id'] for n in c['nodes']},{'top/$PORT','top/U[2].x','top/A'})
            self.assertFalse((folder/'invalid').exists())

    def test_tcl_path_escaping_and_invalid_depth_before_database_access(self):
        script='source {'+str(ROOT/'innovus/export_flow.tcl')+'}\n'
        script+='puts [::ivflow::id [::ivflow::path {top/a~1b~0c} top]]\n'
        script+='if {[catch {::ivflow::export -out unused -depth 0} msg]} {puts $msg} else {exit 1}\n'
        r=subprocess.run(['tclsh'],input=script,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stderr,'');self.assertIn('top/a~1b~0c',r.stdout);self.assertIn('positive integer',r.stdout)


if __name__=='__main__':unittest.main()
