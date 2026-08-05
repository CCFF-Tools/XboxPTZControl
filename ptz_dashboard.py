#!/usr/bin/env python3
"""Small, dependency-free LAN dashboard for ptzpad."""
import hmac
import ipaddress
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import fcntl

from ptz_config import load_config, save_config, validate_camera

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


VISCA_VERSION = b"\x81\x09\x00\x02\xff"
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def parse_visca_version(response):
    """Extract standard VISCA version fields from a camera reply."""
    for offset in range(max(0, len(response) - 9)):
        frame = response[offset:]
        if len(frame) >= 11 and frame[0] & 0xF0 == 0x90 and frame[1] == 0x50:
            return {
                "vendor_id": frame[2:4].hex(),
                "model_id": frame[4:6].hex(),
                "rom_version": frame[6:8].hex(),
                "socket_number": frame[8:10].hex(),
            }
    return {}


def local_interface_details():
    """Return RFC 1918 IPv4 addresses and networks attached to this host."""
    details = set()
    for _, interface_name in socket.if_nameindex():
        request = struct.pack("256s", interface_name.encode()[:15])
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ioctl_socket:
                address_data = fcntl.ioctl(ioctl_socket, 0x8915, request)
                netmask_data = fcntl.ioctl(ioctl_socket, 0x891B, request)
            address = socket.inet_ntoa(address_data[20:24])
            netmask = socket.inet_ntoa(netmask_data[20:24])
            network = ipaddress.ip_interface(f"{address}/{netmask}").network
        except (OSError, ValueError):
            continue
        if any(network.subnet_of(private) for private in RFC1918_NETWORKS):
            details.add((ipaddress.ip_address(address), network))
    return sorted(details, key=lambda item: int(item[0]))


def local_interface_networks():
    """Return RFC 1918 IPv4 networks attached to this host."""
    networks = {network for _, network in local_interface_details()}
    return sorted(networks, key=lambda network: (int(network.network_address), network.prefixlen))


def require_local_camera(camera, networks=None):
    """Reject test targets outside a directly attached RFC 1918 network."""
    try:
        address = ipaddress.ip_address(camera["host"])
    except ValueError as exc:
        raise ValueError("camera testing requires a local IPv4 address") from exc
    attached = local_interface_networks() if networks is None else networks
    if address.version != 4 or not any(address in network for network in attached):
        raise ValueError("camera address is not on a directly attached private network")


def test_camera(cam, enforce_local=True):
    cam = validate_camera(cam)
    if enforce_local:
        require_local_camera(cam)
    started = time.monotonic()
    sock_type = socket.SOCK_DGRAM if cam["protocol"] == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.settimeout(0.5)
        if cam["protocol"] == "tcp":
            sock.connect((cam["host"], cam["port"]))
            sock.sendall(VISCA_VERSION)
        else:
            sock.sendto(VISCA_VERSION, (cam["host"], cam["port"]))
        try:
            response = sock.recv(1024)
        except socket.timeout:
            if cam["protocol"] == "udp":
                raise
            response = b""
    result = {
        "reachable": True,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "protocol": cam["protocol"],
        "response_hex": response.hex(),
        "version_supported": bool(response),
    }
    result.update(parse_visca_version(response))
    return result


_scan_lock = threading.Lock()
_last_scan = 0.0


def validate_discovery_subnet(subnet, networks=None):
    """Validate a small scan range against directly attached networks."""
    network = ipaddress.ip_network(subnet, strict=False)
    attached = local_interface_networks() if networks is None else networks
    allowed = network.version == 4 and any(
        network.subnet_of(local_network) for local_network in attached
    )
    if not allowed or network.prefixlen < 24:
        raise ValueError("subnet must be a directly attached private IPv4 /24 or narrower")
    return network


def discover_network(subnet, protocol, port):
    global _last_scan
    network = validate_discovery_subnet(subnet)
    if protocol not in ("tcp", "udp") or not 1 <= port <= 65535:
        raise ValueError("invalid protocol or port")
    if time.monotonic() - _last_scan < 5 or not _scan_lock.acquire(blocking=False):
        raise RuntimeError("scan already in progress")
    try:
        _last_scan = time.monotonic()
        found = []
        hosts = list(network.hosts())[:256]
        def scan(host):
            camera = {"host": str(host), "protocol": protocol, "port": port}
            try:
                result = test_camera(camera, enforce_local=False)
                return dict(
                    camera,
                    reachability="reachable",
                    response_hex=result.get("response_hex", ""),
                    vendor_id=result.get("vendor_id", ""),
                    model_id=result.get("model_id", ""),
                    rom_version=result.get("rom_version", ""),
                )
            except (OSError, TimeoutError, ValueError):
                return None

        with ThreadPoolExecutor(max_workers=32) as pool:
            for future in as_completed([pool.submit(scan, host) for host in hosts]):
                result = future.result()
                if result:
                    found.append(result)
        return sorted(found, key=lambda camera: ipaddress.ip_address(camera["host"]))
    finally:
        _scan_lock.release()

