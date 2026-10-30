#!/usr/bin/env python3
"""Render audited mempool structural results as standalone SVG; stdlib only.

No EDA, network, fonts or JavaScript dependencies. Counts are leaf instances;
bit mappings are selected directly verified hierarchy connections, not traffic.
"""
import argparse
import csv
import html
import json
import re
from pathlib import Path

INK = '#182b44'
MUTED = '#52647a'
BG = '#f5f8fc'
WHITE = '#ffffff'
BLUE = '#2563a5'
TEAL = '#187d7a'
PURPLE = '#7859a5'
ORANGE = '#b5681d'
LINE = '#d6e0eb'
FIELDS = [
    ('meta_id', 3, '[123:121]', PURPLE),
    ('core_id', 2, '[120:119]', '#956fbc'),
    ('amo', 4, '[118:115]', '#b095cd'),
    ('data', 32, '[114:83]', BLUE),
    ('wen', 1, '[82]', ORANGE),
    ('be', 4, '[81:78]', '#4d95bb'),
    ('addr', 16, '[77:62]', TEAL),
]


class Svg:
    def __init__(self, width, height, title, description):
        self.w, self.h = width, height
        self.parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" viewBox="0 0 {} {}" role="img" aria-labelledby="title desc">'.format(width, height, width, height),
            '<title id="title">{}</title><desc id="desc">{}</desc>'.format(html.escape(title), html.escape(description)),
            '<style>text{font-family:"Noto Sans CJK SC","Microsoft YaHei","PingFang SC",Arial,sans-serif;font-variant-numeric:tabular-nums}</style>',
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="'+TEAL+'"/></marker><pattern id="unknown" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#fff4df"/><path d="M-2 2L2 -2M0 8L8 0M6 10L10 6" stroke="#b5681d" stroke-width="1.5"/></pattern></defs>',
        ]
        self.rect(0, 0, width, height, BG)

    def rect(self, x, y, w, h, fill, rx=0, stroke=None):
        self.parts.append('<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="{}"{}/>'.format(x,y,w,h,rx,fill,' stroke="{}"'.format(stroke) if stroke else ''))

    def text(self, x, y, s, size=18, fill=INK, weight=400, anchor='start'):
        self.parts.append('<text x="{}" y="{}" font-size="{}" fill="{}" font-weight="{}" text-anchor="{}">{}</text>'.format(x,y,size,fill,weight,anchor,html.escape(str(s))))

    def path(self, d, stroke=LINE, width=1.5, arrows=False, dash=False):
        self.parts.append('<path d="{}" fill="none" stroke="{}" stroke-width="{}"{}{} />'.format(d,stroke,width,' marker-start="url(#arrow)" marker-end="url(#arrow)"' if arrows else '', ' stroke-dasharray="5 4"' if dash else ''))

    def line(self, x1,y1,x2,y2,stroke=LINE,width=1.5):
        self.path('M{} {}L{} {}'.format(x1,y1,x2,y2),stroke,width)

    def write(self, path):
        path.write_text('\n'.join(self.parts + ['</svg>']), encoding='utf-8')

    def heading(self, title, sub):
        self.text(42, 53, title, 30, weight=600)
        self.text(43, 86, sub, 17, MUTED)


def source_groups(results):
    groups = json.loads((results / 'dma_tile_groups.json').read_text())
    assert len(groups) == 16
    for g in groups:
        assert g['known_direct_handshake_bits'] == 4
        assert g['tile'] // 4 == g['dma']
    return {i: sorted(g['tile'] for g in groups if g['dma'] == i) for i in range(4)}


