// In-memory DOM integration test. This does not claim browser layout verification.
const fs=require('fs'),vm=require('vm'),path=require('path'),{webcrypto}=require('crypto');
class Matrix{constructor(a=1,b=0,c=0,d=1,e=0,f=0){Object.assign(this,{a,b,c,d,e,f})}inverse(){const z=this.a*this.d-this.b*this.c;return new Matrix(this.d/z,-this.b/z,-this.c/z,this.a/z,(this.c*this.f-this.d*this.e)/z,(this.b*this.e-this.a*this.f)/z)}multiply(m){return new Matrix(this.a*m.a+this.c*m.b,this.b*m.a+this.d*m.b,this.a*m.c+this.c*m.d,this.b*m.c+this.d*m.d,this.a*m.e+this.c*m.f+this.e,this.b*m.e+this.d*m.f+this.f)}}
class Point{constructor(x,y){this.x=x;this.y=y}matrixTransform(m){return {x:m.a*this.x+m.c*this.y+m.e,y:m.b*this.x+m.d*this.y+m.f}}}
class Element{constructor(tag){this.tag=tag;this.children=[];this.events={};this.attributes={};this.open=false;this.value='';this.textContent='';this.classes=new Set();this.classList={toggle:(n,on)=>on?this.classes.add(n):this.classes.delete(n)}}append(...items){this.children.push(...items)}setAttribute(k,v){this.attributes[k]=v}getAttribute(k){return this.attributes[k]}addEventListener(k,f){this.events[k]=f}getBBox(){return {x:100,y:-500,width:150,height:50}}getScreenCTM(){return this.tag.startsWith('module_')?new Matrix(1,0,0,1,30,1000):new Matrix()}getBoundingClientRect(){return {width:1000,height:700}}setPointerCapture(){}}
const file=process.argv[2],html=fs.readFileSync(file,'utf8'),script=html.match(/<script>([\s\S]*)<\/script>/)[1];
function setup(mode='success',missingSvg=false){
 const elements=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element(m[1])]));
 const diagram=new Element('svg');diagram.attributes.viewBox=html.match(/viewBox="([^"]+)"/)[1];
 elements.get('graph-document').querySelector=()=>missingSvg?null:diagram;
 let requested=0,context;
 const head={append:node=>{requested++;Promise.resolve().then(()=>{
  if(mode==='blocked'){node.onerror();return}
  vm.runInContext(fs.readFileSync(path.join(path.dirname(file),node.src.split('?')[0]),'utf8'),context,{timeout:10000});node.onload();
 })}};
 context=vm.createContext({document:{head,getElementById:id=>elements.get(id)||null,createElement:tag=>new Element(tag)},DOMPoint:Point,setTimeout,clearTimeout,crypto:webcrypto,TextDecoder});context.window=context;
 vm.runInContext(script,context,{timeout:10000});
 return {elements,diagram,context,get requested(){return requested}};
}
(async()=>{
 const a=setup();if(a.elements.get('module-edges').children.length)throw Error('Eager module table');a.elements.get('module-edge-list').open=true;a.elements.get('module-edge-list').events.toggle();if(!a.elements.get('module-edges').children.length)throw Error('Missing lazy module table');if(a.requested)throw Error('Eager detail load during startup');
 vm.runInContext("selectNode(MODULES.nodes[0].id);showConnection(MODULES.edges[0]);",a.context,{timeout:10000});
 if(a.requested)throw Error('Details fetched while panel closed');
 if(!a.elements.get('summary').textContent.includes('条模块连线'))throw Error('Missing module summary');
 await vm.runInContext('requestDetails()',a.context,{timeout:10000});
 if(a.requested!==1||!a.elements.get('detail').textContent.includes('路径证据'))throw Error('Missing lazy detail content');
 await vm.runInContext('requestDetails()',a.context,{timeout:10000});
 if(a.requested!==1)throw Error('Repeated detail fetch');
 a.elements.get('zoom-in').events.click();a.elements.get('fit').events.click();
 a.diagram.getScreenCTM=()=>null;vm.runInContext('selectNode(MODULES.nodes[0].id)',a.context);
 const b=setup('blocked');vm.runInContext('showConnection(MODULES.edges[0])',b.context);
 await vm.runInContext('requestDetails()',b.context);
 if(!b.elements.get('detail').textContent.includes('flow.json'))throw Error('No recovery for blocked companion file');
 if(!b.elements.get('summary').textContent.includes('条模块连线'))throw Error('Graph was lost after blocked details');
 const bytes=fs.readFileSync(path.join(path.dirname(file),'flow.json'));
 await b.elements.get('detail-file').events.change({target:{files:[{arrayBuffer:async()=>Uint8Array.from(bytes).buffer}]}});
 if(!b.elements.get('detail').textContent.includes('路径证据'))throw Error('Manual verified JSON fallback failed');
 await b.elements.get('detail-file').events.change({target:{files:[{arrayBuffer:async()=>new TextEncoder().encode('{}').buffer}]}});
 if(!b.elements.get('detail').textContent.includes('不一致'))throw Error('Mismatched JSON was accepted');
 const c=setup('blocked',true);vm.runInContext('selectNode(MODULES.nodes[0].id)',c.context);
 if(!c.elements.get('view-status').textContent)throw Error('No SVG fallback status');
 console.log(JSON.stringify({startup:'passed',lazy_details:'passed',blocked_details_recovery:'passed',manual_json_sha256:'passed',null_svg_matrix:'passed',browser_layout:'not_checked'}));
})().catch(e=>{console.error(e);process.exitCode=1});
