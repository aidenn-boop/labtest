#!/usr/bin/env python3
"""Build a self-contained HTML replay viewer from a trace (sim trace or the official mechreplay/1 sample).

  python3 sim/render.py out/trace_seed3.jsonl.gz -o out/seed3.html      # then open the HTML in a browser
  python3 sim/render.py <official mechanics-v2-16.jsonl.gz> -o out/official.html

Standard library only. Shows buildings (shaded by roof height), zone + next zone, mechs (aim, HP,
weapons, label), NPCs, bullets/shells, lasers, lightning arcs, explosions, hits, deaths, crates/cubes/
weapon pickups, supply drops, and the tracked bot's debug drawings. Click a mech in the list to follow it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_trace(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as f:
        return [json.loads(l) for l in f]


def map_data(arena=None):
    from paths import find_arena
    d = find_arena(arena)
    w = json.load(open(os.path.join(d, "v2", "world.json")))
    prisms = []
    for p in w["prisms"]:
        ring = [round(v, 1) for v in p["ring"]]
        prisms.append([p["kind"][0], round(p["base"], 1), round(p["top"], 1), ring])
    trees = []
    t = w["trees"]
    F = t["fields"]
    n = len(F)
    ix, iy, ir = F.index("crownX"), F.index("crownY"), F.index("crownR")
    for i in range(0, len(t["data"]), n):
        trees.append([round(t["data"][i + ix], 1), round(t["data"][i + iy], 1), round(t["data"][i + ir], 1)])
    return {"bounds": w["bounds"], "prisms": prisms, "trees": trees}


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Mech replay</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#101418;--panel:#1a2027;--fg:#dde3ea;--mut:#8a96a3;--acc:#ffb340}
body{margin:0;background:var(--bg);color:var(--fg);font:12px/1.35 system-ui,sans-serif;display:flex;height:100vh;overflow:hidden}
#main{flex:1;position:relative} canvas{display:block;width:100%;height:100%}
#side{width:330px;background:var(--panel);overflow:auto;padding:8px;box-sizing:border-box}
#bar{position:absolute;left:0;right:0;bottom:0;padding:6px 10px;background:#0009;display:flex;gap:8px;align-items:center}
#bar input[type=range]{flex:1} button,select{background:#2a333d;color:var(--fg);border:1px solid #3a4652;border-radius:4px;padding:2px 8px}
.m{padding:3px 4px;border-radius:4px;cursor:pointer;display:flex;gap:6px;align-items:center}.m:hover{background:#2a333d}.m.sel{outline:1px solid var(--acc)}
.dead{opacity:.45}.sw{width:10px;height:10px;border-radius:50%;flex:none}.hp{height:4px;background:#333;border-radius:2px;margin-top:2px}.hp>i{display:block;height:4px;border-radius:2px;background:#4caf50}
#log{margin-top:8px;font-size:11px;color:var(--mut);white-space:pre-wrap;max-height:40vh;overflow:auto}
#info{position:absolute;left:8px;top:8px;background:#0009;padding:6px 8px;border-radius:4px}
@media (max-width:700px){body{flex-direction:column}#side{width:100%;height:35vh}}
</style></head><body>
<div id="main"><canvas id="c"></canvas><div id="info"></div>
<div id="bar"><button id="play">▶</button><select id="spd"><option>0.5</option><option selected>1</option><option>2</option><option>4</option><option>8</option><option>16</option></select>
<input id="slider" type="range" min="0" value="0"><span id="time"></span><label><input type="checkbox" id="follow"> follow</label><label><input type="checkbox" id="trees"> trees</label></div></div>
<div id="side"><div id="hdr"></div><div id="list"></div><div id="log"></div></div>
<script>
const MAP=__MAP__; const TR=__TRACE__;
const H=TR[0], RES=TR[TR.length-1].type==='result'?TR[TR.length-1]:null;
const FR=TR.filter(r=>r.m);
const WK=['gun','mortar','laser','lightning'];
const names={}; (H.mechs||[]).forEach(m=>names[m.id]=m.name);
const tracked=new Set(H.source==='sim'?(H.mechs||[]).filter(m=>!String(m.name).startsWith('house')&&!String(m.name).startsWith('builtin')).map(m=>m.id):[]);
const col=i=>`hsl(${(i*137.5)%360},75%,${tracked.has(i)?65:55}%)`;
const cv=document.getElementById('c'),cx=cv.getContext('2d');
let view={x:0,y:0,s:1},sel=null,fi=0,playing=false,spd=1,last=0,acc=0;
// precompute: items snapshot index, events window
let items=null; const itemsAt=[]; FR.forEach((f,i)=>{if(f.items)items=f.items;itemsAt[i]=items;});
const evAt=FR.map(f=>f.ev||[]);
const zoneP=H.zone||[];
function resize(){const r=cv.getBoundingClientRect();cv.width=r.width*devicePixelRatio;cv.height=r.height*devicePixelRatio;fit();draw();}
function fit(){const b=MAP.bounds;const w=cv.width,h=cv.height;view.s=Math.min(w/(b.maxX-b.minX),h/(b.maxY-b.minY))*0.98;view.x=(b.minX+b.maxX)/2;view.y=(b.minY+b.maxY)/2;}
const X=x=>(x-view.x)*view.s+cv.width/2, Y=y=>-(y-view.y)*view.s+cv.height/2;
cv.addEventListener('wheel',e=>{e.preventDefault();const k=e.deltaY<0?1.2:1/1.2;const r=cv.getBoundingClientRect();const px=(e.clientX-r.left)*devicePixelRatio,py=(e.clientY-r.top)*devicePixelRatio;const wx=(px-cv.width/2)/view.s+view.x,wy=-(py-cv.height/2)/view.s+view.y;view.s*=k;view.x=wx-(px-cv.width/2)/view.s;view.y=wy+(py-cv.height/2)/view.s;draw();},{passive:false});
let drag=null;cv.onmousedown=e=>drag=[e.clientX,e.clientY];window.onmouseup=()=>drag=null;window.onmousemove=e=>{if(!drag)return;view.x-=(e.clientX-drag[0])*devicePixelRatio/view.s;view.y+=(e.clientY-drag[1])*devicePixelRatio/view.s;drag=[e.clientX,e.clientY];document.getElementById('follow').checked=false;draw();};
function poly(r){cx.beginPath();for(let i=0;i<r.length;i+=2){const x=X(r[i]),y=Y(r[i+1]);i?cx.lineTo(x,y):cx.moveTo(x,y);}cx.closePath();}
function draw(){
 const f=FR[fi]; if(!f)return; const w=cv.width,h=cv.height;
 if(document.getElementById('follow').checked&&sel!=null){const m=f.m.find(m=>m[0]===sel);if(m){view.x=m[1];view.y=m[2];}}
 cx.fillStyle='#1c2329';cx.fillRect(0,0,w,h);
 const b=MAP.bounds;cx.strokeStyle='#444';cx.strokeRect(X(b.minX),Y(b.maxY),(b.maxX-b.minX)*view.s,(b.maxY-b.minY)*view.s);
 if(document.getElementById('trees').checked){cx.fillStyle='#24452a';for(const t of MAP.trees){cx.beginPath();cx.arc(X(t[0]),Y(t[1]),Math.max(1,t[2]*view.s),0,7);cx.fill();}}
 for(const p of MAP.prisms){const ht=p[2]-p[1];const l=Math.max(22,Math.min(70,25+ht));cx.fillStyle=p[0]==='b'&&ht<4?'#5a5040':p[0]==='b'?`hsl(210,10%,${l}%)`:p[0]==='s'?'#6b5a4a':'#7a6a3a';poly(p[3]);cx.fill();}
 // zone
 const z=f.z; cx.lineWidth=2*devicePixelRatio; cx.strokeStyle='#4aa3ff'; cx.beginPath();cx.arc(X(z[0]),Y(z[1]),z[2]*view.s,0,7);cx.stroke();
 const tt=f.t/(H.tick_hz||20); const nxt=zoneP.find(p=>p.shrink_end>tt); if(nxt){cx.setLineDash([6,6]);cx.strokeStyle='#ffffff88';cx.beginPath();cx.arc(X(nxt.center[0]),Y(nxt.center[1]),nxt.radius*view.s,0,7);cx.stroke();cx.setLineDash([]);}
 // items
 const it=itemsAt[fi]; if(it){cx.fillStyle='#b8860b';for(const c of it.c){cx.fillRect(X(c[1])-3,Y(c[2])-3,6,6);}cx.fillStyle='#8ef';for(const c of it.q){cx.fillRect(X(c[1])-2,Y(c[2])-2,4,4);}
  const wc=['#fff','#f80','#f4f','#4ff'];for(const p of it.w){cx.strokeStyle=wc[p[1]];cx.strokeRect(X(p[2])-3,Y(p[3])-3,6,6);}}
 // drops
 for(const d of (f.d||[])){const hd=(H.drops||[]).find(x=>x.id===d[0]);const pos=hd&&hd.pos;if(pos){cx.strokeStyle='#ff0';cx.beginPath();cx.arc(X(pos[0]),Y(pos[1]),8*view.s,0,7);cx.stroke();}}
 // events window (last 1 s)
 for(let k=Math.max(0,fi-10);k<=fi;k++){const a=1-(fi-k)/11;for(const e of evAt[k]){
  if(e.e==='laser'){cx.strokeStyle=`rgba(255,80,255,${a})`;cx.lineWidth=2*devicePixelRatio;cx.beginPath();cx.moveTo(X(e.from[0]),Y(e.from[1]));cx.lineTo(X(e.to[0]),Y(e.to[1]));cx.stroke();}
  else if(e.e==='zap'){cx.strokeStyle=`rgba(80,255,255,${a})`;cx.lineWidth=1.5*devicePixelRatio;cx.beginPath();e.path.forEach((p,i)=>i?cx.lineTo(X(p[0]),Y(p[1])):cx.moveTo(X(p[0]),Y(p[1])));cx.stroke();}
  else if(e.e==='boom'){cx.strokeStyle=`rgba(255,160,40,${a})`;cx.beginPath();cx.arc(X(e.at[0]),Y(e.at[1]),(e.r||7)*view.s,0,7);cx.stroke();}
  else if(e.e==='death'&&e.id<16){const m=FR[k].m.find(m=>m[0]===e.id)||FR[Math.max(0,k-1)].m.find(m=>m[0]===e.id);if(m){cx.strokeStyle=`rgba(255,40,40,${a})`;cx.lineWidth=3;cx.beginPath();cx.moveTo(X(m[1])-8,Y(m[2])-8);cx.lineTo(X(m[1])+8,Y(m[2])+8);cx.moveTo(X(m[1])+8,Y(m[2])-8);cx.lineTo(X(m[1])-8,Y(m[2])+8);cx.stroke();}}
 }}
 cx.lineWidth=1*devicePixelRatio;
 // projectiles
 for(const p of f.p){cx.fillStyle=p[1]===1?'#f80':p[1]===2?'#f55':'#ff8';cx.fillRect(X(p[2])-1.5,Y(p[3])-1.5,3,3);}
 // npcs
 for(const n of f.n){cx.fillStyle='#c33';cx.beginPath();cx.arc(X(n[1]),Y(n[2]),Math.max(2,1.8*view.s),0,7);cx.fill();}
 // mechs
 for(const m of f.m){const x=X(m[1]),y=Y(m[2]);const r=Math.max(3,2.5*(1+0.05*m[9])*view.s);
  cx.fillStyle=col(m[0]);cx.beginPath();cx.arc(x,y,r,0,7);cx.fill(); if(!(m[8]&1)){cx.strokeStyle='#fff';cx.stroke();}
  if(m[8]&4){cx.strokeStyle='#f4f';cx.lineWidth=2;cx.beginPath();cx.arc(x,y,r+3,0,7);cx.stroke();cx.lineWidth=1;}
  cx.strokeStyle=col(m[0]);cx.beginPath();cx.moveTo(x,y);cx.lineTo(x+Math.cos(m[4])*r*4,y-Math.sin(m[4])*r*4);cx.stroke();
  if(tracked.has(m[0])||m[0]===sel){cx.strokeStyle='#ffb340';cx.lineWidth=2;cx.beginPath();cx.arc(x,y,r+5,0,7);cx.stroke();cx.lineWidth=1;}
  cx.fillStyle='#000a';cx.fillRect(x-12,y-r-9,24,4);cx.fillStyle=m[6]/m[10]>0.5?'#4caf50':m[6]/m[10]>0.25?'#fb3':'#f44';cx.fillRect(x-12,y-r-9,24*Math.max(0,m[6]/m[10]),4);
  cx.fillStyle='#fff';cx.font=`${10*devicePixelRatio}px sans-serif`;cx.fillText(`${m[0]} ${String(names[m[0]]||'').replace('house:','')}`,x+r+3,y+3);}
 // debug drawings
 if(f.dbg){for(const [id,d] of Object.entries(f.dbg)){for(const l of (d.lines||[])){cx.strokeStyle=l.color||'#ff0';cx.beginPath();cx.moveTo(X(l.a[0]),Y(l.a[1]));cx.lineTo(X(l.b[0]),Y(l.b[1]));cx.stroke();}
  for(const p of (d.points||[])){cx.fillStyle=p.color||'#ff0';cx.beginPath();cx.arc(X(p.at[0]),Y(p.at[1]),(p.size||4)*devicePixelRatio/2,0,7);cx.fill();}
  for(const t of (d.text||[])){if(t.at){cx.fillStyle=t.color||'#ff0';cx.fillText(t.text,X(t.at[0]),Y(t.at[1]));}}
  const m=f.m.find(m=>m[0]==id);if(m&&(d.label||d.text)){cx.fillStyle='#ffb340';cx.fillText([d.label||'',...(d.text||[]).filter(t=>!t.at).map(t=>t.text)].join(' | '),X(m[1])+10,Y(m[2])-16);}}}
 // panel
 document.getElementById('time').textContent=`t=${tt.toFixed(1)}s  r=${z[2].toFixed(0)}`;
 document.getElementById('slider').value=fi;
 const alive=new Map(f.m.map(m=>[m[0],m]));
 let html='';for(const hm of (H.mechs||[])){const m=alive.get(hm.id);const rr=RES?RES.mechs.find(x=>x.id===hm.id):null;
  html+=`<div class="m ${m?'':'dead'} ${sel===hm.id?'sel':''}" data-id="${hm.id}"><span class="sw" style="background:${col(hm.id)}"></span><div style="flex:1"><b>${hm.id}</b> ${hm.name} ${m?`hp ${m[6]}/${m[10]} e ${m[7]} c ${m[9]} [${[m[11],m[12]].filter(x=>x>=0).map(x=>WK[x]).join(',')}]`:'dead'}${rr?` · place ${rr.place}`:''}<div class="hp"><i style="width:${m?100*m[6]/m[10]:0}%"></i></div></div></div>`;}
 document.getElementById('list').innerHTML=html;
 if(sel!=null){const lines=[];for(let k=Math.max(0,fi-300);k<=fi;k++)for(const e of evAt[k]){if(e.src===sel||e.dst===sel||e.id===sel||e.by===sel){lines.push(`${(e.t/20).toFixed(1)} ${JSON.stringify(e)}`);}}document.getElementById('log').textContent=lines.slice(-40).join('\n');}
 document.getElementById('info').textContent=`frame ${fi}/${FR.length-1}  alive ${f.m.length}`;
}
document.getElementById('list').onclick=e=>{const d=e.target.closest('.m');if(d){sel=+d.dataset.id;document.getElementById('follow').checked=true;draw();}};
const sl=document.getElementById('slider');sl.max=FR.length-1;sl.oninput=()=>{fi=+sl.value;draw();};
document.getElementById('play').onclick=()=>{playing=!playing;document.getElementById('play').textContent=playing?'❚❚':'▶';last=performance.now();if(playing)requestAnimationFrame(loop);};
document.getElementById('spd').onchange=e=>spd=+e.target.value; document.getElementById('trees').onchange=draw;
function loop(now){if(!playing)return;acc+=(now-last)/1000*spd*10;last=now;const n=Math.floor(acc);if(n>0){acc-=n;fi=Math.min(FR.length-1,fi+n);draw();}if(fi>=FR.length-1){playing=false;document.getElementById('play').textContent='▶';}requestAnimationFrame(loop);}
window.onkeydown=e=>{if(e.key===' '){document.getElementById('play').click();e.preventDefault();}if(e.key==='ArrowRight'){fi=Math.min(FR.length-1,fi+10);draw();}if(e.key==='ArrowLeft'){fi=Math.max(0,fi-10);draw();}};
document.getElementById('hdr').innerHTML=`<b>seed ${H.seed}</b> ${H.source||'official'} · ${FR.length} frames`;
if(tracked.size){sel=[...tracked][0];}
window.onresize=resize;resize();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--arena", default=None)
    ap.add_argument("--every", type=int, default=1, help="keep every Nth frame (smaller file)")
    a = ap.parse_args()
    tr = load_trace(a.trace)
    head, frames, rest = tr[0], [r for r in tr[1:] if "m" in r], [r for r in tr[1:] if "m" not in r]
    if a.every > 1:
        kept = []
        carry_ev = []
        for i, f in enumerate(frames):
            carry_ev += f.get("ev", [])
            if i % a.every == 0 or i == len(frames) - 1:
                f = dict(f)
                f["ev"] = carry_ev
                carry_ev = []
                kept.append(f)
        frames = kept
    # header drops: official replay has positions; sim header has none -> fill from drop call events
    drops = {d["id"]: d for d in head.get("drops", [])}
    for f in frames:
        for e in f.get("ev", []):
            if e.get("e") == "drop" and e.get("op") == "call" and e["id"] in drops:
                drops[e["id"]]["pos"] = e["at"]
    head["drops"] = list(drops.values())
    data = [head] + frames + rest
    out = a.out or os.path.splitext(a.trace.replace(".gz", ""))[0] + ".html"
    html = HTML.replace("__MAP__", json.dumps(map_data(a.arena), separators=(",", ":"))).replace("__TRACE__", json.dumps(data, separators=(",", ":")))
    with open(out, "w") as f:
        f.write(html)
    print(out, f"{os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