def verify_bus_mapping(results):
    """Require the source binding evidence used by the fixed mempool example."""
    graph=json.loads((results/'connectivity.json').read_text())
    bound=json.loads((results/'top_instances_connections.json').read_text())
    meta={i['name']:i['ports'] for i in graph['instances']}
    binds={i['name']:i['pins'] for i in bound['instances']}
    def bits(inst,pin):
        p=meta[inst][pin]; raw=binds[inst][pin]
        labels=[None] if p['msb'] is None else list(range(p['msb'],p['lsb']-1,-1))
        assert len(labels)==len(raw)==p['width']
        return dict(zip(labels,map(tuple,raw)))
    src='gen_tiles[0].i_tile'; data=bits(src,'tcdm_master_req_o')
    assert len(data)==248
    for group in (1,2,3):
        dst='gen_remote_interco[{}].i_remote_interco'.format(group)
        offset=62*group
        mappings=[(list(range(offset+61,offset+20,-1)),'req_wdata_i',list(range(40,-1,-1))),
                  ([offset+20],'req_wen_i',[0]),
                  (list(range(offset+19,offset+15,-1)),'req_be_i',list(range(3,-1,-1))),
                  (list(range(offset+15,offset-1,-1)),'req_tgt_addr_i',list(range(15,-1,-1)))]
        for sb,pin,db in mappings:
            dest=bits(dst,pin)
            assert all(data[a]==dest[b] for a,b in zip(sb,db)),(group,pin)
    field_expectations=[('meta_id',range(121,124)),('core_id',range(119,121)),
                        ('amo',range(115,119)),('data',range(83,115))]
    for field,span in field_expectations:
        assert all(data[i][0].endswith('_'+field) for i in span),field
    local_count=sum(e['direct_bit_connections'] for e in graph['direct_connections']
                    if e['source']==src and e['source_pin']=='tcdm_master_req_o'
                    and e['sink']=='i_local_interco')
    assert local_count==58


def dma_figure(groups, out):
    s = Svg(1280, 710, 'DMA与16个tile的已确认连接分组', '四组DMA reqrsp_demux各连接四个tile；每对有四条直接握手位。逻辑拓扑示意，不是物理floorplan，仅显示本组DMA连接。')
    s.heading('DMA ↔ Tile：四组已确认的连接关系', '每条双向边代表4个已核实的握手位：request valid/ready + response valid/ready')
    for g in range(4):
        x = 36 + g * 310
        s.rect(x, 122, 282, 482, WHITE, 16, LINE)
        s.text(x + 22, 156, 'GROUP {}'.format(g), 15, MUTED, 600)
        s.rect(x + 32, 176, 218, 70, '#e6f2f1', 10)
        s.text(x + 141, 204, 'DMA {} 分发逻辑'.format(g), 19, TEAL, 600, 'middle')
        s.text(x + 141, 228, 'reqrsp_demux', 15, TEAL, anchor='middle')
        for j, tile in enumerate(groups[g]):
            y = 282 + j * 72
            s.path('M{} 251 C{} {} {} {} {} {}'.format(x+125, x+20,260,x+20,y+25,x+72,y+25), TEAL, 1.7, arrows=True)
            s.rect(x + 78, y, 172, 50, '#eaf1fa', 8)
            s.text(x + 164, y + 31, 'Tile {:02d}'.format(tile), 20, BLUE, 600, 'middle')
    s.text(43, 644, '所有16个tile的分组均由实际端口绑定核对；没有按编号猜测。', 18, INK)
    s.text(43, 675, '逻辑拓扑示意，不代表物理floorplan；local、remote、AXI等其它连接未画入本图。', 17, MUTED)
    s.write(out / 'dma_tile_groups.svg')


