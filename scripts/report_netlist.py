#!/usr/bin/env python3
"""Build a self-contained offline HTML report and standalone SVG figures.

Python 3.9+ standard library only. Run next to viz_data.py,
render_netlist_figures.py and report_template.html. Inputs are the audited
mempool example analysis results; no EDA tool or network is used.
"""
import argparse
import json
from pathlib import Path


def build_report(results, output):
    """Build all report assets through the reusable Python stage functions."""
    from mempool_profile import validate_profile
    validate_profile(Path(results))
    scripts = Path(__file__).resolve().parent
    source = results.resolve()
    out = output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    from viz_data import build_data
    from render_netlist_figures import build_figures
    from render_matrix_svg import render
    build_data(source, out / 'viz_data.json')
    build_figures(source, out / 'figures')
    render(json.loads((out / 'viz_data.json').read_text(encoding='utf-8')),
           out / 'figures' / 'directed_connections.svg')
    data = json.loads((out / 'viz_data.json').read_text(encoding='utf-8'))
    embedded = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    embedded = (embedded.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
                .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))
    template = (scripts / 'report_template.html').read_text(encoding='utf-8')
    overview = (out / 'figures' / 'mempool_overview.svg').read_text(encoding='utf-8')
    if overview.lstrip().startswith('<?xml'):
        overview = overview.split('?>', 1)[1]
    assert template.count('__NETLIST_DATA__') == 1
    assert template.count('__OVERVIEW_SVG__') == 1
    document = template.replace('__OVERVIEW_SVG__', overview).replace('__NETLIST_DATA__', embedded)
    target = out / 'report.html'
    target.write_text(document, encoding='utf-8')
    print(json.dumps({'report': str(target), 'bytes': target.stat().st_size,
                      'self_contained': True, 'network_required': False,
                      'figure_files': [str(p.relative_to(out)) for p in sorted((out / 'figures').glob('*.svg'))]},
                     ensure_ascii=False))
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    return build_report(args.results, args.out)


if __name__ == '__main__':
    main()
