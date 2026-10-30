#!/usr/bin/env python3
"""Render a directed pin-bit-count matrix using Python's standard library."""
import argparse
import html
import json
import math
from pathlib import Path


def render(data, output):
    matrix = data['matrix']['name_filtered']
    nodes = data['matrix']['nodes']
    n = len(nodes)
    assert len(matrix) == n and all(len(row) == n for row in matrix)
    assert sum(map(sum, matrix)) == data['matrix']['name_filtered_total']
    cell, left, top = 19, 138, 190
    width, height = left + n * cell + 112, top + n * cell + 190
    maximum = max(max(row) for row in matrix)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" viewBox="0 0 {} {}" role="img">'.format(width, height, width, height),
             '<title>Directed module boundary connections</title>',
             '<desc>Rows are source instances; columns are destinations. Values count confirmed pin-bit connections and can count the same net more than once. Zero means no direct boundary connection verified, not no path. Names containing clock/reset/test-related patterns are excluded. Logarithmic color scale. No undefined leaf traversal.</desc>',
             '<rect width="100%" height="100%" fill="#f5f8fc"/>',
             '<style>text{font-family:Arial,DejaVu Sans,sans-serif;font-variant-numeric:tabular-nums}</style>']
    def text(x,y,s,size=14,color='#263b53',anchor='start',extra=''):
        parts.append('<text x="{}" y="{}" font-size="{}" fill="{}" text-anchor="{}" {}>{}</text>'.format(x,y,size,color,anchor,extra,html.escape(str(s))))
    def color(value):
        if not value: return '#e8edf3'
        t = .18 + .82 * math.log1p(value) / math.log1p(maximum)
        a,b=(223,238,246),(25,91,139)
        return '#'+''.join('{:02x}'.format(round(x+(y-x)*t)) for x,y in zip(a,b))
    text(36,43,'mempool_group / Directed boundary connections',25,extra='font-weight="600"')
    text(36,73,'Confirmed pin-bit counts | 40 submodules + I/O | logarithmic color scale',15)
    text(36,98,'Clock/reset/test-related port names excluded by a naming rule; no leaf-cell traversal.',13,'#52647a')
    text(left,131,'Destination columns',14)
    for i,node in enumerate(nodes):
        cx = left + (i+.5)*cell
        cy = top + (i+.5)*cell
        text(left-10,cy+4,node['short'],12,anchor='end')
        parts.append('<text transform="translate({} {}) rotate(-55)" font-size="12" fill="#263b53">{}</text>'.format(cx-2,top-10,html.escape(node['short'])))
    for r,row in enumerate(matrix):
        for c,value in enumerate(row):
            tip='{} -> {}: {} confirmed pin-bit connections'.format(nodes[r]['id'],nodes[c]['id'],value)
            parts.append('<rect x="{}" y="{}" width="{}" height="{}" rx="1" fill="{}"><title>{}</title></rect>'.format(left+c*cell+.5,top+r*cell+.5,cell-1,cell-1,color(value),html.escape(tip)))
    for boundary in [16,20,21,37,40]:
        if boundary >= n: continue
        pos=boundary*cell
        parts.append('<path d="M{} {}V{} M{} {}H{}" stroke="#9caebf" stroke-width="1" fill="none"/>'.format(left+pos,top,top+n*cell,left,top+pos,left+n*cell))
    parts.append('<text transform="translate(30 {}) rotate(-90)" font-size="14" fill="#263b53" text-anchor="middle">Source rows</text>'.format(top+n*cell/2))
    y=top+n*cell+32
    text(36,y,'Color key',13)
    values=sorted(set([0,1,8,64,512,maximum]))
    for k,value in enumerate(values):
        x=140+k*116
        parts.append('<rect x="{}" y="{}" width="28" height="16" rx="2" fill="{}"/>'.format(x,y-13,color(value)))
        text(x+35,y,value,12)
    text(36,y+35,'T0-T15: tiles   Local / R1-R3: interconnects   Dn.*: DMA components   I/O: top ports',13)
    text(36,y+65,'Zero = no direct connection verified. It does not prove there is no path.',14)
    text(36,y+90,'Counts can repeat a net across endpoints. They do not measure traffic, bandwidth or timing.',13,'#52647a')
    text(36,y+115,'Source: supplied netlist; scripts and per-port evidence are included in the report package.',13,'#52647a')
    parts.append('</svg>')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('\n'.join(parts),encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    render(json.loads(args.data.read_text()),args.out)
    print(args.out)


if __name__=='__main__':
    main()
