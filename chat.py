"""本地聊天台：浏览器里直接和大模型协作，告别黑 PowerShell。

启动:  python chat.py   （自动打开浏览器 http://localhost:8765）
原理:  内置 HTTP 服务 + 内嵌聊天前端；发送消息时后台跑 `python cli.py ...`，
       输出流式回显到聊天气泡。与 CLI 完全同一条代码路径。

聊天语法（模式选 auto 时自动识别）:
  新项目 XXX: 需求...        -> 建项目并把需求分派给选中模型
  @deepseek @kimi 问题       -> 广播给指定模型
  其他文本                    -> 单发给"默认模型"（或选中项目则继续该项目）
"""
import json
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
PORT = 8765
MODELS = ["deepseek", "qwen", "yuanbao", "kimi", "doubao", "chatglm", "chatgpt", "claude"]

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def start_job(cmd: list[str]) -> str:
    jid = uuid.uuid4().hex[:8]
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "cli.py", *cmd],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    with LOCK:
        JOBS[jid] = {"proc": proc, "out": "", "done": False, "code": None}
    threading.Thread(target=_pump, args=(jid,), daemon=True).start()
    return jid


def _pump(jid: str):
    job = JOBS[jid]
    for line in job["proc"].stdout:
        with LOCK:
            job["out"] += line
    job["proc"].wait()
    with LOCK:
        job["done"] = True
        job["code"] = job["proc"].returncode


def parse_intent(text: str, mode: str, models: list[str], project: str) -> tuple[list[str], str]:
    """返回 (cli子命令, 描述)。auto 模式按语法猜。"""
    t = text.strip()
    ms = models or []
    # pipeline: 手动选模式；或 auto 模式下选了 >=2 模型 + 没选项目 + 文本不是"新项目"
    if mode == "pipeline" or (
        mode == "auto" and len(ms) >= 2 and not project
        and not t.startswith("新项目") and "@" not in t
    ):
        return (["pipeline", t, "--to", *ms], f"五阶段协作·{'/'.join(ms)}")
    if mode == "ask" or (mode == "auto" and ms and not project and not t.startswith("新项目")):
        return (["ask", ms[0], t], f"单问 {ms[0]}")
    if mode == "broadcast" or (mode == "auto" and "@" in t):
        if "@" in t:
            hits = [m for m in MODELS if f"@{m}" in t]
            body = t
            for h in hits:
                body = body.replace(f"@{h}", "").strip()
            if hits:
                return (["broadcast", body, "--to", *hits], f"广播 {'/'.join(hits)}")
        return (["broadcast", t, "--to", *ms], f"广播 {'/'.join(ms)}")
    if mode == "new" or (mode == "auto" and t.startswith("新项目")):
        body = t[3:].strip() if t.startswith("新项目") else t
        name, sep, req = body.partition(":")
        if not sep:
            name, sep, req = body.partition("：")
        return (["project", "new", name.strip() or "未命名", req.strip() or "无描述"], f"新项目 {name.strip()}")
    if mode == "project" or (mode == "auto" and project):
        if not project:
            return (["ask", (ms[0] if ms else "deepseek"), t], "（未选项目，改为单问）")
        if len(ms) >= 2:
            return (["project", "iterate", project, t, "--to", *ms], f"项目 {project} · 多模型迭代")
        m = ms[0] if ms else "deepseek"
        return (["project", "ask", project, m, t], f"项目 {project} · {m}")
    return (["ask", "deepseek", t], "单问 deepseek")


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>多模型协作台</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#2d333b;--fg:#e6edf3;--dim:#8b949e;--acc:#7c6cf0;--ok:#3fb950}
*{box-sizing:border-box;margin:0}
body{background:radial-gradient(1200px 600px at 70% -10%,#1b2333,var(--bg));color:var(--fg);
 font:14px/1.6 "Segoe UI",system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:15px;margin-right:8px;background:linear-gradient(90deg,#7c6cf0,#4cc2ff);
 -webkit-background-clip:text;background-clip:text;color:transparent}
select,.chip,button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
 border-radius:8px;padding:4px 10px;font-size:13px;cursor:pointer}
select:focus{outline:1px solid var(--acc)}
.chip{user-select:none}.chip.on{border-color:var(--acc);background:#2a2450}
#log{flex:1;overflow-y:auto;padding:18px 10%;display:flex;flex-direction:column;gap:12px}
.msg{max-width:82%;padding:10px 14px;border-radius:14px;white-space:pre-wrap;word-break:break-word}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,#4b3fd4,#7c6cf0);border-bottom-right-radius:4px}
.msg.bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);border-bottom-left-radius:4px}
.tag{font-size:11px;color:var(--dim);margin-bottom:4px}
.msg.bot .cursor{display:inline-block;width:8px;height:14px;background:var(--acc);animation:bl 1s steps(2) infinite;vertical-align:-2px}
@keyframes bl{50%{opacity:0}}
footer{border-top:1px solid var(--line);padding:12px 10%;display:flex;gap:10px;align-items:flex-end}
#in{flex:1;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:12px;
 padding:10px 14px;resize:none;font:inherit;max-height:140px}
