"""A small local web page for reviewing and merging duplicate family groups and entities.

    python -m xplan_extract merge-ui --db <url>        then open http://127.0.0.1:8765

It only listens on this machine (127.0.0.1). On the server, reach it through an SSH tunnel:
    ssh -L 8765:127.0.0.1:8765 brightly@<server>
The page shows likely duplicates, lines the two records up field by field, pre-selects the
merge rules (filled beats blank, the kept record wins a conflict) and lets you change any pick
or exclude a field before merging. Merges are logged in change_log and merge_record.
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sqlalchemy as sa

from . import merge

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brightly Merge</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400&family=DM+Mono&family=Montserrat:wght@400;500;600&display=swap">
<style>
:root{--bg:#fffdf8;--alt:#f6f3ec;--ink:#0a0a0a;--muted:#5a5751;--accent:#1a5c4a;--rule:#2e8b6e;
--line:rgba(10,10,10,.14);--conflict:#b0812a;--onlya:#3e6c9b;--sans:Montserrat,Arial,sans-serif;
--mono:"DM Mono",Consolas,monospace;--display:"Cormorant Garamond",Garamond,serif}
@media (prefers-color-scheme:dark){:root{--bg:#0a0a0a;--alt:#1a1a1a;--ink:#fffdf8;--muted:#a8a49b;
--accent:#2e8b6e;--line:rgba(255,253,248,.16);color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans)}
header{padding:20px 24px;border-bottom:2px solid var(--rule);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font:400 30px var(--display);margin:0}.muted{color:var(--muted)}
main{display:grid;grid-template-columns:320px minmax(0,1fr);min-height:calc(100vh - 70px)}
aside{border-right:1px solid var(--line);padding:16px;overflow:auto;max-height:calc(100vh - 70px)}
section{padding:16px 24px;overflow:auto}
.tabs button,.btn{font:600 12px var(--sans);letter-spacing:.08em;text-transform:uppercase;padding:8px 12px;
border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer;border-radius:0}
.tabs button[aria-pressed=true],.btn.primary{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.btn:disabled{opacity:.4;cursor:default}
input[type=text]{font:inherit;padding:7px 9px;border:1px solid var(--line);background:var(--bg);color:var(--ink);width:100%;border-radius:2px}
.pair{display:block;width:100%;text-align:left;padding:10px;border:0;border-bottom:1px solid var(--line);background:none;color:inherit;cursor:pointer;font:inherit}
.pair:hover,.pair[aria-current=true]{background:var(--alt)}
.pair small{display:block;color:var(--muted);font:12px var(--mono)}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.card{border:1px solid var(--line);padding:12px 14px}.card.kept{border:2px solid var(--accent)}
.card h2{font:400 22px var(--display);margin:0 0 4px}.id{font:12px var(--mono);color:var(--muted)}
table{border-collapse:collapse;width:100%}th,td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font:600 11px var(--sans);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
td.v{font-size:13px;max-width:320px;overflow-wrap:anywhere}td.v.win{background:var(--alt);font-weight:500}
td.pick{white-space:nowrap;text-align:center}tr.excluded td.v{opacity:.45;background:none}
.tag{display:inline-block;font:600 10px var(--sans);letter-spacing:.08em;text-transform:uppercase;padding:2px 6px;border:1px solid var(--line)}
.tag.conflict{border-color:var(--conflict);color:var(--conflict)}
.bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:12px 0}
.confirm{border:2px solid var(--conflict);padding:12px 14px;margin:12px 0}
.ok{border:2px solid var(--accent);padding:12px 14px;margin:12px 0}.err{color:#b4463f}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style></head><body>
<header><h1>Brightly Merge</h1><span class="muted">Duplicate family groups and entities: compare side by side, choose, merge.</span></header>
<main>
<aside>
  <div class="tabs bar"><button id="t-fg" aria-pressed="true">Family groups</button><button id="t-en" aria-pressed="false">Entities</button></div>
  <label class="muted" for="filter">Filter</label><input type="text" id="filter" placeholder="Name or id">
  <p class="muted" id="count"></p><div id="pairs"></div>
  <p class="muted">Or compare any two ids:</p>
  <div class="bar"><input type="text" id="ida" placeholder="First id" style="width:45%"><input type="text" id="idb" placeholder="Second id" style="width:45%"></div>
  <button class="btn" id="go">Compare</button>
</aside>
<section id="main"><p class="muted">Pick a likely duplicate on the left.</p></section>
</main>
<script>
const TOKEN="__TOKEN__";let kind="family_group",pairs=[],cmp=null,keep="a",picks={},excl=new Set(),onlyDiff=true,current=null;
const $=s=>document.querySelector(s),el=(t,a={},...k)=>{const n=document.createElement(t);for(const[x,y]of Object.entries(a)){if(x==="class")n.className=y;else if(x.startsWith("on"))n.addEventListener(x.slice(2),y);else if(y!==false&&y!=null)n.setAttribute(x,y===true?"":y)}for(const c of k)n.append(c?.nodeType?c:document.createTextNode(c??""));return n};
const show=v=>v==null||v===""?"(blank)":Array.isArray(v)?v.map(i=>typeof i==="object"?Object.values(i).filter(x=>x!==""&&x!=null).join(", "):i).join(" | "):typeof v==="boolean"?(v?"Yes":"No"):String(v);
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json","X-Merge-Token":TOKEN},body:JSON.stringify(body)}:{});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function loadPairs(){$("#pairs").textContent="Looking for duplicates…";pairs=(await api("/api/candidates?kind="+kind)).pairs;renderPairs()}
function renderPairs(){const f=$("#filter").value.toLowerCase(),box=$("#pairs");box.textContent="";const list=pairs.filter(p=>!f||(p.a+p.b+p.a_name+p.b_name).toLowerCase().includes(f));
$("#count").textContent=list.length+" likely duplicate"+(list.length===1?"":"s");
for(const p of list)box.append(el("button",{class:"pair","aria-current":current===p.a+p.b,onclick:()=>open(p.a,p.b)},p.a_name+"  /  "+p.b_name,el("small",{},p.a+" · "+p.b+" · "+p.reasons.join(", "))))}
async function open(a,b){current=a+b;renderPairs();$("#main").textContent="Loading…";try{cmp=await api(`/api/compare?kind=${kind}&a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`)}catch(e){$("#main").textContent="";$("#main").append(el("p",{class:"err"},e.message));return}
keep="a";picks={};excl=new Set();render()}
const ids=()=>({k:keep==="a"?cmp.a.id:cmp.b.id,d:keep==="a"?cmp.b.id:cmp.a.id});
function winner(f){if(excl.has(f.key))return keep;if(picks[f.key])return picks[f.key];if(f.state==="a_only")return"a";if(f.state==="b_only")return"b";return keep}
function render(){const m=$("#main");m.textContent="";const{a,b,children,fields}=cmp;
const card=(s,r)=>el("div",{class:"card"+(keep===s?" kept":"")},el("h2",{},r.name),el("div",{class:"id"},r.id+(r.xplan_id?" · Xplan "+r.xplan_id:"")+(r.status?" · "+r.status:"")),
 el("p",{class:"muted"},Object.entries(children[s]).filter(([,n])=>n).map(([k,n])=>n+" "+k).join(", ")||"Nothing attached"),
 el("label",{},el("input",{type:"radio",name:"keep",checked:keep===s,onchange:()=>{keep=s;render()}})," Keep this record"));
m.append(el("div",{class:"cards"},card("a",a),card("b",b)));
const conflicts=fields.filter(f=>f.state==="conflict").length,fills=fields.filter(f=>f.state===(keep==="a"?"b_only":"a_only")).length;
m.append(el("div",{class:"bar"},el("span",{},`${conflicts} conflict${conflicts===1?"":"s"} · ${fills} field${fills===1?"":"s"} filled from the other record`),
 el("label",{},el("input",{type:"checkbox",checked:onlyDiff,onchange:e=>{onlyDiff=e.target.checked;render()}})," Differences only")));
const tb=el("tbody");
for(const f of fields){if(onlyDiff&&f.state==="same")continue;const w=winner(f),ex=excl.has(f.key);
 const pick=s=>f.state==="same"?"":el("input",{type:"radio",name:"p-"+f.key,"aria-label":"Use "+s.toUpperCase()+" for "+f.label,checked:!ex&&w===s,disabled:ex,onchange:()=>{picks[f.key]=s;render()}});
 const both=f.group&&f.state==="conflict"?el("label",{},el("input",{type:"radio",name:"p-"+f.key,checked:!ex&&picks[f.key]==="combine",disabled:ex,onchange:()=>{picks[f.key]="combine";render()}})," both"):"";
 tb.append(el("tr",{class:ex?"excluded":""},el("td",{},f.label," ",f.state==="conflict"?el("span",{class:"tag conflict"},"conflict"):""),
  el("td",{class:"v"+(w==="a"&&!ex||picks[f.key]==="combine"?" win":"")},show(f.a)),el("td",{class:"pick"},pick("a")," ",pick("b")," ",both),
  el("td",{class:"v"+(w==="b"&&!ex||picks[f.key]==="combine"?" win":"")},show(f.b)),
  el("td",{class:"pick"},f.state==="same"?"":el("input",{type:"checkbox","aria-label":"Exclude "+f.label,checked:ex,onchange:e=>{e.target.checked?excl.add(f.key):excl.delete(f.key);render()}}))))}
m.append(el("table",{},el("thead",{},el("tr",{},el("th",{},"Field"),el("th",{},"Record A"),el("th",{},"Use"),el("th",{},"Record B"),el("th",{},"Exclude"))),tb));
m.append(el("p",{class:"muted"},"Excluded fields keep the kept record's value as it is, even if blank. Everything attached to the other record (people, notes, accounts, tasks, documents, roles) moves to the kept record."));
m.append(el("div",{class:"bar"},el("button",{class:"btn primary",onclick:confirmMerge},"Merge into "+(keep==="a"?a:b).name)))}
function payload(){const{k,d}=ids(),p={};for(const f of cmp.fields){if(f.state==="same"||excl.has(f.key))continue;const s=picks[f.key];if(s==="combine")p[f.key]="combine";else{const w=winner(f);p[f.key]=w==="a"?cmp.a.id:cmp.b.id}}return{kind,keep:k,drop:d,picks:p,exclude:[...excl]}}
function confirmMerge(){const{k,d}=ids(),box=el("div",{class:"confirm"},el("p",{},`Merge ${d} into ${k}? ${d} will be removed and everything attached to it moves to ${k}. This can't be undone from this page (a snapshot is kept in merge_record).`),
 el("div",{class:"bar"},el("button",{class:"btn primary",onclick:doMerge},"Yes, merge"),el("button",{class:"btn",onclick:()=>box.remove()},"Cancel")));$("#main").append(box)}
async function doMerge(){try{const r=await api("/api/merge",payload());$("#main").textContent="";$("#main").append(el("div",{class:"ok"},el("p",{},`Merged ${r.dropped} into ${r.kept}.`),el("p",{class:"muted"},Object.entries(r.moved).filter(([,n])=>n).map(([k,n])=>n+" "+k+" moved").join(", "))));current=null;loadPairs()}
catch(e){$("#main").append(el("p",{class:"err"},"Merge failed, nothing changed: "+e.message))}}
const setKind=k=>{kind=k;$("#t-fg").setAttribute("aria-pressed",k==="family_group");$("#t-en").setAttribute("aria-pressed",k==="entity");$("#main").textContent="";loadPairs()};
$("#t-fg").onclick=()=>setKind("family_group");$("#t-en").onclick=()=>setKind("entity");$("#filter").oninput=renderPairs;
$("#go").onclick=()=>{const a=$("#ida").value.trim(),b=$("#idb").value.trim();if(a&&b)open(a,b)};loadPairs();
</script></body></html>"""


def serve(engine: sa.Engine, port: int = 8765, actor: str = "", progress=print) -> None:
    token = secrets.token_urlsafe(24)
    page = PAGE.replace("__TOKEN__", token).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep client data out of the console
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(page)
                elif u.path == "/api/candidates":
                    self._json(200, {"pairs": merge.find_candidates(engine, q.get("kind", "family_group"))})
                elif u.path == "/api/compare":
                    self._json(200, merge.compare(engine, q["kind"], q["a"], q["b"]))
                else:
                    self._json(404, {"error": "not found"})
            except (merge.MergeError, KeyError) as exc:
                self._json(400, {"error": str(exc)})

        def do_POST(self):
            if self.path != "/api/merge" or self.headers.get("X-Merge-Token") != token:
                return self._json(403, {"error": "refused"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                rep = merge.apply_merge(engine, body["kind"], body["keep"], body["drop"],
                                        picks=body.get("picks"), exclude=body.get("exclude"),
                                        actor=actor)
                progress(f"Merged {rep['dropped']} into {rep['kept']}")
                self._json(200, rep)
            except (merge.MergeError, KeyError, ValueError) as exc:
                self._json(400, {"error": str(exc)})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    progress(f"Brightly Merge is running: open http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
