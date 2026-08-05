#!/usr/bin/env python3
"""Small, dependency-free LAN dashboard for ptzpad."""
import hmac
import json
import os
import secrets
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ptz_config import load_config, save_config

TOKEN_FILE = Path(os.environ.get("PTZPAD_TOKEN_FILE", "~/.config/ptzpad/token")).expanduser()
STATE_FILE = Path(os.environ.get("PTZPAD_STATE", "/run/ptzpad/status.json")).expanduser()
MAX_BODY = 128 * 1024

def token():
    try: return TOKEN_FILE.read_text().strip()
    except OSError:
        TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(value); os.chmod(TOKEN_FILE, 0o600)
        print(
            f"Dashboard listening on port {os.environ.get('PTZPAD_PORT', '8080')}; "
            f"token file: {TOKEN_FILE}"
        )
        return value

TOKEN = token()

def state():
    try:
        data = json.loads(STATE_FILE.read_text())
        data["stale"] = time.time() - float(data.get("heartbeat", 0)) > 10
        return data
    except Exception: return {"service": "offline", "stale": True}

def probe(cam):
    if cam["protocol"] == "udp": return "indeterminate"
    try:
        with socket.create_connection((cam["host"], cam["port"]), .35): return "reachable"
    except OSError: return "unreachable"

def joysticks():
    result = []
    for path in Path("/sys/class/input").glob("js*/device/name"):
        try: result.append({"name": path.read_text().strip(), "path": str(path.parent.parent)})
        except OSError: pass
    return result