def bus_figure(out):
    s = Svg(1280, 960, 'tile0请求总线的248位分组与remote1的62位映射', '248位请求总线分为local和三个remote组。remote1的62位全部直接核验。local的四个be位需要未知leaf模型。41位wdata打包字段由32data、4amo、2core_id和3meta_id组成。')
    s.heading('Tile 0 请求总线：从248位定位到每个字段', 'tcdm_master_req_o[247:0] · 拼接与两端绑定逐位核对 · 位宽不代表吞吐带宽')
    s.text(52, 135, '1  四组请求接口，按原始高位 → 低位排列', 21, weight=600)
    parts = [('Remote 3','[247:186]','62/62位直接核实'),('Remote 2','[185:124]','62/62位直接核实'),('Remote 1','[123:62]','62/62位直接核实'),('Local','[61:0]','58/62位直接核实')]
    for i,(name,bits,coverage) in enumerate(parts):
        x = 52 + i * 294
        s.rect(x, 158, 282, 94, '#eaf1fa' if i<3 else '#fff4df', 8)
        s.text(x+141,188,name,21,BLUE if i<3 else ORANGE,600,'middle')
        s.text(x+141,216,bits+' · 62 bit',17,INK,anchor='middle')
        s.text(x+141,278,coverage,16,MUTED,anchor='middle')
        if i==3:
            s.rect(x + 282*42/62, 239, 282*4/62, 13, 'url(#unknown)')
    s.text(52, 311, 'Local 的4个be位跨未定义leaf；以斜线标示待模型部分，不把它补成直接连接。',16,ORANGE)
    s.line(52, 337, 1228, 337)
    s.text(52, 379, '2  放大 Remote 1：62位全部直接连接', 21, weight=600)
    x, y, width = 52, 407, 1176
    outer = [('req_wdata_i[40:0]',41,BLUE),('wen',1,ORANGE),('be',4,'#4d95bb'),('req_tgt_addr_i[15:0]',16,TEAL)]
    cursor = x
    for name,n,c in outer:
        w=width*n/62
        s.rect(cursor,y,w,70,c)
        if n>=16:
            s.text(cursor+w/2,y+29,name,17,WHITE,600,'middle')
            s.text(cursor+w/2,y+53,str(n)+' bit',16,WHITE,anchor='middle')
        elif n==4:
            s.text(cursor+w/2,y+30,'be',16,WHITE,600,'middle')
            s.text(cursor+w/2,y+53,'4 bit',14,WHITE,anchor='middle')
        else:
            s.line(cursor+w/2,y+74,cursor+w/2,y+102,ORANGE)
            s.text(cursor+w/2,y+127,'wen · 1 bit',15,ORANGE,anchor='middle')
        cursor += w
    s.text(52, 512, '源 [123:83] → 目的 [40:0]',16,MUTED)
    s.text(1228,512,'源 [77:62] → 目的 [15:0]',16,MUTED,anchor='end')
    s.text(52, 586, '3  再拆开41位打包字段：32位data + 9位元信息', 21, weight=600)
    cursor = 52
    for name,n,bits,c in FIELDS[:4]:
        w=1176*n/41
        s.rect(cursor,616,w,70,c)
        s.text(cursor+w/2,645,name,16,WHITE,600,'middle')
        s.text(cursor+w/2,671,str(n)+' bit',15,WHITE,anchor='middle')
        cursor+=w
    for i,(name,n,bits,c) in enumerate(FIELDS):
        col,row=i%2,i//2
        x=58+col*588;y=737+row*43
        s.rect(x,y-15,13,13,c,3)
        s.text(x+26,y,name,17,INK,600)
        s.text(x+145,y,str(n)+' bit',17,INK)
        s.text(x+245,y,'源 '+bits,16,MUTED)
    s.text(52, 938, '字段名称来自网名；连接方向来自已定义模块端口。此图不证明完整协议功能或实际时序。',16,MUTED)
    s.write(out / 'tile0_request_mapping.svg')


def short_name(name):
    n=name.lstrip('\\')
    m=re.match(r'gen_tiles\[(\d+)\]',n)
    if m:return 'Tile {:02d}'.format(int(m.group(1)))
    m=re.match(r'gen_remote_interco\[(\d+)\]',n)
    if m:return 'Remote {} / interco'.format(m.group(1))
    m=re.match(r'gen_dmas\[(\d+)\]\.(.*)',n)
    if m:
        tail=m.group(2)
        label = 'reqrsp_demux' if 'demux' in tail else tail.replace('i_axi_dma_backend','AXI backend').replace('i_dma_axi_xbar','AXI xbar').replace('i_axi_to_reqrsp','AXI to reqrsp')
        return 'DMA {} / {}'.format(m.group(1),label)
    if n.startswith('gen_axi_group_cuts'):return 'AXI group cut 0'
    return n


def category_color(n):
    if 'gen_tiles' in n:return BLUE
    if 'gen_dmas' in n:return ORANGE
    if 'remote_interco' in n or n=='i_local_interco':return TEAL
    if n=='i_axi_interco':return PURPLE
    return '#677b93'