#in:focus{outline:1px solid var(--acc)}
button.send{background:var(--acc);border:none;padding:10px 22px;border-radius:12px;font-weight:600}
button.send:disabled{opacity:.4}
button.stop{display:none;border-color:#f85149;color:#f85149}
.hint{padding:2px 10% 8px;color:var(--dim);font-size:12px}
</style></head><body>
<header><h1>⬡ 多模型协作台</h1>
<select id="project"><option value="">— 不用项目 —</option></select>
<button class="chip" onclick="loadProjects()">刷新</button>
<select id="mode">
 <option value="auto">自动识别</option><option value="ask">单问</option>
 <option value="broadcast">广播</option><option value="pipeline">流水线</option>
 <option value="new">新项目</option><option value="project">继续项目</option>
</select>
<span style="color:var(--dim)">参与模型:</span><span id="chips"></span></header>
<div id="log"></div>
<div class="hint">语法: 「新项目 名字: 需求」建项目 · 「@deepseek @kimi 问题」广播 · 项目下拉选中后发送即继续该项目 · Enter发送 / Shift+Enter换行</div>
<footer><textarea id="in" rows="1" placeholder="输入需求…"></textarea>
<button class="stop" id="stop" onclick="stopJob()">停止</button>
<button class="send" id="send" onclick="send()">发送</button></footer>
<script>
const MODELS=__MODELS__;let curJob=null,poll=null;
const chips=document.getElementById('chips');
MODELS.forEach((m,i)=>{const c=document.createElement('span');c.className='chip'+(i<3?' on':'');
 c.textContent=m;c.onclick=()=>c.classList.toggle('on');chips.appendChild(c);});
const onModels=()=>[...chips.children].filter(c=>c.classList.contains('on')).map(c=>c.textContent);
function add(role,text,tag){const d=document.createElement('div');d.className='msg '+role;
 if(tag)d.innerHTML='<div class="tag">'+tag+'</div>';d.appendChild(document.createTextNode(text||''));
 document.getElementById('log').appendChild(d);d.scrollIntoView({behavior:'smooth'});return d;}
async function loadProjects(){const r=await fetch('/projects');const j=await r.json();
 const s=document.getElementById('project');const cur=s.value;s.innerHTML='<option value="">— 不用项目 —</option>';
 j.forEach(p=>{const o=document.createElement('option');o.value=o.textContent=p;s.appendChild(o);});s.value=cur;}
loadProjects();
const ta=document.getElementById('in');
ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,140)+'px';});
async function send(){const t=ta.value.trim();if(!t||curJob)return;
 ta.value='';ta.style.height='auto';
 add('user',t);
 const body={text:t,mode:document.getElementById('mode').value,
   models:onModels(),project:document.getElementById('project').value};
 const r=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();curJob=j.job;
 const bubble=add('bot','','⚡ '+j.label);
 const stream=document.createElement('span');bubble.appendChild(stream);
 const cursor=document.createElement('span');cursor.className='cursor';bubble.appendChild(cursor);
 document.getElementById('stop').style.display='inline-block';
 let last='';
 poll=setInterval(async()=>{const q=await fetch('/poll?job='+curJob);const s=await q.json();
  if(s.out.length>last.length){stream.textContent=s.out.slice(last.length-  (last?0:0));
   stream.textContent=s.out;last=s.out;
   bubble.scrollIntoView({behavior:'smooth'});}
  if(s.done){clearInterval(poll);cursor.remove();curJob=null;
   document.getElementById('stop').style.display='none';
   const tag=bubble.querySelector('.tag');
   if(tag)tag.textContent+=' · '+(s.code===0?'✓':'✗ '+s.code);
   loadProjects();}},700);}
async function stopJob(){if(!curJob)return;await fetch('/stop?job='+curJob);}
</script></body></html>""".replace("__MODELS__", json.dumps(MODELS, ensure_ascii=False))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音访问日志
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"))
        elif self.path.startswith("/projects"):
            ps = sorted(p.name for p in (ROOT / "projects").glob("*") if p.is_dir()) if (ROOT / "projects").exists() else []
            self._send(json.dumps(ps, ensure_ascii=False).encode("utf-8"), "application/json")
        elif self.path.startswith("/poll"):
            jid = self.path.split("job=")[-1]
            with LOCK:
                job = JOBS.get(jid, {"out": "", "done": True, "code": -1})
                payload = {"out": job["out"], "done": job["done"], "code": job.get("code")}
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")
        elif self.path.startswith("/stop"):
            jid = self.path.split("job=")[-1]
            with LOCK:
                job = JOBS.get(jid)
            if job and not job["done"]:
                # 树杀：连同它启动的 Firefox 一起终止，避免 profile 被孤儿进程锁死
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(job["proc"].pid)],
                               capture_output=True)
            self._send(b"ok", "text/plain")

    def do_POST(self):
        if self.path == "/send":
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            cmd, label = parse_intent(data.get("text", ""), data.get("mode", "auto"),
                                      data.get("models", []), data.get("project", ""))
            jid = start_job(cmd)
            self._send(json.dumps({"job": jid, "label": label, "cmd": cmd}, ensure_ascii=False).encode("utf-8"), "application/json")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"协作台已启动: http://localhost:{PORT}  (Ctrl+C 退出)")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    srv.serve_forever()