HTML = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PTZPad status</title>
<style>
:root{color-scheme:dark;font:15px system-ui;background:#0b1020;color:#edf2f7}
body{max-width:1100px;margin:auto;padding:24px}h1{margin-bottom:4px}h2{font-size:17px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
.card{background:#172033;border:1px solid #29354d;border-radius:14px;padding:18px;margin:14px 0}
.camera{padding:12px 0;border-bottom:1px solid #334155}.ok{color:#63d6a0}.bad{color:#fb7185}
.muted{color:#9ca3af}.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
button,input,select,textarea{box-sizing:border-box;padding:9px;border-radius:7px;border:1px solid #45536d;background:#0f172a;color:white}
button{cursor:pointer;background:#2563eb}textarea{width:100%;font:13px ui-monospace;min-height:190px}
pre{white-space:pre-wrap;max-height:380px;overflow:auto;background:#0b1020;padding:12px;border-radius:8px}
</style>
<body><h1>PTZPad</h1><div class="muted" id="msg">Enter the dashboard token.</div>
<div class="grid"><section class="card"><h2>Bridge</h2><div id="status">—</div></section>
<section class="card"><h2>Controllers</h2><div id="controller">—</div></section></div>
<section class="card"><h2>Cameras</h2><div id="cams"></div></section>
<section class="card"><h2>Configuration</h2><p class="muted">Edit the validated camera list and tuning values. Changes hot-reload without restarting the bridge.</p>
<textarea id="cfg" spellcheck="false"></textarea><div class="controls"><button id="save">Save configuration</button><button id="reload">Discard edits</button></div></section>
<section class="card"><h2>Logs</h2><div class="controls"><label>Lines<br><input id="lines" type="number" min="1" max="500" value="100"></label>
<label>Level<br><select id="level"><option value="">All</option><option>ERROR</option><option>WARNING</option><option>INFO</option></select></label>
<label>Search<br><input id="search"></label><button id="logs">Refresh</button></div><pre id="log"></pre></section>
<script>
const $=id=>document.getElementById(id);let token=sessionStorage.ptzToken||prompt('Dashboard token');
if(token)sessionStorage.ptzToken=token;let dirty=false;
async function api(url,options={}){const response=await fetch(url,{...options,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}});if(!response.ok)throw new Error(await response.text());return response.json()}
function text(tag,value,cls=''){const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;return node}
function renderCameras(cameras){const nodes=cameras.map((camera,index)=>{const row=document.createElement('div');row.className='camera';
row.append(text('strong',(index+1)+'. '+(camera.name||camera.host)));
row.append(text('div',(camera.model||'Model not set')+' • '+camera.host+':'+camera.port+' • '+camera.protocol.toUpperCase()));
row.append(text('div',camera.reachability,camera.reachability==='reachable'?'ok':'muted'));return row});$('cams').replaceChildren(...nodes)}
function renderControllers(data){const items=[];if(data.state.controller?.connected)items.push('Active: '+data.state.controller.name+(data.state.controller.wireless?' (wireless)':''));
for(const pad of data.controllers)items.push(pad.name);$('controller').replaceChildren(...(items.length?items:['No controller connected']).map(value=>text('div',value)))}
async function loadConfig(force=false){if(dirty&&!force)return;$('cfg').value=JSON.stringify(await api('/api/config'),null,2);dirty=false}
async function refresh(){try{const data=await api('/api/status');const state=data.state;const uptime=data.uptime==null?'unknown':Math.floor(data.uptime/3600)+'h';
$('status').replaceChildren(text('div',data.hostname+' • '+(state.stale?'offline/stale':'online'),state.stale?'bad':'ok'),text('div','Host uptime '+uptime+' • load '+data.load.map(v=>v.toFixed(2)).join(' / ')),text('div','Speed '+state.max_speed+' • deadzone '+state.deadzone+' • zoom '+state.zoom_speed));
renderControllers(data);renderCameras(data.cameras);await loadConfig();$('msg').textContent='Connected'}catch(error){$('msg').textContent='Authentication or service error: '+error.message}}
async function save(){try{await api('/api/config',{method:'PUT',body:$('cfg').value});dirty=false;$('msg').textContent='Configuration saved';await refresh()}catch(error){$('msg').textContent='Configuration rejected: '+error.message}}
async function logs(){try{const query=new URLSearchParams({lines:$('lines').value,level:$('level').value,search:$('search').value});$('log').textContent=(await api('/api/logs?'+query)).text}catch(error){$('log').textContent='Log unavailable: '+error.message}}
$('cfg').addEventListener('input',()=>dirty=true);$('save').onclick=save;$('reload').onclick=()=>loadConfig(true);$('logs').onclick=logs;refresh();logs();setInterval(refresh,5000);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return hmac.compare_digest(supplied, TOKEN)
    def _json(self, obj, code=200):
        raw=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.end_headers(); self.wfile.write(raw)
    def _safe_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host", "")
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; object-src 'none'",
            )
            self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "no-referrer"); self.end_headers(); self.wfile.write(HTML.encode()); return
        if not self._auth() or not self._safe_origin(): self._json({"error":"unauthorized"},401); return
        path, _, query = self.path.partition("?")
        if path == "/api/health": self._json({"ok": True, "stale": state().get("stale", True)}); return
        if path == "/api/status":
            cfg = load_config()
            runtime = state()
            cameras = []
            for camera in cfg["cameras"]:
                cameras.append(
                    dict(
                        camera,
                        reachability=probe(camera),
                        send=runtime.get("camera_send", {}).get(camera["host"], {}),
                    )
                )
            self._json(
                {
                    "state": runtime,
                    "cameras": cameras,
                    "controller": runtime.get("controller", {}),
                    "controllers": joysticks(),
                    "hostname": socket.gethostname(),
                    "load": os.getloadavg(),
                    "uptime": (
                        time.time() - os.stat("/proc/1").st_ctime
                        if os.path.exists("/proc/1")
                        else None
                    ),
                }
            )
            return
        if path == "/api/config": self._json(load_config()); return
        if path == "/api/logs":
            params={k: v[0] for k, v in parse_qs(query).items()}
            try: requested = int(params.get("lines", "100"))
            except ValueError: requested = 100
            lines=min(max(requested,1),500); level=params.get("level", "")[:40]; search=params.get("search", "")[:200]
            argv=["journalctl","-u","ptzpad.service","-n",str(lines),"--no-pager"]; 
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
                out = result.stdout if result.returncode == 0 else (result.stderr or "journalctl failed")
            except (OSError, subprocess.TimeoutExpired): out="journalctl unavailable"
            if level: out="\n".join(x for x in out.splitlines() if level.lower() in x.lower())
            if search: out="\n".join(x for x in out.splitlines() if search.lower() in x.lower())
            self._json({"text":out[:100000]}); return
        self._json({"error":"not found"},404)
    def do_PUT(self):
        if not self._auth() or not self._safe_origin(): self._json({"error":"unauthorized"},401); return
        if self.path != "/api/config": self._json({"error":"not found"},404); return
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json": self._json({"error":"content type must be application/json"},415); return
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = MAX_BODY + 1
        if length <= 0 or length > MAX_BODY: self._json({"error":"invalid body length"},413); return
        try: value=json.loads(self.rfile.read(length)); save_config(value); self._json(load_config())
        except (ValueError, json.JSONDecodeError, OSError) as exc: self._json({"error":str(exc)},400)
    def log_message(self,*args): pass

def main():
    host=os.environ.get("PTZPAD_BIND","0.0.0.0"); port=int(os.environ.get("PTZPAD_PORT","8080")); ThreadingHTTPServer((host,port),Handler).serve_forever()
if __name__ == "__main__": main()