def size_figure(results,out):
    rows=list(csv.DictReader((results/'top_inventory.csv').open()))
    assert len(rows)==40
    rows.sort(key=lambda r:int(r['expanded_leaf_instances']),reverse=True)
    total=sum(int(r['expanded_leaf_instances']) for r in rows)
    assert total==3067784
    s=Svg(1280,1650,'40个顶层子树的叶实例数量','按实际展开的叶实例数降序，包含全部40个已定义顶层子实例。数量不是面积。顶层直接10205个叶实例未分配进这些子树。')
    s.heading('顶层模块规模：40个子树，按叶实例数排序', '数量是结构规模代理，不是面积；顶层直接的10,205个叶实例不在这些子树中')
    x0,y0,span,maxv=370,164,766,250000
    for t in range(0,250001,50000):
        xx=x0+span*t/maxv
        s.line(xx,143,xx,y0+40*33,LINE,1)
        s.text(xx,130,'{:,.0f}'.format(t),14,MUTED,anchor='middle')
    for i,r in enumerate(rows):
        name=r['instance'];v=int(r['expanded_leaf_instances']);y=y0+i*33
        s.parts.append('<g><title>{}: {:,} leaf instances</title>'.format(html.escape(name),v))
        s.text(346,y+17,short_name(name),16,INK,anchor='end')
        s.rect(x0,y,span*v/maxv,24,category_color(name),3)
        s.text(x0+span*v/maxv+9,y+17,'{:,}'.format(v),15,INK)
        s.parts.append('</g>')
    s.text(x0,y0+40*33+40,'叶实例数量 / instances',17,MUTED)
    legends=[('Tile',BLUE),('Local / Remote',TEAL),('DMA',ORANGE),('Shared AXI',PURPLE),('其它', '#677b93')]
    for i,(n,c) in enumerate(legends):
        xx=52+i*225;s.rect(xx,1558,14,14,c,3);s.text(xx+24,1571,n,17,MUTED)
    s.text(52,1617,'子树合计 3,067,784 + 顶层直接 10,205 = 全设计 3,077,989 个叶实例。',18,INK)
    s.write(out/'top_module_sizes.svg')


