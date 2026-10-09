"""HTML for the client page and the staff page. All client/staff data is put into the page with
html.escape (server side) or textContent (in the browser), never as raw HTML."""

from __future__ import annotations

import html as h
import json

from .config import Settings
from .store import parse

STYLE = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d2327;--muted:#5f6b72;--line:#dde2e5;--brand:#0f6e6a;
--brand-ink:#fff;--ok:#1f7a3a;--bad:#b42318;--soft:#e8f2f1;--focus:#0f6e6a55}
@media (prefers-color-scheme:dark){:root{--bg:#121617;--card:#1b2124;--ink:#e8ecee;--muted:#9aa7ae;
--line:#2e3639;--brand:#3fb3ab;--brand-ink:#08201f;--ok:#5cc982;--bad:#ff8a80;--soft:#1d2f2e;
--focus:#3fb3ab55}}
*{box-sizing:border-box}html,body{margin:0}[hidden]{display:none!important}
a{color:var(--brand)}
body{background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:32px 16px 64px}
.brand{font-weight:700;letter-spacing:.02em;color:var(--brand);font-size:15px;
text-transform:uppercase}
h1{font-size:26px;line-height:1.25;margin:8px 0 6px}
p{margin:0 0 12px}.muted{color:var(--muted)}.small{font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;
margin-top:20px}
button,.btn{font:inherit;border:0;border-radius:8px;padding:10px 16px;cursor:pointer;
background:var(--brand);color:var(--brand-ink);font-weight:600;text-decoration:none;
display:inline-block}
button.ghost,.btn.ghost{background:transparent;color:var(--brand);border:1px solid var(--line)}
button.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,
.drop:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
input,select,textarea{font:inherit;color:var(--ink);background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:10px 12px;width:100%}
label{display:block;font-weight:600;font-size:14px;margin:12px 0 4px}
.err{color:var(--bad);font-weight:600}.ok{color:var(--ok);font-weight:600}
.lock{display:inline-flex;gap:8px;align-items:center;color:var(--muted);font-size:14px}
.mt{margin-top:16px}.h2{margin:0 0 4px;font-size:18px}.h2s{margin:0;font-size:18px}
.between{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.check{font-weight:400}.check input{width:auto;margin-right:6px}.narrow{max-width:260px}
"""

PUBLIC_STYLE = STYLE + """
.code{font-size:28px;letter-spacing:.4em;text-align:center;max-width:260px}
.drop{border:2px dashed var(--line);border-radius:12px;padding:40px 16px;text-align:center;
background:var(--soft);transition:border-color .15s}
.drop.over{border-color:var(--brand)}
.drop strong{display:block;font-size:18px;margin-bottom:6px}
.files{list-style:none;padding:0;margin:16px 0 0}
.files li{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:8px;
display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center}
.files .name{overflow-wrap:anywhere}
.bar{grid-column:1/-1;height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.bar span{display:block;height:100%;width:0;background:var(--brand)}
"""

ADMIN_STYLE = STYLE + """
.wrap{max-width:1100px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.state{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.state.open{color:var(--ok)}.state.closed,.state.expired,.state.locked{color:var(--muted)}
.state.locked{color:var(--bad)}
.filelist{margin:6px 0 0;padding:0;list-style:none}
.filelist li{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:4px 0}
.actions{display:flex;gap:6px;flex-wrap:wrap}
.actions button{padding:6px 10px;font-size:13px}
.secret{background:var(--soft);border-radius:8px;padding:12px;margin-top:12px}
.secret code{font-size:15px;overflow-wrap:anywhere}
.tablewrap{overflow-x:auto}
"""


def _page(title: str, style: str, body: str, nonce: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='robots' content='noindex,nofollow'><title>{h.escape(title)}</title>"
            f"<style nonce='{nonce}'>{style}</style></head><body>{body}</body></html>")


def closed_page(s: Settings, state: str, nonce: str) -> str:
    why = {"missing": "This link isn't valid. Please check you copied all of it.",
           "expired": "This link has expired.",
           "closed": "This link has been closed.",
           "locked": "This link was locked after too many wrong codes."}.get(state, "")
    body = (f"<div class='wrap'><div class='brand'>{h.escape(s.brand)}</div>"
            f"<h1>Secure upload</h1><div class='card'><p>{h.escape(why)}</p>"
            f"<p class='muted'>Please contact your adviser for a new link.</p></div></div>")
    return _page(f"{s.brand} secure upload", PUBLIC_STYLE, body, nonce)


def public_page(s: Settings, link: dict, unlocked: bool, nonce: str,
                allowed: list[str]) -> str:
    expires = parse(link["expires_at"]).astimezone().strftime("%d %B %Y")
    message = (f"<p>{h.escape(link['message'])}</p>" if link.get("message") else "")
    types = ", ".join(a.lstrip(".").upper() for a in allowed)
    if not unlocked:
        main = f"""
<div class='card'>
  <p>Enter the 6-digit code your adviser gave you.</p>
  <form id='codeform' autocomplete='off'>
    <label for='code'>Code</label>
    <input id='code' class='code' inputmode='numeric' pattern='[0-9]*' maxlength='6' required
      autocomplete='one-time-code'>
    <p id='codemsg' class='err' role='alert'></p>
    <button type='submit'>Continue</button>
  </form>
</div>"""
    else:
        main = f"""
<div class='card'>
  <div id='drop' class='drop' tabindex='0' role='button'
    aria-label='Drop files here or press Enter to choose files'>
    <strong>Drag and drop your files here</strong>
    <span class='muted'>or</span><br><br>
    <button type='button' id='choose'>Choose files</button>
    <input id='picker' type='file' multiple hidden>
  </div>
  <p class='muted small mt'>PDFs, photos and Office documents up to
    {s.max_mb} MB each ({h.escape(types)}).</p>
  <ul id='files' class='files' aria-live='polite'></ul>
  <div id='finish' class='mt' hidden>
    <button type='button' id='donebtn'>I'm finished</button>
    <p id='donemsg' class='ok' role='status'></p>
  </div>
</div>"""
    body = f"""
<div class='wrap'>
  <div class='brand'>{h.escape(s.brand)}</div>
  <h1>Secure upload for {h.escape(link['client_name'])}</h1>
  {message}
  <p class='lock'><span aria-hidden='true'>&#128274;</span> Files are encrypted and checked for
    viruses as they upload. Only {h.escape(s.brand)} staff can open them.</p>
  {main}
  <p class='muted small mt'>This link works until {h.escape(expires)}.
    You can come back and add more files until then.</p>
</div>
<script nonce='{nonce}'>{PUBLIC_JS}</script>"""
    return _page(f"{s.brand} secure upload", PUBLIC_STYLE, body, nonce)


PUBLIC_JS = r"""
(function(){
  var base = location.pathname.replace(/\/+$/, '');
  function post(path, body){
    return fetch(base + path, {method:'POST', credentials:'same-origin',
      headers:{'X-Vault':'1','Content-Type':'application/json'}, body:JSON.stringify(body||{})})
      .then(function(r){ return r.json().catch(function(){ return {ok:false,
        error:'Something went wrong. Please try again.'}; }); });
  }
  var form = document.getElementById('codeform');
  if (form) {
    var input = document.getElementById('code'), msg = document.getElementById('codemsg');
    input.focus();
    form.addEventListener('submit', function(e){
      e.preventDefault(); msg.textContent = '';
      post('/code', {code: input.value.trim()}).then(function(r){
        if (r.ok) { location.reload(); } else { msg.textContent = r.error; input.select(); }
      });
    });
    return;
  }
  var drop = document.getElementById('drop'), picker = document.getElementById('picker');
  var list = document.getElementById('files'), finish = document.getElementById('finish');
  var queue = [], busy = false;
  function size(n){ return n < 1048576 ? Math.max(1, Math.round(n/1024)) + ' KB'
    : (n/1048576).toFixed(1) + ' MB'; }
  function add(files){
    for (var i = 0; i < files.length; i++) {
      var f = files[i], li = document.createElement('li');
      var name = document.createElement('span'); name.className = 'name';
      name.textContent = f.name + ' (' + size(f.size) + ')';
      var status = document.createElement('span'); status.className = 'muted small';
      status.textContent = 'Waiting';
      var bar = document.createElement('div'); bar.className = 'bar';
      var fill = document.createElement('span'); bar.appendChild(fill);
      li.appendChild(name); li.appendChild(status); li.appendChild(bar);
      list.appendChild(li);
      queue.push({file:f, status:status, fill:fill, bar:bar});
    }
    next();
  }
  function next(){
    if (busy || !queue.length) return;
    busy = true;
    var job = queue.shift(), xhr = new XMLHttpRequest();
    job.status.textContent = 'Uploading';
    xhr.open('POST', base + '/upload');
    xhr.setRequestHeader('X-Vault', '1');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(job.file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = function(e){
      if (e.lengthComputable) {
        job.fill.style.width = Math.round(e.loaded / e.total * 100) + '%';
        if (e.loaded === e.total) job.status.textContent = 'Checking';
      }
    };
    xhr.onload = function(){
      var r = {}; try { r = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 200 && r.ok) {
        job.status.className = 'ok small'; job.status.textContent = 'Received';
        job.fill.style.width = '100%'; finish.hidden = false;
      } else {
        job.status.className = 'err small';
        job.status.textContent = r.error || 'Upload failed. Please try again.';
        job.bar.hidden = true;
        if (xhr.status === 401 || xhr.status === 410) { setTimeout(function(){
          location.reload(); }, 2500); }
      }
      busy = false; next();
    };
    xhr.onerror = function(){
      job.status.className = 'err small';
      job.status.textContent = 'Connection lost. Please try again.';
      busy = false; next();
    };
    xhr.send(job.file);
  }
  document.getElementById('choose').addEventListener('click', function(e){
    e.stopPropagation(); picker.click(); });
  drop.addEventListener('keydown', function(e){
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); picker.click(); } });
  picker.addEventListener('change', function(){ add(picker.files); picker.value = ''; });
  ['dragenter','dragover'].forEach(function(t){ drop.addEventListener(t, function(e){
    e.preventDefault(); drop.classList.add('over'); }); });
  ['dragleave','drop'].forEach(function(t){ drop.addEventListener(t, function(e){
    e.preventDefault(); drop.classList.remove('over'); }); });
  drop.addEventListener('drop', function(e){ add(e.dataTransfer.files); });
  window.addEventListener('dragover', function(e){ e.preventDefault(); });
  window.addEventListener('drop', function(e){ e.preventDefault(); });
  document.getElementById('donebtn').addEventListener('click', function(){
    var btn = this; btn.disabled = true;
    post('/done').then(function(r){
      document.getElementById('donemsg').textContent = r.ok
        ? 'Thank you. We have let your adviser know.' : (r.error || 'Please try again.');
      if (!r.ok) btn.disabled = false;
    });
  });
})();
"""


def admin_page(s: Settings, who: str, nonce: str, email_enabled: bool) -> str:
    cfg = json.dumps({"email": email_enabled, "days": s.default_days, "brand": s.brand})
    body = f"""
<div class='wrap'>
  <div class='brand'>{h.escape(s.brand)} vault</div>
  <h1>Client uploads</h1>
  <p class='muted'>Signed in as {h.escape(who)}. Send a client a link and, separately, its
    6-digit code. Files arrive encrypted and virus-checked.</p>

  <div class='card'>
    <h2 class='h2'>New upload link</h2>
    <form id='create' autocomplete='off'>
      <div class='grid'>
        <div><label for='client_name'>Client name</label>
          <input id='client_name' required maxlength='120'></div>
        <div><label for='client_ref'>Xplan ID / family group (optional)</label>
          <input id='client_ref' maxlength='60'></div>
        <div><label for='client_email'>Client email (optional)</label>
          <input id='client_email' type='email' maxlength='200'></div>
        <div><label for='days'>Link works for</label>
          <select id='days'><option value='7'>7 days</option>
            <option value='14' selected>14 days</option><option value='30'>30 days</option>
            <option value='60'>60 days</option></select></div>
      </div>
      <label for='message'>Message shown to the client (optional)</label>
      <textarea id='message' rows='2' maxlength='1000'
        placeholder='e.g. Please upload your latest super statements and payslips.'></textarea>
      <label id='sendwrap' class='check' hidden><input type='checkbox' id='send_email'> Email the link to the client (the code is never emailed)</label>
      <p id='createmsg' class='err' role='alert'></p>
      <button type='submit'>Create link</button>
    </form>
    <div id='secret' class='secret' hidden></div>
  </div>

  <div class='card'>
    <div class='between'>
      <h2 class='h2s'>Links</h2>
      <input id='filter' placeholder='Filter by client' class='narrow'>
    </div>
    <div class='tablewrap'><table>
      <thead><tr><th>Client</th><th>Files</th><th>Status</th><th>Created</th><th></th></tr>
      </thead><tbody id='rows'></tbody></table></div>
  </div>

  <div class='card'>
    <h2 class='h2'>Activity</h2>
    <div class='tablewrap'><table><thead><tr><th>When</th><th>Who</th><th>What</th>
      <th>Client</th><th>Detail</th></tr></thead><tbody id='audit'></tbody></table></div>
  </div>
</div>
<script nonce='{nonce}'>var CFG = {cfg};{ADMIN_JS}</script>"""
    return _page(f"{s.brand} vault", ADMIN_STYLE, body, nonce)


ADMIN_JS = r"""
(function(){
  function el(tag, text, cls){ var e = document.createElement(tag);
    if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function when(iso){ if (!iso) return ''; var d = new Date(iso);
    return d.toLocaleDateString('en-AU', {day:'numeric', month:'short', year:'numeric'}) + ' ' +
      d.toLocaleTimeString('en-AU', {hour:'numeric', minute:'2-digit'}); }
  function api(path, body){
    var opt = body === undefined ? {credentials:'same-origin'} : {method:'POST',
      credentials:'same-origin', headers:{'X-Vault':'1','Content-Type':'application/json'},
      body:JSON.stringify(body)};
    return fetch('/vault/api/' + path, opt).then(function(r){ return r.json(); });
  }
  if (CFG.email) document.getElementById('sendwrap').hidden = false;
  var secretBox = document.getElementById('secret');
  function copyBtn(label, value){
    var b = el('button', label, 'ghost'); b.type = 'button';
    b.addEventListener('click', function(){ navigator.clipboard.writeText(value).then(function(){
      b.textContent = 'Copied'; setTimeout(function(){ b.textContent = label; }, 1500); }); });
    return b;
  }
  function showSecret(name, url, code, emailed){
    secretBox.textContent = ''; secretBox.hidden = false;
    secretBox.appendChild(el('p', 'Link for ' + name + (emailed ? ' (emailed to the client)' : '')
      + ':'));
    var c1 = el('p'); c1.appendChild(el('code', url)); secretBox.appendChild(c1);
    secretBox.appendChild(el('p', 'Code: ' + code + '. Send it separately, by SMS or phone, '
      + 'never in the same email as the link.'));
    var row = el('div', null, 'actions');
    row.appendChild(copyBtn('Copy link', url)); row.appendChild(copyBtn('Copy code', code));
    row.appendChild(copyBtn('Copy SMS text', 'Your ' + CFG.brand + ' upload code is ' + code));
    secretBox.appendChild(row);
    secretBox.scrollIntoView({behavior:'smooth', block:'nearest'});
  }
  document.getElementById('create').addEventListener('submit', function(e){
    e.preventDefault();
    var msg = document.getElementById('createmsg'); msg.textContent = '';
    var body = {client_name: v('client_name'), client_ref: v('client_ref'),
      client_email: v('client_email'), days: +v('days'), message: v('message'),
      send_email: CFG.email && document.getElementById('send_email').checked};
    api('links', body).then(function(r){
      if (!r.ok) { msg.textContent = r.error; return; }
      showSecret(body.client_name, r.url, r.code, r.emailed);
      e.target.reset(); load();
    });
  });
  function v(id){ return document.getElementById(id).value.trim(); }
  var all = [];
  document.getElementById('filter').addEventListener('input', render);
  function render(){
    var q = v('filter').toLowerCase(), tb = document.getElementById('rows');
    tb.textContent = '';
    all.filter(function(l){ return !q || (l.client_name + ' ' + (l.client_ref || ''))
      .toLowerCase().indexOf(q) >= 0; }).forEach(function(l){
      var tr = el('tr');
      var c = el('td'); c.appendChild(el('div', l.client_name));
      if (l.client_ref) c.appendChild(el('div', l.client_ref, 'muted small'));
      tr.appendChild(c);
      var f = el('td');
      if (!l.files.length) f.appendChild(el('span', 'None yet', 'muted'));
      var ul = el('ul', null, 'filelist');
      l.files.forEach(function(file){
        var li = el('li'), a = el('a', file.name);
        a.href = '/vault/files/' + file.id;
        li.appendChild(a); li.appendChild(el('span', file.size_text + ' · ' + when(file.uploaded_at)
          + (file.scan === 'clean' ? '' : ' · ' + file.scan), 'muted small'));
        var del = el('button', 'Delete', 'danger'); del.type = 'button';
        del.style.padding = '2px 8px'; del.style.fontSize = '12px';
        del.addEventListener('click', function(){
          if (confirm('Delete ' + file.name + '? This cannot be undone.'))
            api('files/' + file.id + '/delete', {}).then(load); });
        li.appendChild(del); ul.appendChild(li);
      });
      f.appendChild(ul); tr.appendChild(f);
      var st = el('td'); st.appendChild(el('div', l.state, 'state ' + l.state));
      st.appendChild(el('div', 'until ' + when(l.expires_at), 'muted small'));
      if (l.finished_at) st.appendChild(el('div', 'client finished ' + when(l.finished_at),
        'ok small'));
      tr.appendChild(st);
      var cr = el('td'); cr.appendChild(el('div', when(l.created_at)));
      cr.appendChild(el('div', l.created_by, 'muted small')); tr.appendChild(cr);
      var act = el('td'), row = el('div', null, 'actions');
      var show = el('button', 'Link & code', 'ghost'); show.type = 'button';
      show.addEventListener('click', function(){ api('links/' + l.id + '/reveal').then(function(r){
        if (r.ok) showSecret(l.client_name, r.url, r.code, false); }); });
      row.appendChild(show);
      if (l.state === 'open') {
        var close = el('button', 'Close', 'ghost'); close.type = 'button';
        close.addEventListener('click', function(){ api('links/' + l.id + '/close', {})
          .then(load); });
        row.appendChild(close);
      } else {
        var re = el('button', 'Reopen 14 days', 'ghost'); re.type = 'button';
        re.addEventListener('click', function(){ api('links/' + l.id + '/extend', {days:14})
          .then(load); });
        row.appendChild(re);
      }
      act.appendChild(row); tr.appendChild(act);
      tb.appendChild(tr);
    });
  }
  function load(){
    api('links').then(function(r){ all = r.links || []; render(); });
    api('audit').then(function(r){
      var tb = document.getElementById('audit'); tb.textContent = '';
      (r.events || []).slice(0, 50).forEach(function(a){
        var tr = el('tr');
        [when(a.at), a.actor, a.action, a.client_name || '', a.detail || ''].forEach(function(t){
          tr.appendChild(el('td', t)); });
        tb.appendChild(tr);
      });
    });
  }
  load(); setInterval(load, 60000);
})();
"""