def joysticks():
    result = []
    for path in Path("/sys/class/input").glob("js*/device/name"):
        try: result.append({"name": path.read_text().strip(), "path": str(path.parent.parent)})
        except OSError: pass
    return result


def local_networks():
    suggestions = set()
    for address, attached_network in local_interface_details():
        prefix = max(24, attached_network.prefixlen)
        suggestions.add(str(ipaddress.ip_network(f"{address}/{prefix}", strict=False)))
    return sorted(suggestions)

HTML = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PTZPad status</title>
<style>
:root{color-scheme:dark;font:15px system-ui;background:#0b1020;color:#edf2f7}
body{max-width:1100px;margin:auto;padding:24px}h1{margin-bottom:4px}h2{font-size:17px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}
.card{background:#172033;border:1px solid #29354d;border-radius:14px;padding:18px;margin:14px 0}
.camera{display:grid;grid-template-columns:repeat(5,minmax(100px,1fr));gap:8px;padding:12px 0;border-bottom:1px solid #334155}.camera .actions,.camera .health,.camera .result{grid-column:1/-1}.ok{color:#63d6a0}.bad{color:#fb7185}
.muted{color:#9ca3af}.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
button,input,select,textarea{box-sizing:border-box;padding:9px;border-radius:7px;border:1px solid #45536d;background:#0f172a;color:white}
button{cursor:pointer;background:#2563eb}.danger{background:#9f1239}.secondary{background:#334155}label{color:#cbd5e1}label input,label select{display:block;width:100%;margin-top:4px}
pre{white-space:pre-wrap;max-height:380px;overflow:auto;background:#0b1020;padding:12px;border-radius:8px}
</style>
<body><h1>PTZPad</h1><div class="muted" id="msg">Enter the dashboard token.</div>
<div class="grid"><section class="card"><h2>Bridge</h2><div id="status">—</div></section>
<section class="card"><h2>Controllers</h2><div id="controller">—</div></section></div>
<section class="card"><h2>Stream Deck</h2><div id="streamdeck">—</div></section>
<section class="card"><h2>Cameras</h2><p class="muted">Add, reorder, test, and edit cameras. Tests send only the read-only VISCA version inquiry.</p><div id="cameras"></div>
<div class="controls"><button id="addCamera">Add camera</button><button id="save">Save changes</button><button class="secondary" id="reload">Discard edits</button></div></section>
<section class="card"><h2>Tuning</h2><p class="muted">Saved tuning values are editable below. Bridge live values are shown in the Bridge card and may differ briefly while settings reload.</p><div class="controls"><label>Saved maximum speed<input id="maxSpeed" type="number" min="1" max="24"></label><label>Saved deadzone<input id="deadzone" type="number" min="0" max="0.5" step="0.01"></label><label>Saved zoom speed<input id="zoomSpeed" type="number" min="0" max="7"></label><label>Stream Deck brightness<input id="deckBrightness" type="number" min="0" max="100"></label><label>Stream Deck enabled<input id="deckEnabled" type="checkbox"></label></div></section>
<section class="card"><h2>Discover cameras</h2><p class="muted">Scans at most one private /24 using bounded VISCA inquiries. No motion commands are sent.</p><div class="controls"><label>Subnet<input id="discoverSubnet" placeholder="192.168.1.0/24"></label><label>Protocol<select id="discoverProtocol"><option>tcp</option><option>udp</option></select></label><label>Port<input id="discoverPort" type="number" value="5678"></label><button id="discover">Discover</button></div><div id="discoverResults"></div></section>
<section class="card"><h2>Logs</h2><div class="controls"><label>Lines<br><input id="lines" type="number" min="1" max="500" value="100"></label>
<label>Level<br><select id="level"><option value="">All</option><option>ERROR</option><option>WARNING</option><option>INFO</option></select></label>
<label>Search<br><input id="search"></label><button id="logs">Refresh</button></div><pre id="log"></pre></section>
<script>
const $=id=>document.getElementById(id);
let token=sessionStorage.ptzToken||prompt('Dashboard token');
if(token)sessionStorage.ptzToken=token;
let dirty=false;
async function api(url,options={}){const response=await fetch(url,{...options,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'}});if(!response.ok)throw new Error(await response.text());return response.json()}
function text(tag,value,cls=''){const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;return node}
function field(label,key,value,type='text'){const wrap=document.createElement('label');wrap.textContent=label;const input=document.createElement('input');input.type=type;input.dataset.key=key;input.value=value??'';input.oninput=()=>dirty=true;wrap.append(input);return wrap}
function cameraFromRow(row){const get=key=>row.querySelector('[data-key="'+key+'"]').value;return{name:get('name'),model:get('model'),host:get('host'),protocol:get('protocol'),port:Number(get('port'))}}
function cameraRow(camera){const row=document.createElement('div');row.className='camera';row.append(field('Name','name',camera.name),field('Model','model',camera.model),field('IP / host','host',camera.host));
const protocol=document.createElement('select');protocol.dataset.key='protocol';for(const value of ['tcp','udp']){const option=document.createElement('option');option.value=value;option.textContent=value.toUpperCase();protocol.append(option)}protocol.value=camera.protocol||'tcp';protocol.onchange=()=>dirty=true;const protocolLabel=document.createElement('label');protocolLabel.textContent='Protocol';protocolLabel.append(protocol);row.append(protocolLabel,field('Port','port',camera.port||5678,'number'));
const actions=document.createElement('div');actions.className='actions controls';const health=text('span','Status unknown','health muted');const result=text('span','Not tested','result muted');
function button(label,action,cls='secondary'){const node=document.createElement('button');node.textContent=label;node.className=cls;node.onclick=action;return node}
actions.append(button('Test',async()=>{result.textContent='Testing…';try{const value=await api('/api/cameras/test',{method:'POST',body:JSON.stringify(cameraFromRow(row))});result.textContent='Reachable in '+value.latency_ms+' ms'+(value.model_id?' • model ID '+value.model_id:' • version inquiry unsupported');result.className='result ok'}catch(error){result.textContent='Test failed: '+error.message;result.className='result bad'}}));
actions.append(button('Up',()=>{const previous=row.previousElementSibling;if(previous){row.parentNode.insertBefore(row,previous);dirty=true}}));
actions.append(button('Down',()=>{const next=row.nextElementSibling;if(next){row.parentNode.insertBefore(next,row);dirty=true}}));
actions.append(button('Remove',()=>{row.remove();dirty=true},'danger'));row.append(actions,health,result);return row}
function addCamera(camera={name:'New camera',model:'',host:'',protocol:'tcp',port:5678}){$('cameras').append(cameraRow(camera));dirty=true}
function renderConfig(config){$('cameras').replaceChildren(...config.cameras.map(cameraRow));$('maxSpeed').value=config.max_speed;$('deadzone').value=config.deadzone;$('zoomSpeed').value=config.zoom_speed;$('deckBrightness').value=config.streamdeck?.brightness??35;$('deckEnabled').checked=config.streamdeck?.enabled??true;dirty=false}
function buildConfig(){return{cameras:[...$('cameras').children].map(cameraFromRow),max_speed:Number($('maxSpeed').value),deadzone:Number($('deadzone').value),zoom_speed:Number($('zoomSpeed').value),streamdeck:{enabled:$('deckEnabled').checked,brightness:Number($('deckBrightness').value)}}}
function renderControllers(data){const items=[];if(data.state.controller?.connected)items.push('Active: '+data.state.controller.name+(data.state.controller.wireless?' (wireless)':''));for(const pad of data.controllers)items.push(pad.name);$('controller').replaceChildren(...(items.length?items:['No controller connected']).map(value=>text('div',value)));const d=data.state.streamdeck||{};const deckClass=!d.enabled?'muted':d.connected?'ok':'bad';const library=d.library_available==null?'unknown':d.library_available?'available':'unavailable';$('streamdeck').replaceChildren(text('div',(d.enabled?'Enabled':'Disabled')+' • '+(d.connected?'Connected':'Disconnected'),deckClass),text('div','Library '+library+' • Device '+(d.device||'—')+' • keys '+(d.key_count||0)+' • brightness '+(d.brightness??'—')),text('div','Last render '+(d.last_render_at?new Date(d.last_render_at*1000).toLocaleString():'—')+' • last event '+(d.last_event_at?new Date(d.last_event_at*1000).toLocaleString():'—')),text('div','Camera '+(d.camera_name||'—')+' • save armed '+(d.save_armed?'yes':'no')),text('div','Last error '+(d.last_error||'none'),d.last_error?'bad':'ok'))}
async function loadConfig(force=false){if(dirty&&!force)return;renderConfig(await api('/api/config'))}
async function refresh(){try{const data=await api('/api/status');const state=data.state;const input=state.input||{};const direction=input.zoom_direction??0;const protocol=input.protocol||'unknown';const triggerLine=input.lt==null?'Triggers unavailable':'Triggers LT '+input.lt+' RT '+input.rt+' • zoom direction '+direction+' (0 = commanded stop) • '+protocol.toUpperCase();const uptime=data.uptime==null?'unknown':Math.floor(data.uptime/3600)+'h';$('status').replaceChildren(text('div',data.hostname+' • '+(state.stale?'offline/stale':'online'),state.stale?'bad':'ok'),text('div','Host uptime '+uptime+' • load '+data.load.map(v=>v.toFixed(2)).join(' / ')),text('div','Live speed '+state.max_speed+' • live deadzone '+state.deadzone+' • live zoom '+state.zoom_speed),text('div',triggerLine,'muted'));renderControllers(data);if(!$('discoverSubnet').value&&data.local_networks.length)$('discoverSubnet').value=data.local_networks[0];await loadConfig();if(!dirty){[...$('cameras').children].forEach((row,index)=>{const value=data.cameras[index]?.reachability||'unknown';const health=row.querySelector('.health');health.textContent='Automatic status: '+value;health.className='health '+(value==='reachable'?'ok':value==='unreachable'?'bad':'muted')})}$('msg').textContent=dirty?'Connected • unsaved changes':'Connected'}catch(error){$('msg').textContent='Authentication or service error: '+error.message}}
async function save(){try{const saved=await api('/api/config',{method:'PUT',body:JSON.stringify(buildConfig())});renderConfig(saved);$('msg').textContent='Configuration saved'}catch(error){$('msg').textContent='Configuration rejected: '+error.message}}
async function logs(){try{const query=new URLSearchParams({lines:$('lines').value,level:$('level').value,search:$('search').value});$('log').textContent=(await api('/api/logs?'+query)).text}catch(error){$('log').textContent='Log unavailable: '+error.message}}
function renderDiscovery(results){const nodes=results.map(camera=>{const row=document.createElement('div');row.className='camera';row.append(text('div',camera.host+':'+camera.port+' • '+camera.protocol.toUpperCase()+(camera.model_id?' • model ID '+camera.model_id:'')));const add=document.createElement('button');add.textContent='Add camera';add.onclick=()=>addCamera({name:'Camera '+camera.host,model:camera.model_id||'',host:camera.host,protocol:camera.protocol,port:camera.port});row.append(add);return row});$('discoverResults').replaceChildren(text('p','Found '+results.length+' camera(s)'),...nodes)}
async function discover(){const button=$('discover');button.disabled=true;$('discoverResults').textContent='Scanning…';try{const result=await api('/api/cameras/discover',{method:'POST',body:JSON.stringify({subnet:$('discoverSubnet').value,protocol:$('discoverProtocol').value,port:Number($('discoverPort').value)})});renderDiscovery(result.results)}catch(error){$('discoverResults').textContent='Discovery failed: '+error.message}finally{button.disabled=false}}
for(const id of ['maxSpeed','deadzone','zoomSpeed','deckBrightness','deckEnabled'])$(id).oninput=()=>dirty=true;$('save').onclick=save;$('reload').onclick=()=>loadConfig(true);$('logs').onclick=logs;$('addCamera').onclick=()=>addCamera();$('discover').onclick=discover;refresh();logs();setInterval(refresh,5000);
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
                    "local_networks": local_networks(),
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
    def do_POST(self):
        if not self._auth() or not self._safe_origin():
            self._json({"error": "unauthorized"}, 401); return
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            self._json({"error": "content type must be application/json"}, 415); return
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = -1
        if length <= 0 or length > MAX_BODY:
            self._json({"error": "invalid body length"}, 413); return
        try: body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "invalid JSON"}, 400); return
        try:
            if self.path == "/api/cameras/test":
                self._json(test_camera(body)); return
            if self.path == "/api/cameras/discover":
                subnet = body.get("subnet", "")
                protocol = body.get("protocol", "tcp")
                port = int(body.get("port", 5678 if protocol == "tcp" else 1259))
                self._json({"results": discover_network(subnet, protocol, port)}); return
        except (ValueError, OSError, TimeoutError, RuntimeError) as exc:
            self._json({"error": str(exc)}, 400); return
        self._json({"error": "not found"}, 404)
    def log_message(self,*args): pass

def main():
    host=os.environ.get("PTZPAD_BIND","0.0.0.0"); port=int(os.environ.get("PTZPAD_PORT","8080")); ThreadingHTTPServer((host,port),Handler).serve_forever()
if __name__ == "__main__": main()