def overview(groups,out):
    s=Svg(1280,790,'mempool_group结构连接速览','左侧为四组DMA与16个tile的直接握手分组；右侧展示remote1请求包62位中的32位data及其它字段。所有位置只是逻辑示意。')
    s.heading('mempool_group · 结构连接速览', '从大网表中提取两件可直接核对的事实：谁与谁相连，打包总线的每一段是什么')
    s.rect(32,116,674,558,WHITE,14,LINE)
    s.rect(728,116,520,558,WHITE,14,LINE)
    s.text(54,152,'16个tile → 4个DMA邻接组',22,weight=600)
    s.text(54,181,'双向边：每对4个直接握手位',16,MUTED)
    for g in range(4):
        xx=49+(g%2)*327;yy=207+(g//2)*218
        s.rect(xx,yy,307,196,'#f7fafd',10)
        s.rect(xx+12,yy+77,97,47,'#e6f2f1',7)
        s.text(xx+60,yy+98,'DMA {}'.format(g),18,TEAL,600,'middle')
        s.text(xx+60,yy+116,'demux',13,TEAL,anchor='middle')
        for j,t in enumerate(groups[g]):
            ty=yy+15+j*43
            s.path('M{} {} C{} {} {} {} {} {}'.format(xx+115,yy+100,xx+143,yy+100,xx+145,ty+16,xx+171,ty+16),TEAL,1.5,arrows=True)
            s.rect(xx+179,ty,112,33,'#eaf1fa',6)
            s.text(xx+235,ty+22,'Tile {:02d}'.format(t),17,BLUE,600,'middle')
    s.text(750,152,'Remote 1 请求包 · 62 bit',22,weight=600)
    s.text(750,181,'Tile 0 的 [123:62]：62/62位直接核实',16,MUTED)
    cursor=750;barwidth=476
    for name,n,bits,c in FIELDS:
        w=barwidth*n/62;s.rect(cursor,208,w,49,c);cursor+=w
    # A compact label above the 41-bit sub-bundle, independent of narrow segments.
    s.line(750,269,750+476*41/62,269,BLUE,2)
    s.text(750,297,'41位wdata打包字段 = 32 data + 9 元信息',16,INK)
    s.text(750,335,'字段',16,MUTED)
    s.text(963,335,'位数',16,MUTED,anchor='end')
    s.text(1215,335,'源位段',16,MUTED,anchor='end')
    s.line(750,345,1226,345)
    for i,(name,n,bits,c) in enumerate(FIELDS):
        y=375+i*37
        s.rect(750,y-13,13,13,c,3)
        s.text(775,y,name,18,INK,600 if name=='data' else 400)
        s.text(963,y,str(n),18,INK,anchor='end')
        s.text(1215,y,bits,17,MUTED,anchor='end')
    s.text(750,651,'位宽不等于流量或时序关键性。',16,MUTED)
    s.text(43,714,'布局启发：先比较DMA与其四个tile的邻接关系，再结合共享互连调整。',20,INK)
    s.text(43,751,'逻辑拓扑示意，非物理floorplan。其它连接未画入；全部数据来自上传网表的离线脚本结果。',16,MUTED)
    s.write(out/'mempool_overview.svg')


def english_overview(out):
    """Companion preview for renderers that lack a local CJK font."""
    text=(out/'mempool_overview.svg').read_text(encoding='utf-8')
    replacements={
        'mempool_group结构连接速览':'mempool_group connectivity overview',
        '左侧为四组DMA与16个tile的直接握手分组；右侧展示remote1请求包62位中的32位data及其它字段。所有位置只是逻辑示意。':'Four DMA groups connect to sixteen tiles through directly verified handshakes. The remote 1 request contains 32 data bits and other fields in a 62-bit bundle. Positions are logical only.',
        'mempool_group · 结构连接速览':'mempool_group · Connectivity overview',
        '从大网表中提取两件可直接核对的事实：谁与谁相连，打包总线的每一段是什么':'Two directly verified views: DMA tile groups and the fields of a packed request.',
        '16个tile → 4个DMA邻接组':'16 tiles → 4 DMA groups',
        '双向边：每对4个直接握手位':'Each edge: 4 direct handshake bits',
        'Remote 1 请求包 · 62 bit':'Remote 1 request · 62 bits',
        'Tile 0 的 [123:62]：62/62位直接核实':'Tile 0 [123:62] · 62/62 directly verified',
        '41位wdata打包字段 = 32 data + 9 元信息':'41-bit wdata bundle = 32 data + 9 metadata',
        '字段':'Field', '位数':'Bits', '源位段':'Source range',
        '位宽不等于流量或时序关键性。':'Width does not measure traffic or criticality.',
        '布局启发：先比较DMA与其四个tile的邻接关系，再结合共享互连调整。':'Placement hint: compare DMA + four-tile neighborhoods, then consider shared interconnects.',
        '逻辑拓扑示意，非物理floorplan。其它连接未画入；全部数据来自上传网表的离线脚本结果。':'Logical grouping, not a physical floorplan. Other links omitted. Source: the uploaded netlist.',
    }
    for a,b in replacements.items():
        assert a in text,a
        text=text.replace(a,b)
    assert not re.search(r'[\u4e00-\u9fff]',text)
    (out/'mempool_overview_en.svg').write_text(text,encoding='utf-8')


def build_figures(results, out):
    """Render the fixed example only after checking its source provenance."""
    results, out = Path(results), Path(out)
    from mempool_profile import validate_profile
    validate_profile(results)
    out.mkdir(parents=True, exist_ok=True)
    groups=source_groups(results)
    verify_bus_mapping(results)
    overview(groups,out)
    english_overview(out)
    dma_figure(groups,out)
    bus_figure(out)
    size_figure(results,out)
    print(json.dumps({'output':str(out),'svg_files':[p.name for p in sorted(out.glob('*.svg'))]},ensure_ascii=False))


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args(argv)
    return build_figures(args.results, args.out)


if __name__=='__main__':main()
