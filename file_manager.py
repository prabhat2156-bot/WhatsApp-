import os, time, json, shutil, mimetypes, zipfile, io, secrets, stat
from pathlib import Path
from flask import (
    Flask, request, jsonify, send_file, abort, Response,
    render_template_string,
)

# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(32)

# Shared token store (bot.py populates this)
token_store: dict = {}

HIDDEN_NAMES = {
    "venv", "__pycache__", ".git", "node_modules",
    "output.log", ".env.bak",
}

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def is_hidden(name: str) -> bool:
    if name.startswith("."):
        return True
    if name in HIDDEN_NAMES:
        return True
    if name.endswith(".pyc"):
        return True
    return False

def validate_token(token: str):
    data = token_store.get(token)
    if not data:
        return None
    if time.time() > data["expires_at"]:
        token_store.pop(token, None)
        return None
    return data

def safe_path(base: str, rel: str):
    base = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, (rel or "").lstrip("/")))
    if not (target == base or target.startswith(base + os.sep)):
        return None
    return target

def get_token_data(token: str):
    data = validate_token(token)
    if not data:
        abort(401, "Session expired")
    return data

def human_size(n: int) -> str:
    if n is None: return ""
    for u in ("B","KB","MB","GB","TB"):
        if n < 1024: return f"{n:.0f} {u}" if u=="B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

# ─────────────────────────────────────────────────────────────
# Landing
# ─────────────────────────────────────────────────────────────
LANDING_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>God Madara Hosting</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d0f14;color:#c9d1d9;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{text-align:center;padding:3rem 2rem}
h1{font-size:1.8rem;font-weight:700;color:#e2e8f0;margin-bottom:.5rem}
p{color:#8896a9;font-size:.95rem}
.status{display:inline-flex;align-items:center;gap:.5rem;background:#161b22;
border:1px solid #252d3a;border-radius:2rem;padding:.5rem 1.5rem;margin-top:1.5rem}
.dot{width:8px;height:8px;background:#3b82f6;border-radius:50%;animation:p 2s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}
a{color:#3b82f6;display:block;margin-top:1.5rem;font-size:.9rem;text-decoration:none}
a:hover{text-decoration:underline}
</style></head><body>
<div class="card">
<h1>God Madara Hosting</h1>
<p>Advanced Mobile File Manager</p>
<div class="status"><span class="dot"></span> Online</div>
<a href="/fm/dev/">Open file manager →</a>
</div></body></html>"""

@app.route("/")
def index():
    return render_template_string(LANDING_HTML)

@app.route("/health")
def health():
    return jsonify({"status":"ok"})

# ─────────────────────────────────────────────────────────────
# File Manager UI
# ─────────────────────────────────────────────────────────────
FM_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#0d0f14">
<title>Files — God Madara</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0d0f14;
  --surface:#13161c;
  --surface2:#1a1d24;
  --card:#181b22;
  --accent:#3b82f6;
  --accent2:#2563eb;
  --text:#e2e8f0;
  --text2:#8896a9;
  --muted:#4a5568;
  --green:#4ade80;
  --yellow:#fbbf24;
  --red:#f87171;
  --blue:#3b82f6;
  --border:rgba(255,255,255,0.07);
  --radius:14px;
  --radius-sm:9px;
  --bar-h:56px;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
font-family:'Segoe UI',system-ui,-apple-system,sans-serif;font-size:15px}

/* Ripple */
.ripple-host{position:relative;overflow:hidden}
.ripple{position:absolute;border-radius:50%;background:rgba(255,255,255,0.1);
transform:scale(0);animation:ra .5s linear;pointer-events:none}
@keyframes ra{to{transform:scale(4);opacity:0}}

/* Shell */
#app{width:100%;height:100%;position:relative;overflow:hidden}
.screen{position:absolute;inset:0;display:flex;flex-direction:column;
transition:transform .3s cubic-bezier(.4,0,.2,1)}
#screen-list{transform:translateX(0)}
#screen-editor{transform:translateX(100%)}
#screen-preview{transform:translateX(100%)}
#app.editor-open #screen-list{transform:translateX(-100%)}
#app.editor-open #screen-editor{transform:translateX(0)}
#app.preview-open #screen-list{transform:translateX(-100%)}
#app.preview-open #screen-preview{transform:translateX(0)}

/* App bar */
.app-bar{height:var(--bar-h);background:var(--surface);display:flex;
align-items:center;gap:4px;padding:0 6px;flex-shrink:0;
border-bottom:1px solid var(--border);z-index:10}
.app-bar-title{font-size:.95rem;font-weight:600;flex:1;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:6px;
color:var(--text)}
.logo-emoji{font-size:1.1rem;padding-left:6px;opacity:.7}
.icon-btn{width:38px;height:38px;border:none;background:transparent;
color:var(--text2);border-radius:8px;cursor:pointer;display:flex;
align-items:center;justify-content:center;font-size:1rem;flex-shrink:0;
transition:background .15s,color .15s}
.icon-btn:hover,.icon-btn:active{background:var(--surface2);color:var(--text)}
.icon-btn.active{color:var(--accent)}

/* Timer */
#timer-wrap{position:relative;width:34px;height:34px;flex-shrink:0}
#timer-svg{position:absolute;inset:0;transform:rotate(-90deg)}
#timer-ring-bg{fill:none;stroke:var(--border);stroke-width:3}
#timer-ring{fill:none;stroke:var(--text2);stroke-width:3;stroke-linecap:round;
transition:stroke-dashoffset .9s linear,stroke .5s}
#timer-text{position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;font-size:.52rem;font-weight:600;color:var(--text2)}

/* Selection bar */
.app-bar.selecting{background:var(--surface2)}
.app-bar.selecting .icon-btn{color:var(--text2)}
.app-bar.selecting .app-bar-title{color:var(--text)}

/* Search */
#search-wrap{padding:8px 12px;background:var(--surface);
border-bottom:1px solid var(--border);flex-shrink:0;display:none}
#search-wrap.open{display:block}
#search-input{width:100%;background:var(--bg);border:1px solid var(--border);
border-radius:8px;padding:9px 14px;color:var(--text);font-size:.88rem;
outline:none;transition:border-color .2s}
#search-input:focus{border-color:rgba(59,130,246,0.5)}
#search-input::placeholder{color:var(--muted)}

/* Breadcrumb */
#breadcrumb-wrap{display:flex;align-items:center;gap:4px;padding:7px 12px;
overflow-x:auto;flex-shrink:0;scrollbar-width:none;
-webkit-overflow-scrolling:touch;background:var(--surface);
border-bottom:1px solid var(--border)}
#breadcrumb-wrap::-webkit-scrollbar{display:none}
.bc-chip{padding:4px 10px;font-size:.76rem;color:var(--text2);cursor:pointer;
white-space:nowrap;flex-shrink:0;transition:color .15s;border-radius:6px}
.bc-chip:active{background:var(--surface2)}
.bc-chip.current{color:var(--accent);font-weight:500}
.bc-sep{color:var(--muted);font-size:.65rem;flex-shrink:0;opacity:.6}

/* View toolbar */
#view-toolbar{display:flex;align-items:center;justify-content:space-between;
padding:5px 12px;flex-shrink:0;background:var(--surface);
border-bottom:1px solid var(--border)}
.view-toggle{display:flex;gap:2px}
.view-btn{width:28px;height:28px;border:none;background:transparent;
color:var(--muted);border-radius:6px;cursor:pointer;font-size:.88rem;
display:flex;align-items:center;justify-content:center;transition:all .15s}
.view-btn.active{background:var(--surface2);color:var(--text)}
#item-count{font-size:.75rem;color:var(--muted)}

/* Paste banner */
#paste-banner{display:none;align-items:center;gap:10px;padding:9px 14px;
background:var(--surface2);border-bottom:1px solid var(--border);
font-size:.8rem;color:var(--text2);flex-shrink:0}
#paste-banner.show{display:flex}
#paste-banner b{color:var(--text)}
#paste-banner button{margin-left:auto;background:var(--accent);border:none;
color:#fff;border-radius:7px;padding:5px 12px;font-size:.78rem;
font-weight:600;cursor:pointer}
#paste-banner .ghost{background:transparent;color:var(--text2);margin-left:0}

/* File list */
#file-list-wrap{flex:1;overflow-y:auto;padding:0 0 90px;
-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
#file-list-wrap::-webkit-scrollbar{width:2px}
#file-list-wrap::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

.file-item{display:flex;align-items:center;gap:12px;padding:10px 14px;
min-height:62px;cursor:pointer;border-bottom:1px solid var(--border);
transition:background .12s;animation:fs .2s both}
.file-item:active{background:rgba(255,255,255,0.03)}
.file-item.selected{background:rgba(59,130,246,0.1)}
.file-item .checkbox{width:20px;height:20px;border-radius:50%;
border:1.5px solid var(--muted);flex-shrink:0;display:none;
align-items:center;justify-content:center;color:#fff;font-size:.65rem}
#app.selecting .file-item .checkbox{display:flex}
.file-item.selected .checkbox{background:var(--accent);border-color:var(--accent)}

/* All icon backgrounds are the same neutral tone */
.fi-icon-wrap{width:40px;height:40px;border-radius:10px;display:flex;
align-items:center;justify-content:center;font-size:1.3rem;flex-shrink:0;
background:rgba(255,255,255,0.06)}
.fi-icon-wrap.folder,
.fi-icon-wrap.py,
.fi-icon-wrap.js,
.fi-icon-wrap.html,
.fi-icon-wrap.css,
.fi-icon-wrap.json,
.fi-icon-wrap.md,
.fi-icon-wrap.img,
.fi-icon-wrap.media,
.fi-icon-wrap.code,
.fi-icon-wrap.archive,
.fi-icon-wrap.generic{background:rgba(255,255,255,0.06)}

.fi-info{flex:1;min-width:0}
.fi-name{font-size:.9rem;font-weight:500;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis;color:var(--text)}
.fi-meta{font-size:.72rem;color:var(--muted);margin-top:2px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fi-right{display:flex;flex-direction:column;align-items:flex-end;
gap:2px;flex-shrink:0}
.fi-size{font-size:.72rem;color:var(--text2)}
.fi-more{width:32px;height:32px;border:none;background:transparent;
color:var(--muted);border-radius:7px;cursor:pointer;font-size:1.1rem;
display:flex;align-items:center;justify-content:center}
.fi-more:active{background:var(--surface2);color:var(--text)}

/* Grid view */
#file-list-wrap.grid-view{display:grid;
grid-template-columns:repeat(auto-fill,minmax(90px,1fr));
gap:7px;padding:10px 10px 90px;align-content:start}
#file-list-wrap.grid-view .file-item{flex-direction:column;justify-content:center;
gap:6px;min-height:100px;padding:10px 6px;border:1px solid var(--border);
border-radius:var(--radius-sm);background:var(--card);text-align:center}
#file-list-wrap.grid-view .fi-icon-wrap{width:44px;height:44px;font-size:1.5rem}
#file-list-wrap.grid-view .fi-info{width:100%}
#file-list-wrap.grid-view .fi-name{font-size:.75rem;text-align:center;
white-space:nowrap}
#file-list-wrap.grid-view .fi-meta{display:none}
#file-list-wrap.grid-view .fi-right{display:none}
#file-list-wrap.grid-view .checkbox{position:absolute;top:5px;right:5px}

@keyframes fs{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.empty-state{display:flex;flex-direction:column;align-items:center;
justify-content:center;padding:60px 20px;gap:10px;color:var(--muted)}
.empty-state .es-icon{font-size:2.5rem;opacity:.5}
.empty-state p{font-size:.88rem}

/* FAB */
#fab-wrap{position:absolute;bottom:22px;right:16px;display:flex;
flex-direction:column-reverse;align-items:flex-end;gap:10px;z-index:400}
.fab-main{width:52px;height:52px;border-radius:50%;background:var(--accent);
border:none;color:#fff;font-size:1.5rem;cursor:pointer;
box-shadow:0 4px 16px rgba(0,0,0,.4);transition:transform .2s,background .2s}
.fab-main:active{transform:scale(.93)}
#fab-wrap.open .fab-main{transform:rotate(45deg);background:var(--accent2)}
.fab-mini-group{display:flex;flex-direction:column;gap:8px;align-items:flex-end;
transform-origin:bottom right;opacity:0;transform:scale(.6) translateY(16px);
pointer-events:none;transition:all .2s cubic-bezier(.4,0,.2,1)}
#fab-wrap.open .fab-mini-group{opacity:1;transform:none;pointer-events:auto}
.fab-mini-row{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.fab-mini-label{background:var(--surface);border:1px solid var(--border);
border-radius:7px;padding:5px 11px;font-size:.8rem;color:var(--text2);
white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.fab-mini{width:40px;height:40px;border-radius:50%;border:none;
background:var(--surface2);color:var(--text2);
cursor:pointer;font-size:1rem;display:flex;align-items:center;
justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.fab-mini:active{transform:scale(.9)}
.fab-mini.new-file{background:var(--surface2)}
.fab-mini.new-folder{background:var(--surface2)}
.fab-mini.upload{background:var(--surface2)}
#fab-backdrop{position:absolute;inset:0;z-index:399;
background:rgba(13,15,20,.7);backdrop-filter:blur(2px);display:none}
#fab-wrap.open ~ #fab-backdrop{display:block}

/* Bottom sheet */
#sheet-overlay,#input-sheet-overlay,#confirm-overlay,#props-overlay,#sort-overlay,#move-overlay{
position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:450;display:none;
animation:oi .2s}
@keyframes oi{from{opacity:0}to{opacity:1}}
#bottom-sheet,#input-sheet,#confirm-sheet,#props-sheet,#sort-sheet,#move-sheet{
position:fixed;bottom:0;left:0;right:0;background:var(--surface);
border-radius:18px 18px 0 0;z-index:500;transform:translateY(100%);
transition:transform .28s cubic-bezier(.4,0,.2,1);
max-height:85vh;overflow-y:auto;
padding-bottom:env(safe-area-inset-bottom,16px)}
#bottom-sheet.open,#input-sheet.open,#confirm-sheet.open,
#props-sheet.open,#sort-sheet.open,#move-sheet.open{transform:translateY(0)}
.sheet-handle{width:32px;height:3px;background:rgba(255,255,255,0.15);
border-radius:2px;margin:10px auto 4px}
.sheet-header{display:flex;align-items:center;gap:12px;padding:10px 16px 8px;
border-bottom:1px solid var(--border)}
.sheet-file-icon{width:42px;height:42px;border-radius:10px;font-size:1.4rem;
display:flex;align-items:center;justify-content:center;
background:rgba(255,255,255,0.06)}
.sheet-file-info{flex:1;min-width:0}
.sheet-file-name{font-size:.95rem;font-weight:600;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.sheet-file-meta{font-size:.75rem;color:var(--muted);margin-top:2px}
.sheet-actions{padding:4px 0 10px}
.sheet-action{display:flex;align-items:center;gap:14px;padding:13px 18px;
cursor:pointer;font-size:.9rem;transition:background .1s;color:var(--text)}
.sheet-action:active{background:rgba(255,255,255,.04)}
.sheet-action .sa-icon{font-size:1.1rem;width:26px;text-align:center;opacity:.7}
.sheet-action.danger{color:var(--red)}
.sheet-action.disabled{opacity:.3;pointer-events:none}
.sheet-divider{height:1px;background:var(--border);margin:4px 0}

/* Input sheet */
#input-sheet,#confirm-sheet,#props-sheet,#sort-sheet,#move-sheet{
padding:16px 18px calc(env(safe-area-inset-bottom,16px) + 16px);z-index:601}
#input-sheet-overlay,#confirm-overlay,#props-overlay,#sort-overlay,#move-overlay{z-index:600}
.sheet-title{font-size:1rem;font-weight:700;margin-bottom:14px;padding-top:4px;
color:var(--text)}
#input-sheet input,#confirm-sheet input{width:100%;background:var(--bg);
border:1px solid var(--border);border-radius:9px;padding:11px 14px;
color:var(--text);font-size:.95rem;outline:none;margin-bottom:14px;
transition:border-color .2s}
#input-sheet input:focus{border-color:rgba(59,130,246,0.5)}
#input-sheet input::placeholder{color:var(--muted)}
.input-sheet-btns{display:flex;gap:8px}
.btn-sheet{flex:1;padding:12px;border:none;border-radius:10px;font-size:.9rem;
font-weight:600;cursor:pointer;transition:opacity .15s}
.btn-sheet:active{opacity:.8}
.btn-cancel{background:var(--surface2);color:var(--text2)}
.btn-confirm{background:var(--accent);color:#fff}
.btn-danger{background:var(--red);color:#fff}
.confirm-msg{color:var(--text2);font-size:.88rem;margin-bottom:16px;line-height:1.5}

/* Properties */
.props-row{display:flex;justify-content:space-between;padding:9px 0;
border-bottom:1px solid var(--border);font-size:.86rem}
.props-row:last-child{border-bottom:none}
.props-key{color:var(--muted)}
.props-val{color:var(--text);font-family:monospace;text-align:right;
word-break:break-all;max-width:60%;font-size:.82rem}

/* Sort options */
.sort-opt{display:flex;align-items:center;gap:12px;padding:12px 4px;
font-size:.9rem;cursor:pointer;border-radius:8px;color:var(--text2)}
.sort-opt:active{background:var(--card)}
.sort-opt.active{color:var(--text);font-weight:600}
.sort-check{margin-left:auto;color:var(--accent);font-size:.8rem}

/* Move folder picker */
#move-list{max-height:50vh;overflow-y:auto;margin:6px -2px;padding:0 2px}
.move-item{display:flex;align-items:center;gap:12px;padding:10px 6px;
cursor:pointer;border-radius:8px;font-size:.9rem;color:var(--text2)}
.move-item:active{background:var(--card)}
.move-item .mv-icon{font-size:1.05rem;opacity:.7}

/* Drag overlay */
#drag-overlay{position:fixed;inset:0;background:rgba(13,15,20,.9);
z-index:800;display:none;align-items:center;justify-content:center;
flex-direction:column;gap:14px;border:2px dashed rgba(255,255,255,0.15)}
#drag-overlay.active{display:flex}
#drag-overlay .dnd-icon{font-size:3rem;opacity:.6}
#drag-overlay p{font-size:1.1rem;color:var(--text2)}

/* Toast */
#toast{position:fixed;bottom:86px;left:50%;
transform:translateX(-50%) translateY(50px);background:var(--surface);
border:1px solid var(--border);border-radius:20px;padding:9px 18px;
font-size:.84rem;z-index:9999;opacity:0;transition:all .28s cubic-bezier(.4,0,.2,1);
white-space:nowrap;max-width:calc(100vw - 40px);pointer-events:none;
box-shadow:0 4px 16px rgba(0,0,0,.5);color:var(--text2)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.success{border-color:rgba(74,222,128,.3);color:var(--green)}
#toast.error{border-color:rgba(248,113,113,.3);color:var(--red)}
#toast.info{border-color:rgba(59,130,246,.3);color:var(--blue)}

/* Expired */
#expired-screen{position:fixed;inset:0;background:var(--bg);z-index:9999;
display:none;flex-direction:column;align-items:center;justify-content:center;
gap:14px;text-align:center;padding:20px}
#expired-screen.show{display:flex}
#expired-screen .exp-icon{font-size:3rem;opacity:.6}
#expired-screen h2{font-size:1.4rem;color:var(--text)}
#expired-screen p{color:var(--muted);font-size:.9rem;line-height:1.6}

/* Editor */
#editor-bar,#preview-bar{height:var(--bar-h);background:var(--surface);
display:flex;align-items:center;gap:4px;padding:0 6px;flex-shrink:0;
border-bottom:1px solid var(--border)}
#editor-filename,#preview-filename{flex:1;font-size:.88rem;font-weight:600;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:6px}
#editor-filename.modified::after{content:" •";color:var(--yellow)}
.btn-save{background:var(--accent);border:none;color:#fff;border-radius:8px;
padding:7px 14px;font-size:.82rem;font-weight:700;cursor:pointer}
.btn-save:disabled{opacity:.35;cursor:default}
.editor-wrap{flex:1;overflow:hidden;position:relative}
.CodeMirror{height:100%!important;font-size:13px;line-height:1.6;
background:#111318!important;font-family:'Fira Code','Consolas',monospace}

/* Preview */
.preview-wrap{flex:1;overflow:auto;display:flex;align-items:center;
justify-content:center;background:#000;padding:10px}
.preview-wrap img,.preview-wrap video{max-width:100%;max-height:100%;
object-fit:contain;border-radius:8px}
.preview-wrap audio{width:90%;max-width:500px}
.preview-wrap iframe{width:100%;height:100%;border:0;background:#fff;border-radius:8px}
.preview-wrap.text{background:var(--bg);align-items:flex-start;
justify-content:flex-start;padding:16px}
.preview-wrap.text pre{font-family:monospace;font-size:.84rem;color:var(--text2);
white-space:pre-wrap;word-break:break-all}

/* Keyboard toolbar */
#kbd-toolbar{background:var(--surface);border-top:1px solid var(--border);
padding:5px 7px;display:flex;gap:5px;overflow-x:auto;flex-shrink:0;
scrollbar-width:none;-webkit-overflow-scrolling:touch}
#kbd-toolbar::-webkit-scrollbar{display:none}
.kbd-btn{background:var(--card);border:1px solid var(--border);
color:var(--text2);border-radius:7px;padding:6px 11px;font-size:.8rem;
font-family:monospace;cursor:pointer;white-space:nowrap;flex-shrink:0}
.kbd-btn:active{background:var(--surface2);color:var(--text)}

#file-upload-input{display:none}

/* Upload progress */
#upload-progress-wrap{position:fixed;bottom:86px;left:12px;right:12px;
z-index:700;display:flex;flex-direction:column;gap:5px;pointer-events:none}
.upload-prog-item{background:var(--surface);border:1px solid var(--border);
border-radius:9px;padding:9px 12px;font-size:.8rem;color:var(--text2)}
.upload-prog-bar-wrap{height:2px;background:var(--border);
border-radius:2px;margin-top:6px}
.upload-prog-bar{height:2px;background:var(--accent);border-radius:2px;
transition:width .2s;width:0}

/* Loader */
#loader{position:absolute;top:0;left:0;height:2px;background:var(--accent);
width:0;z-index:50;transition:width .3s;opacity:.8}
#loader.loading{width:90%}
#loader.done{width:100%;opacity:0;transition:opacity .3s .15s}
</style>
</head>
<body>

<div id="expired-screen">
  <div class="exp-icon">⏰</div>
  <h2>Session Expired</h2>
  <p>Your file manager session has expired.<br>Request a new link from the Telegram bot.</p>
</div>

<div id="app">

  <!-- LIST SCREEN -->
  <div id="screen-list" class="screen">
    <div class="app-bar" id="main-bar">
      <span class="logo-emoji">◆</span>
      <span class="app-bar-title" id="bar-title">Files</span>
      <button class="icon-btn" id="btn-search" title="Search">🔍</button>
      <button class="icon-btn" id="btn-sort" title="Sort">⇅</button>
      <button class="icon-btn" id="btn-hidden" title="Toggle hidden">👁</button>
      <button class="icon-btn" id="btn-refresh" title="Refresh">↻</button>
      <div id="timer-wrap" title="Session time remaining">
        <svg id="timer-svg" viewBox="0 0 38 38" width="34" height="34">
          <circle id="timer-ring-bg" cx="19" cy="19" r="16"/>
          <circle id="timer-ring" cx="19" cy="19" r="16"
            stroke-dasharray="100.53" stroke-dashoffset="0"/>
        </svg>
        <div id="timer-text">--:--</div>
      </div>
    </div>

    <div class="app-bar" id="select-bar" style="display:none">
      <button class="icon-btn" id="btn-cancel-select" title="Cancel">✕</button>
      <span class="app-bar-title" id="select-count">0 selected</span>
      <button class="icon-btn" id="btn-select-all" title="Select all">☑</button>
      <button class="icon-btn" id="btn-bulk-cut" title="Cut">✂</button>
      <button class="icon-btn" id="btn-bulk-copy" title="Copy">⎘</button>
      <button class="icon-btn" id="btn-bulk-download" title="Download">⬇</button>
      <button class="icon-btn" id="btn-bulk-delete" title="Delete">🗑</button>
    </div>

    <div id="loader"></div>

    <div id="search-wrap">
      <input id="search-input" type="text" placeholder="Search in current folder…">
    </div>

    <div id="breadcrumb-wrap"></div>

    <div id="paste-banner">
      <span><b id="paste-mode-label">Cut</b> <span id="paste-count-label">1 item</span></span>
      <button id="btn-paste">Paste here</button>
      <button class="ghost" id="btn-paste-cancel">Cancel</button>
    </div>

    <div id="view-toolbar">
      <span id="item-count"></span>
      <div class="view-toggle">
        <button class="view-btn active" id="btn-list-view" title="List view">☰</button>
        <button class="view-btn" id="btn-grid-view" title="Grid view">⊞</button>
      </div>
    </div>

    <div id="file-list-wrap"></div>

    <div id="fab-wrap">
      <button class="fab-main ripple-host" id="fab-main-btn" aria-label="Actions">＋</button>
      <div class="fab-mini-group">
        <div class="fab-mini-row">
          <span class="fab-mini-label">Upload files</span>
          <button class="fab-mini upload ripple-host" id="fab-upload">⬆</button>
        </div>
        <div class="fab-mini-row">
          <span class="fab-mini-label">New folder</span>
          <button class="fab-mini new-folder ripple-host" id="fab-newfolder">📁</button>
        </div>
        <div class="fab-mini-row">
          <span class="fab-mini-label">New file</span>
          <button class="fab-mini new-file ripple-host" id="fab-newfile">📄</button>
        </div>
      </div>
    </div>
    <div id="fab-backdrop"></div>
  </div>

  <!-- EDITOR SCREEN -->
  <div id="screen-editor" class="screen">
    <div id="editor-bar">
      <button class="icon-btn ripple-host" id="editor-back">←</button>
      <span id="editor-filename">untitled</span>
      <button class="icon-btn ripple-host" id="dl-btn" title="Download" disabled>⬇</button>
      <button class="btn-save ripple-host" id="save-btn" disabled>Save</button>
    </div>
    <div class="editor-wrap" id="editor-wrap"></div>
    <div id="kbd-toolbar">
      <button class="kbd-btn" data-ins="\t">⇥ Tab</button>
      <button class="kbd-btn" data-ins="()">( )</button>
      <button class="kbd-btn" data-ins="[]">[ ]</button>
      <button class="kbd-btn" data-ins="{}">{ }</button>
      <button class="kbd-btn" data-ins='""'>" "</button>
      <button class="kbd-btn" data-ins="''">' '</button>
      <button class="kbd-btn" data-ins=":">:</button>
      <button class="kbd-btn" data-ins="=">=</button>
      <button class="kbd-btn" data-ins="->">→</button>
      <button class="kbd-btn" data-ins="#">#</button>
      <button class="kbd-btn" data-ins="import ">import</button>
      <button class="kbd-btn" data-ins="def ">def</button>
      <button class="kbd-btn" data-ins="self.">self.</button>
    </div>
  </div>

  <!-- PREVIEW SCREEN -->
  <div id="screen-preview" class="screen">
    <div id="preview-bar">
      <button class="icon-btn ripple-host" id="preview-back">←</button>
      <span id="preview-filename">preview</span>
      <button class="icon-btn ripple-host" id="preview-dl" title="Download">⬇</button>
    </div>
    <div class="preview-wrap" id="preview-wrap"></div>
  </div>

</div>

<!-- Bottom sheet -->
<div id="sheet-overlay"></div>
<div id="bottom-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-header">
    <div class="sheet-file-icon" id="sheet-icon"></div>
    <div class="sheet-file-info">
      <div class="sheet-file-name" id="sheet-name"></div>
      <div class="sheet-file-meta" id="sheet-meta"></div>
    </div>
  </div>
  <div class="sheet-actions">
    <div class="sheet-action ripple-host" data-act="open"><span class="sa-icon">📂</span> Open</div>
    <div class="sheet-action ripple-host" data-act="rename"><span class="sa-icon">✏️</span> Rename</div>
    <div class="sheet-action ripple-host" data-act="duplicate"><span class="sa-icon">⎘</span> Duplicate</div>
    <div class="sheet-action ripple-host" data-act="cut"><span class="sa-icon">✂</span> Cut</div>
    <div class="sheet-action ripple-host" data-act="copy"><span class="sa-icon">📋</span> Copy</div>
    <div class="sheet-action ripple-host" data-act="move"><span class="sa-icon">➡</span> Move to…</div>
    <div class="sheet-action ripple-host" data-act="download" id="sheet-download-btn">
      <span class="sa-icon">⬇</span> Download</div>
    <div class="sheet-action ripple-host" data-act="copypath"><span class="sa-icon">🔗</span> Copy path</div>
    <div class="sheet-action ripple-host" data-act="props"><span class="sa-icon">ℹ</span> Properties</div>
    <div class="sheet-divider"></div>
    <div class="sheet-action danger ripple-host" data-act="delete">
      <span class="sa-icon">🗑</span> Delete</div>
  </div>
</div>

<!-- Input sheet -->
<div id="input-sheet-overlay"></div>
<div id="input-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title" id="input-sheet-title">Input</div>
  <input type="text" id="input-sheet-field">
  <div class="input-sheet-btns">
    <button class="btn-sheet btn-cancel" id="input-cancel">Cancel</button>
    <button class="btn-sheet btn-confirm" id="input-confirm">OK</button>
  </div>
</div>

<!-- Confirm sheet -->
<div id="confirm-overlay"></div>
<div id="confirm-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title" id="confirm-title">Confirm</div>
  <div class="confirm-msg" id="confirm-msg"></div>
  <div class="input-sheet-btns">
    <button class="btn-sheet btn-cancel" id="confirm-cancel">Cancel</button>
    <button class="btn-sheet btn-danger" id="confirm-ok">Delete</button>
  </div>
</div>

<!-- Properties -->
<div id="props-overlay"></div>
<div id="props-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title">Properties</div>
  <div id="props-body"></div>
  <div class="input-sheet-btns" style="margin-top:14px">
    <button class="btn-sheet btn-cancel" id="props-close">Close</button>
  </div>
</div>

<!-- Sort sheet -->
<div id="sort-overlay"></div>
<div id="sort-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title">Sort by</div>
  <div id="sort-opts">
    <div class="sort-opt" data-by="name" data-dir="asc">Name (A → Z)<span class="sort-check">✓</span></div>
    <div class="sort-opt" data-by="name" data-dir="desc">Name (Z → A)<span class="sort-check">✓</span></div>
    <div class="sort-opt" data-by="size" data-dir="desc">Size (largest)<span class="sort-check">✓</span></div>
    <div class="sort-opt" data-by="size" data-dir="asc">Size (smallest)<span class="sort-check">✓</span></div>
    <div class="sort-opt" data-by="mtime" data-dir="desc">Modified (newest)<span class="sort-check">✓</span></div>
    <div class="sort-opt" data-by="mtime" data-dir="asc">Modified (oldest)<span class="sort-check">✓</span></div>
  </div>
</div>

<!-- Move folder picker -->
<div id="move-overlay"></div>
<div id="move-sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-title">Move to folder</div>
  <div id="move-current" style="font-size:.76rem;color:var(--muted);margin-bottom:8px">~/</div>
  <div id="move-list"></div>
  <div class="input-sheet-btns" style="margin-top:14px">
    <button class="btn-sheet btn-cancel" id="move-cancel">Cancel</button>
    <button class="btn-sheet btn-confirm" id="move-ok">Move here</button>
  </div>
</div>

<div id="drag-overlay">
  <div class="dnd-icon">📂</div>
  <p>Drop files here</p>
</div>

<div id="upload-progress-wrap"></div>
<div id="toast"></div>
<input type="file" id="file-upload-input" multiple>

<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/python/python.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/css/css.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/markdown/markdown.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/shell/shell.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/closebrackets.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/keymap/sublime.min.js"></script>

<script>
/* ============================================================
   STATE
============================================================ */
const TOKEN     = "__TOKEN__";
const BASE      = `/fm/${TOKEN}`;
let currentDir  = "";
let currentFile = null;
let sheetTarget = null;
let editor      = null;
let modified    = false;
let expiresAt   = __EXPIRES__;
let sessionTotal= __SESSION_TOTAL__;
let inputCb     = null;
let confirmCb   = null;
let viewMode    = localStorage.getItem("fm_view") || "list";
let sortBy      = localStorage.getItem("fm_sort_by") || "name";
let sortDir     = localStorage.getItem("fm_sort_dir") || "asc";
let showHidden  = localStorage.getItem("fm_hidden") === "1";
let allItems    = [];
let selecting   = false;
let selected    = new Set();
let clipboard   = null; // {mode:'cut'|'copy', items:[paths]}
let moveCursor  = "";   // current dir inside move picker

const $ = (id)=>document.getElementById(id);

/* ============================================================
   TIMER
============================================================ */
(function(){
  const ring = $("timer-ring"), txt = $("timer-text");
  const CIRC = 100.53;
  function tick(){
    const remaining = Math.max(0, expiresAt - Math.floor(Date.now()/1000));
    txt.textContent = `${String(Math.floor(remaining/60)).padStart(2,"0")}:${String(remaining%60).padStart(2,"0")}`;
    if(sessionTotal>0){
      const frac = remaining/sessionTotal;
      ring.style.strokeDashoffset = CIRC*(1-frac);
      ring.style.stroke = remaining<120?"var(--red)":remaining<300?"var(--yellow)":"var(--text2)";
    }
    if(remaining===0) $("expired-screen").classList.add("show");
  }
  tick();
  setInterval(tick,1000);
})();

/* ============================================================
   TOAST
============================================================ */
let toastTimer;
function toast(msg, type="success"){
  const el = $("toast");
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>el.className="", 3000);
}

/* ============================================================
   RIPPLE
============================================================ */
document.addEventListener("pointerdown", e=>{
  const host = e.target.closest(".ripple-host");
  if(!host) return;
  const r = document.createElement("span");
  r.className = "ripple";
  const rect = host.getBoundingClientRect();
  const sz = Math.max(rect.width, rect.height)*2;
  r.style.cssText = `width:${sz}px;height:${sz}px;left:${e.clientX-rect.left-sz/2}px;top:${e.clientY-rect.top-sz/2}px`;
  host.appendChild(r);
  r.addEventListener("animationend", ()=>r.remove());
});

/* ============================================================
   API HELPER
============================================================ */
function setLoading(on){
  const l = $("loader");
  if(on){ l.className = "loading"; }
  else { l.className = "done"; setTimeout(()=>l.className="", 400); }
}
async function api(endpoint, opts={}){
  setLoading(true);
  try{
    const res = await fetch(`${BASE}/api/${endpoint}`, opts);
    if(res.status===401){ toast("Session expired","error"); $("expired-screen").classList.add("show"); return null; }
    return res;
  }catch(e){
    toast("Network error","error");
    return null;
  } finally { setLoading(false); }
}
async function apiJson(endpoint, opts={}){
  const r = await api(endpoint, opts);
  if(!r) return null;
  try { return await r.json(); } catch { return null; }
}

/* ============================================================
   FILE UTILS
============================================================ */
function fileIcon(name, type){
  if(type==="dir") return "📁";
  const ext = name.split(".").pop().toLowerCase();
  const m = {py:"🐍",js:"🟨",ts:"🔷",tsx:"🔷",jsx:"🟨",html:"🌐",htm:"🌐",
    css:"🎨",scss:"🎨",json:"📋",md:"📝",txt:"📄",sh:"⚙️",bash:"⚙️",
    env:"🔐",log:"📜",zip:"📦",tar:"📦",gz:"📦",rar:"📦","7z":"📦",
    png:"🖼",jpg:"🖼",jpeg:"🖼",gif:"🖼",svg:"🖼",webp:"🖼",bmp:"🖼",
    mp4:"🎬",mkv:"🎬",mov:"🎬",webm:"🎬",avi:"🎬",
    mp3:"🎵",wav:"🎵",ogg:"🎵",flac:"🎵",m4a:"🎵",
    pdf:"📕",csv:"📊",xlsx:"📊",xls:"📊",doc:"📘",docx:"📘",
    xml:"📰",yml:"⚙️",yaml:"⚙️",toml:"⚙️",cfg:"⚙️",ini:"⚙️",
    sql:"🗄",db:"🗄",sqlite:"🗄"};
  return m[ext]||"📄";
}
function iconClass(name, type){
  if(type==="dir") return "folder";
  const ext = name.split(".").pop().toLowerCase();
  if(ext==="py") return "py";
  if(["js","ts","jsx","tsx"].includes(ext)) return "js";
  if(["html","htm"].includes(ext)) return "html";
  if(["css","scss","sass"].includes(ext)) return "css";
  if(["json","yaml","yml","toml"].includes(ext)) return "json";
  if(["md","txt","log","csv"].includes(ext)) return "md";
  if(["png","jpg","jpeg","gif","svg","webp","bmp"].includes(ext)) return "img";
  if(["mp4","mp3","mov","wav","ogg","flac","webm","mkv","avi","m4a"].includes(ext)) return "media";
  if(["zip","tar","gz","rar","7z"].includes(ext)) return "archive";
  if(["sh","bash","env","cfg","ini"].includes(ext)) return "code";
  return "generic";
}
function humanSize(b){
  if(b==null) return "";
  if(b<1024) return `${b} B`;
  if(b<1048576) return `${(b/1024).toFixed(1)} KB`;
  if(b<1073741824) return `${(b/1048576).toFixed(1)} MB`;
  return `${(b/1073741824).toFixed(2)} GB`;
}
function humanTime(ts){
  if(!ts) return "";
  const d = new Date(ts*1000);
  const diff = (Date.now() - d)/1000;
  if(diff<60) return "Just now";
  if(diff<3600) return `${Math.floor(diff/60)}m ago`;
  if(diff<86400) return `${Math.floor(diff/3600)}h ago`;
  if(diff<604800) return `${Math.floor(diff/86400)}d ago`;
  return d.toLocaleDateString();
}
function isPreviewable(name){
  const ext = name.split(".").pop().toLowerCase();
  return ["png","jpg","jpeg","gif","svg","webp","bmp",
    "mp4","webm","mov","mkv","mp3","wav","ogg","m4a",
    "pdf","txt","md","log","csv","json","xml","yml","yaml"].includes(ext);
}
function isImage(name){ return ["png","jpg","jpeg","gif","svg","webp","bmp"].includes(name.split(".").pop().toLowerCase()); }
function isVideo(name){ return ["mp4","webm","mov","mkv"].includes(name.split(".").pop().toLowerCase()); }
function isAudio(name){ return ["mp3","wav","ogg","m4a","flac"].includes(name.split(".").pop().toLowerCase()); }
function isPdf(name){ return name.toLowerCase().endsWith(".pdf"); }

/* ============================================================
   BREADCRUMB
============================================================ */
function renderBreadcrumb(dir){
  const wrap = $("breadcrumb-wrap");
  const parts = dir ? dir.split("/").filter(Boolean) : [];
  wrap.innerHTML = "";
  const home = document.createElement("span");
  home.className = "bc-chip" + (parts.length===0?" current":"");
  home.textContent = "Home";
  home.onclick = ()=>listDir("");
  wrap.appendChild(home);
  let cum = "";
  parts.forEach((p,i)=>{
    const sep = document.createElement("span");
    sep.className = "bc-sep"; sep.textContent = "›";
    wrap.appendChild(sep);
    cum = cum?`${cum}/${p}`:p;
    const cp = cum;
    const chip = document.createElement("span");
    chip.className = "bc-chip" + (i===parts.length-1?" current":"");
    chip.textContent = p;
    chip.onclick = ()=>listDir(cp);
    wrap.appendChild(chip);
  });
  setTimeout(()=>wrap.scrollLeft = wrap.scrollWidth, 50);
}

/* ============================================================
   LIST DIR
============================================================ */
function sortItems(items){
  const dirs = items.filter(i=>i.type==="dir");
  const files = items.filter(i=>i.type==="file");
  const cmp = (a,b)=>{
    let v=0;
    if(sortBy==="name") v = a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    else if(sortBy==="size") v = (a.size||0) - (b.size||0);
    else if(sortBy==="mtime") v = (a.mtime||0) - (b.mtime||0);
    return sortDir==="asc"?v:-v;
  };
  dirs.sort(cmp); files.sort(cmp);
  return [...dirs, ...files];
}

async function listDir(dir){
  currentDir = dir;
  exitSelectMode();
  renderBreadcrumb(dir);
  const data = await apiJson(`list?dir=${encodeURIComponent(dir)}&hidden=${showHidden?1:0}`);
  if(!data) return;
  if(!data.success){ toast(data.error,"error"); return; }
  allItems = data.items||[];
  const q = $("search-input").value.trim();
  if(q) filterFiles(q); else renderFileList(sortItems(allItems));
}

function renderFileList(items){
  const wrap = $("file-list-wrap");
  wrap.className = viewMode==="grid"?"grid-view":"";
  $("item-count").textContent = `${items.length} item${items.length!==1?"s":""}`;
  wrap.innerHTML = "";

  if(currentDir!==""){
    const back = document.createElement("div");
    back.className = "file-item ripple-host";
    back.innerHTML = `<div class="checkbox"></div>
      <div class="fi-icon-wrap generic">⬆</div>
      <div class="fi-info">
        <div class="fi-name">..</div>
        <div class="fi-meta">Parent folder</div>
      </div>`;
    back.onclick = ()=>{
      if(selecting) return;
      const parts = currentDir.split("/").filter(Boolean);
      parts.pop();
      listDir(parts.join("/"));
    };
    wrap.appendChild(back);
  }

  if(items.length===0){
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `<span class="es-icon">📭</span><p>This folder is empty</p>`;
    wrap.appendChild(empty);
    return;
  }

  items.forEach(item=>wrap.appendChild(makeFileItem(item)));
  refreshSelectionUI();
}

function makeFileItem(item){
  const el = document.createElement("div");
  el.className = "file-item ripple-host";
  el.dataset.path = item.path;
  el.dataset.name = item.name;
  el.dataset.type = item.type;
  if(selected.has(item.path)) el.classList.add("selected");

  const icon = fileIcon(item.name, item.type);
  const cls  = iconClass(item.name, item.type);
  const meta = item.type==="dir"
    ? `Folder${item.count!=null?` · ${item.count} items`:""} · ${humanTime(item.mtime)}`
    : `${humanSize(item.size)} · ${humanTime(item.mtime)}`;
  const sizeRight = item.type==="file"?humanSize(item.size):"";

  el.innerHTML = `
    <div class="checkbox">✓</div>
    <div class="fi-icon-wrap ${cls}">${icon}</div>
    <div class="fi-info">
      <div class="fi-name"></div>
      <div class="fi-meta"></div>
    </div>
    <div class="fi-right">
      <span class="fi-size">${sizeRight}</span>
      <button class="fi-more" aria-label="More">⋮</button>
    </div>`;
  el.querySelector(".fi-name").textContent = item.name;
  el.querySelector(".fi-meta").textContent = meta;

  el.querySelector(".fi-more").addEventListener("click", e=>{
    e.stopPropagation();
    openSheet(item);
  });

  // long press to enter selection
  let lpTimer = null;
  const startLP = ()=>{
    lpTimer = setTimeout(()=>{
      enterSelectMode();
      toggleSelect(item.path);
      lpTimer = null;
    }, 450);
  };
  const cancelLP = ()=>{ if(lpTimer){ clearTimeout(lpTimer); lpTimer=null; } };
  el.addEventListener("touchstart", startLP, {passive:true});
  el.addEventListener("touchend", cancelLP);
  el.addEventListener("touchmove", cancelLP);
  el.addEventListener("mousedown", startLP);
  el.addEventListener("mouseup", cancelLP);
  el.addEventListener("mouseleave", cancelLP);

  el.addEventListener("click", e=>{
    if(e.target.closest(".fi-more")) return;
    if(selecting){ toggleSelect(item.path); return; }
    if(item.type==="dir") listDir(item.path);
    else if(isPreviewable(item.name) && !["txt","md","log","csv","json","xml","yml","yaml"].includes(item.name.split(".").pop().toLowerCase()))
      openPreview(item);
    else openFile(item.path, item.name);
  });

  return el;
}

/* ============================================================
   SEARCH
============================================================ */
function filterFiles(q){
  const filtered = q
    ? allItems.filter(i=>i.name.toLowerCase().includes(q.toLowerCase()))
    : allItems;
  renderFileList(sortItems(filtered));
}
$("search-input").addEventListener("input", e=>filterFiles(e.target.value));
$("btn-search").addEventListener("click", ()=>{
  const sw = $("search-wrap");
  sw.classList.toggle("open");
  if(sw.classList.contains("open")) $("search-input").focus();
  else { $("search-input").value=""; filterFiles(""); }
});

/* ============================================================
   VIEW TOGGLE
============================================================ */
function setView(mode){
  viewMode = mode;
  localStorage.setItem("fm_view", mode);
  $("btn-list-view").classList.toggle("active", mode==="list");
  $("btn-grid-view").classList.toggle("active", mode==="grid");
  renderFileList(sortItems(allItems));
}
$("btn-list-view").onclick = ()=>setView("list");
$("btn-grid-view").onclick = ()=>setView("grid");
setView(viewMode);

/* ============================================================
   HIDDEN / REFRESH
============================================================ */
function syncHiddenBtn(){ $("btn-hidden").classList.toggle("active", showHidden); }
syncHiddenBtn();
$("btn-hidden").onclick = ()=>{
  showHidden = !showHidden;
  localStorage.setItem("fm_hidden", showHidden?"1":"0");
  syncHiddenBtn();
  toast(showHidden?"Showing hidden files":"Hidden files hidden","info");
  listDir(currentDir);
};
$("btn-refresh").onclick = ()=>{ listDir(currentDir); toast("Refreshed","info"); };

/* ============================================================
   SORT
============================================================ */
function openSortSheet(){
  document.querySelectorAll(".sort-opt").forEach(o=>{
    o.classList.toggle("active", o.dataset.by===sortBy && o.dataset.dir===sortDir);
  });
  $("sort-overlay").style.display = "block";
  setTimeout(()=>$("sort-sheet").classList.add("open"), 10);
}
function closeSortSheet(){
  $("sort-sheet").classList.remove("open");
  setTimeout(()=>$("sort-overlay").style.display="none", 300);
}
$("btn-sort").onclick = openSortSheet;
$("sort-overlay").onclick = closeSortSheet;
document.querySelectorAll(".sort-opt").forEach(o=>{
  o.addEventListener("click", ()=>{
    sortBy = o.dataset.by; sortDir = o.dataset.dir;
    localStorage.setItem("fm_sort_by", sortBy);
    localStorage.setItem("fm_sort_dir", sortDir);
    closeSortSheet();
    renderFileList(sortItems(allItems));
  });
});

/* ============================================================
   FAB
============================================================ */
$("fab-main-btn").onclick = ()=>$("fab-wrap").classList.toggle("open");
$("fab-backdrop").onclick = ()=>$("fab-wrap").classList.remove("open");
function closeFab(){ $("fab-wrap").classList.remove("open"); }
$("fab-upload").onclick = ()=>{ closeFab(); $("file-upload-input").click(); };
$("fab-newfile").onclick = ()=>{ closeFab(); newFile(); };
$("fab-newfolder").onclick = ()=>{ closeFab(); newFolder(); };

/* ============================================================
   BOTTOM SHEET (single item)
============================================================ */
function openSheet(item){
  sheetTarget = item;
  $("sheet-icon").textContent = fileIcon(item.name, item.type);
  $("sheet-icon").className = `sheet-file-icon fi-icon-wrap ${iconClass(item.name, item.type)}`;
  $("sheet-name").textContent = item.name;
  $("sheet-meta").textContent = item.type==="dir"
    ? `Folder · ${humanTime(item.mtime)}`
    : `${humanSize(item.size)} · ${humanTime(item.mtime)}`;
  const dl = $("sheet-download-btn");
  dl.classList.remove("disabled");
  $("sheet-overlay").style.display = "block";
  setTimeout(()=>$("bottom-sheet").classList.add("open"), 10);
}
function closeSheet(){
  $("bottom-sheet").classList.remove("open");
  setTimeout(()=>$("sheet-overlay").style.display="none", 300);
}
$("sheet-overlay").onclick = closeSheet;
document.querySelectorAll("#bottom-sheet .sheet-action").forEach(a=>{
  a.addEventListener("click", ()=>handleSheetAction(a.dataset.act));
});

async function handleSheetAction(act){
  const t = sheetTarget;
  closeSheet();
  if(!t) return;
  switch(act){
    case "open":
      if(t.type==="dir") listDir(t.path);
      else if(isPreviewable(t.name) && !["txt","md","log","csv","json","xml","yml","yaml"].includes(t.name.split(".").pop().toLowerCase()))
        openPreview(t);
      else openFile(t.path, t.name);
      break;
    case "rename":
      showInputSheet(`Rename "${t.name}"`, t.name, async newName=>{
        if(!newName.trim() || newName===t.name) return;
        const dir = t.path.split("/").slice(0,-1).join("/");
        const newPath = dir?`${dir}/${newName.trim()}`:newName.trim();
        const d = await apiJson("rename",{method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({old_path:t.path, new_path:newPath})});
        if(d&&d.success){ toast("Renamed"); listDir(currentDir); }
        else toast(d?.error||"Failed","error");
      });
      break;
    case "duplicate": {
      const d = await apiJson("duplicate",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({path:t.path})});
      if(d&&d.success){ toast("Duplicated"); listDir(currentDir); }
      else toast(d?.error||"Failed","error");
      break;
    }
    case "cut":
      clipboard = {mode:"cut", items:[t.path]};
      showPasteBanner();
      break;
    case "copy":
      clipboard = {mode:"copy", items:[t.path]};
      showPasteBanner();
      break;
    case "move":
      openMovePicker([t.path]);
      break;
    case "download":
      if(t.type==="dir") downloadZip([t.path], t.name+".zip");
      else downloadFile(t.path);
      break;
    case "copypath":
      const full = "/" + t.path;
      if(navigator.clipboard) navigator.clipboard.writeText(full).then(
        ()=>toast("Path copied","info"),
        ()=>toast(`Path: ${full}`,"info"));
      else toast(`Path: ${full}`,"info");
      break;
    case "props":
      showProps(t);
      break;
    case "delete":
      askConfirm(`Delete "${t.name}"?`,
        t.type==="dir"?"This folder and its contents will be permanently removed.":
                       "This file will be permanently removed.",
        async ()=>{
          const d = await apiJson("delete",{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({paths:[t.path]})});
          if(d&&d.success){ toast("Deleted"); if(currentFile===t.path) closeEditor(); listDir(currentDir); }
          else toast(d?.error||"Failed","error");
        });
      break;
  }
}

/* ============================================================
   INPUT SHEET
============================================================ */
function showInputSheet(title, prefill, cb){
  inputCb = cb;
  $("input-sheet-title").textContent = title;
  const inp = $("input-sheet-field");
  inp.value = prefill||"";
  inp.placeholder = prefill||"Enter value…";
  $("input-sheet-overlay").style.display = "block";
  setTimeout(()=>{
    $("input-sheet").classList.add("open");
    inp.focus();
    if(prefill){
      const dot = prefill.lastIndexOf(".");
      if(dot>0) inp.setSelectionRange(0, dot);
      else inp.select();
    }
  },10);
}
function closeInputSheet(){
  $("input-sheet").classList.remove("open");
  setTimeout(()=>$("input-sheet-overlay").style.display="none",300);
  inputCb = null;
}
$("input-sheet-overlay").onclick = closeInputSheet;
$("input-cancel").onclick = closeInputSheet;
$("input-confirm").onclick = ()=>{
  const v = $("input-sheet-field").value;
  const cb = inputCb;
  closeInputSheet();
  if(cb) cb(v);
};
$("input-sheet-field").addEventListener("keydown", e=>{
  if(e.key==="Enter") $("input-confirm").click();
  if(e.key==="Escape") closeInputSheet();
});

/* ============================================================
   CONFIRM SHEET
============================================================ */
function askConfirm(title, msg, cb, okText="Delete", danger=true){
  confirmCb = cb;
  $("confirm-title").textContent = title;
  $("confirm-msg").textContent = msg;
  const ok = $("confirm-ok");
  ok.textContent = okText;
  ok.className = "btn-sheet " + (danger?"btn-danger":"btn-confirm");
  $("confirm-overlay").style.display = "block";
  setTimeout(()=>$("confirm-sheet").classList.add("open"),10);
}
function closeConfirm(){
  $("confirm-sheet").classList.remove("open");
  setTimeout(()=>$("confirm-overlay").style.display="none",300);
  confirmCb = null;
}
$("confirm-overlay").onclick = closeConfirm;
$("confirm-cancel").onclick = closeConfirm;
$("confirm-ok").onclick = ()=>{ const cb = confirmCb; closeConfirm(); if(cb) cb(); };

/* ============================================================
   PROPERTIES
============================================================ */
async function showProps(item){
  const d = await apiJson(`stat?path=${encodeURIComponent(item.path)}`);
  if(!d||!d.success){ toast("Cannot read properties","error"); return; }
  const rows = [
    ["Name", d.name],
    ["Type", d.type==="dir"?"Folder":(d.mime||"File")],
    ["Path", "/"+item.path],
    ["Size", d.type==="dir"?(d.count!=null?`${d.count} items`:""):humanSize(d.size)],
    ["Modified", new Date(d.mtime*1000).toLocaleString()],
    ["Created",  new Date(d.ctime*1000).toLocaleString()],
    ["Permissions", d.perms],
  ];
  $("props-body").innerHTML = rows.map(([k,v])=>
    `<div class="props-row"><span class="props-key">${k}</span><span class="props-val"></span></div>`
  ).join("");
  document.querySelectorAll("#props-body .props-val").forEach((el,i)=>el.textContent = rows[i][1]||"—");
  $("props-overlay").style.display = "block";
  setTimeout(()=>$("props-sheet").classList.add("open"),10);
}
function closeProps(){
  $("props-sheet").classList.remove("open");
  setTimeout(()=>$("props-overlay").style.display="none",300);
}
$("props-overlay").onclick = closeProps;
$("props-close").onclick = closeProps;

/* ============================================================
   NEW FILE / FOLDER
============================================================ */
function newFile(){
  showInputSheet("New file", "untitled.txt", async name=>{
    if(!name.trim()) return;
    const path = currentDir?`${currentDir}/${name.trim()}`:name.trim();
    const d = await apiJson("write",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path, content:""})});
    if(d&&d.success){ toast("File created"); listDir(currentDir); }
    else toast(d?.error||"Failed","error");
  });
}
function newFolder(){
  showInputSheet("New folder", "new_folder", async name=>{
    if(!name.trim()) return;
    const path = currentDir?`${currentDir}/${name.trim()}`:name.trim();
    const d = await apiJson("mkdir",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path})});
    if(d&&d.success){ toast("Folder created"); listDir(currentDir); }
    else toast(d?.error||"Failed","error");
  });
}

/* ============================================================
   UPLOAD
============================================================ */
$("file-upload-input").addEventListener("change", e=>uploadFiles(e.target.files));

async function uploadFiles(files){
  if(!files||!files.length) return;
  const wrap = $("upload-progress-wrap");
  let ok = 0, fail = 0;
  for(const file of files){
    const item = document.createElement("div");
    item.className = "upload-prog-item";
    item.innerHTML = `<div></div>
      <div class="upload-prog-bar-wrap"><div class="upload-prog-bar"></div></div>`;
    item.firstChild.textContent = file.name;
    wrap.appendChild(item);
    const bar = item.querySelector(".upload-prog-bar");

    const success = await new Promise(resolve=>{
      const fd = new FormData();
      fd.append("file", file);
      fd.append("dir", currentDir);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/api/upload`);
      xhr.upload.onprogress = ev=>{
        if(ev.lengthComputable) bar.style.width = `${(ev.loaded/ev.total)*100}%`;
      };
      xhr.onload = ()=>{
        try{ const d = JSON.parse(xhr.responseText); resolve(d.success); }
        catch{ resolve(false); }
      };
      xhr.onerror = ()=>resolve(false);
      xhr.send(fd);
    });

    if(success) ok++; else fail++;
    bar.style.width = "100%";
    setTimeout(()=>item.remove(), 800);
  }

  if(ok) toast(`${ok} uploaded${fail?` · ${fail} failed`:""}`, fail?"info":"success");
  if(!ok && fail) toast(`Upload failed`,"error");
  listDir(currentDir);
  $("file-upload-input").value="";
}

/* ============================================================
   DRAG & DROP
============================================================ */
const dragOverlay = $("drag-overlay");
let dragDepth = 0;
document.addEventListener("dragenter", e=>{ e.preventDefault(); dragDepth++; dragOverlay.classList.add("active"); });
document.addEventListener("dragleave", ()=>{ dragDepth--; if(dragDepth<=0){ dragDepth=0; dragOverlay.classList.remove("active"); } });
document.addEventListener("dragover", e=>e.preventDefault());
document.addEventListener("drop", e=>{
  e.preventDefault();
  dragDepth = 0;
  dragOverlay.classList.remove("active");
  if(e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
});

/* ============================================================
   EDITOR
============================================================ */
async function openFile(path, name){
  const d = await apiJson(`read?path=${encodeURIComponent(path)}`);
  if(!d) return;
  if(!d.success){ toast(d.error,"error"); return; }
  if(d.binary){ toast("Binary file — opening preview","info"); openPreview({path, name, type:"file"}); return; }

  currentFile = path;
  modified = false;
  const label = $("editor-filename");
  label.textContent = name;
  label.classList.remove("modified");
  $("save-btn").disabled = false;
  $("dl-btn").disabled = false;

  const wrap = $("editor-wrap");
  wrap.innerHTML = `<textarea id="cm-editor"></textarea>`;
  editor = CodeMirror.fromTextArea($("cm-editor"),{
    value: d.content, mode: detectMode(name), theme: "dracula",
    lineNumbers:true, matchBrackets:true, autoCloseBrackets:true,
    keyMap:"sublime", tabSize:4, indentWithTabs:false, lineWrapping:true,
    extraKeys:{"Ctrl-S":saveFile,"Cmd-S":saveFile,"Ctrl-F":"findPersistent"}
  });
  editor.setValue(d.content);
  editor.clearHistory();
  editor.on("change", ()=>{
    if(!modified){ modified=true; label.classList.add("modified"); }
  });

  $("app").classList.add("editor-open");
  setTimeout(()=>{ editor.setSize("100%","100%"); editor.refresh(); },350);
}
function closeEditor(force){
  if(modified && !force){
    askConfirm("Unsaved changes",
      "You have unsaved edits. Discard them?",
      ()=>closeEditor(true), "Discard", true);
    return;
  }
  $("app").classList.remove("editor-open");
  modified = false;
  $("editor-filename").classList.remove("modified");
  setTimeout(()=>listDir(currentDir), 300);
}
$("editor-back").onclick = ()=>closeEditor();
$("save-btn").onclick = ()=>saveFile();
$("dl-btn").onclick = ()=>downloadFile();

function detectMode(name){
  const ext = name.split(".").pop().toLowerCase();
  return ({py:"python",js:"javascript",ts:"javascript",jsx:"javascript",
    tsx:"javascript",json:"javascript",
    html:"htmlmixed",htm:"htmlmixed",xml:"xml",
    css:"css",scss:"css",md:"markdown",
    sh:"shell",bash:"shell"})[ext]||"text/plain";
}

async function saveFile(){
  if(!currentFile||!editor) return;
  const btn = $("save-btn");
  btn.textContent = "Saving…"; btn.disabled = true;
  const d = await apiJson("write",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:currentFile, content:editor.getValue()})});
  btn.textContent = "Save"; btn.disabled = false;
  if(d&&d.success){
    modified = false;
    $("editor-filename").classList.remove("modified");
    toast("Saved");
  } else toast(d?.error||"Save failed","error");
}

document.querySelectorAll(".kbd-btn").forEach(b=>{
  b.addEventListener("click", ()=>{
    if(!editor) return;
    const s = b.dataset.ins;
    editor.replaceSelection(s);
    if(["()","[]","{}",'""',"''"].includes(s)){
      const c = editor.getCursor();
      editor.setCursor({line:c.line, ch:c.ch-1});
    }
    editor.focus();
  });
});

/* ============================================================
   PREVIEW
============================================================ */
function openPreview(item){
  $("preview-filename").textContent = item.name;
  const wrap = $("preview-wrap");
  wrap.classList.remove("text");
  wrap.innerHTML = "";
  const url = `${BASE}/api/raw?path=${encodeURIComponent(item.path)}`;
  if(isImage(item.name)){
    const img = document.createElement("img");
    img.src = url; wrap.appendChild(img);
  } else if(isVideo(item.name)){
    const v = document.createElement("video");
    v.src = url; v.controls = true; v.autoplay = true;
    wrap.appendChild(v);
  } else if(isAudio(item.name)){
    const a = document.createElement("audio");
    a.src = url; a.controls = true; a.autoplay = true;
    wrap.appendChild(a);
  } else if(isPdf(item.name)){
    const f = document.createElement("iframe");
    f.src = url; wrap.appendChild(f);
  } else {
    wrap.classList.add("text");
    wrap.innerHTML = `<pre>Loading…</pre>`;
    fetch(`${BASE}/api/read?path=${encodeURIComponent(item.path)}`)
      .then(r=>r.json()).then(d=>{
        wrap.querySelector("pre").textContent = d.content||"";
      });
  }
  $("preview-dl").onclick = ()=>downloadFile(item.path);
  $("app").classList.add("preview-open");
}
function closePreview(){
  $("app").classList.remove("preview-open");
  $("preview-wrap").innerHTML = "";
}
$("preview-back").onclick = closePreview;

/* ============================================================
   DOWNLOAD
============================================================ */
function downloadFile(path){
  const p = path||currentFile;
  if(!p) return;
  const a = document.createElement("a");
  a.href = `${BASE}/api/download?path=${encodeURIComponent(p)}`;
  a.download = p.split("/").pop();
  document.body.appendChild(a); a.click(); a.remove();
}
function downloadZip(paths, name){
  const a = document.createElement("a");
  const qs = paths.map(p=>`paths=${encodeURIComponent(p)}`).join("&");
  a.href = `${BASE}/api/zip?${qs}&name=${encodeURIComponent(name||"archive.zip")}`;
  a.download = name||"archive.zip";
  document.body.appendChild(a); a.click(); a.remove();
  toast("Preparing zip…","info");
}

/* ============================================================
   SELECTION MODE
============================================================ */
function enterSelectMode(){
  selecting = true;
  $("app").classList.add("selecting");
  $("main-bar").style.display = "none";
  $("select-bar").style.display = "flex";
  refreshSelectionUI();
}
function exitSelectMode(){
  selecting = false;
  selected.clear();
  $("app").classList.remove("selecting");
  $("main-bar").style.display = "flex";
  $("select-bar").style.display = "none";
  document.querySelectorAll(".file-item.selected").forEach(e=>e.classList.remove("selected"));
}
function toggleSelect(path){
  if(selected.has(path)) selected.delete(path);
  else selected.add(path);
  document.querySelectorAll(".file-item").forEach(el=>{
    if(el.dataset.path===path) el.classList.toggle("selected", selected.has(path));
  });
  refreshSelectionUI();
  if(selected.size===0) exitSelectMode();
}
function refreshSelectionUI(){
  $("select-count").textContent = `${selected.size} selected`;
}
$("btn-cancel-select").onclick = exitSelectMode;
$("btn-select-all").onclick = ()=>{
  if(selected.size===allItems.length){ exitSelectMode(); return; }
  allItems.forEach(i=>selected.add(i.path));
  document.querySelectorAll(".file-item[data-path]").forEach(el=>{
    if(el.dataset.path) el.classList.add("selected");
  });
  refreshSelectionUI();
};
$("btn-bulk-cut").onclick = ()=>{
  if(!selected.size) return;
  clipboard = {mode:"cut", items:[...selected]};
  exitSelectMode();
  showPasteBanner();
};
$("btn-bulk-copy").onclick = ()=>{
  if(!selected.size) return;
  clipboard = {mode:"copy", items:[...selected]};
  exitSelectMode();
  showPasteBanner();
};
$("btn-bulk-download").onclick = ()=>{
  if(!selected.size) return;
  if(selected.size===1){
    const p = [...selected][0];
    const it = allItems.find(i=>i.path===p);
    if(it && it.type==="file") downloadFile(p);
    else downloadZip([p], (it?.name||"folder")+".zip");
  } else downloadZip([...selected], "selection.zip");
  exitSelectMode();
};
$("btn-bulk-delete").onclick = ()=>{
  if(!selected.size) return;
  const n = selected.size;
  askConfirm(`Delete ${n} item${n>1?"s":""}?`,
    "These items will be permanently removed.", async ()=>{
      const paths = [...selected];
      const d = await apiJson("delete",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paths})});
      if(d&&d.success){ toast(`Deleted ${n} item(s)`); exitSelectMode(); listDir(currentDir); }
      else toast(d?.error||"Failed","error");
    });
};

/* ============================================================
   PASTE BANNER
============================================================ */
function showPasteBanner(){
  if(!clipboard) return;
  $("paste-mode-label").textContent = clipboard.mode==="cut"?"Cut":"Copy";
  $("paste-count-label").textContent = `${clipboard.items.length} item${clipboard.items.length>1?"s":""}`;
  $("paste-banner").classList.add("show");
}
function hidePasteBanner(){ $("paste-banner").classList.remove("show"); }
$("btn-paste-cancel").onclick = ()=>{ clipboard=null; hidePasteBanner(); };
$("btn-paste").onclick = async ()=>{
  if(!clipboard) return;
  const d = await apiJson("paste",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({mode:clipboard.mode, items:clipboard.items, dest:currentDir})});
  if(d&&d.success){
    toast(clipboard.mode==="cut"?"Moved":"Copied");
    clipboard = null;
    hidePasteBanner();
    listDir(currentDir);
  } else toast(d?.error||"Failed","error");
};

/* ============================================================
   MOVE PICKER
============================================================ */
let movePayload = [];
async function openMovePicker(paths){
  movePayload = paths;
  moveCursor = "";
  $("move-overlay").style.display = "block";
  setTimeout(()=>$("move-sheet").classList.add("open"), 10);
  await renderMovePicker();
}
async function renderMovePicker(){
  $("move-current").textContent = "/" + (moveCursor||"");
  const d = await apiJson(`list?dir=${encodeURIComponent(moveCursor)}&hidden=${showHidden?1:0}`);
  const list = $("move-list");
  list.innerHTML = "";
  if(moveCursor){
    const up = document.createElement("div");
    up.className = "move-item";
    up.innerHTML = `<span class="mv-icon">⬆</span><span>.. (parent)</span>`;
    up.onclick = ()=>{
      const parts = moveCursor.split("/").filter(Boolean);
      parts.pop();
      moveCursor = parts.join("/");
      renderMovePicker();
    };
    list.appendChild(up);
  }
  if(d&&d.success){
    d.items.filter(i=>i.type==="dir").forEach(i=>{
      const el = document.createElement("div");
      el.className = "move-item";
      el.innerHTML = `<span class="mv-icon">📁</span><span></span>`;
      el.lastChild.textContent = i.name;
      el.onclick = ()=>{ moveCursor = i.path; renderMovePicker(); };
      list.appendChild(el);
    });
  }
}
function closeMove(){
  $("move-sheet").classList.remove("open");
  setTimeout(()=>$("move-overlay").style.display="none",300);
}
$("move-overlay").onclick = closeMove;
$("move-cancel").onclick = closeMove;
$("move-ok").onclick = async ()=>{
  if(!movePayload.length){ closeMove(); return; }
  const d = await apiJson("paste",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({mode:"cut", items:movePayload, dest:moveCursor})});
  closeMove();
  if(d&&d.success){ toast("Moved"); listDir(currentDir); }
  else toast(d?.error||"Failed","error");
};

/* ============================================================
   GLOBAL KEYBOARD
============================================================ */
document.addEventListener("keydown", e=>{
  if((e.ctrlKey||e.metaKey) && e.key==="s"){ e.preventDefault(); saveFile(); }
  if(e.key==="Escape"){
    closeFab(); closeSheet(); closeInputSheet(); closeConfirm();
    closeProps(); closeSortSheet(); closeMove();
    if(selecting) exitSelectMode();
  }
});

/* ============================================================
   INIT
============================================================ */
listDir("");
</script>
</body>
</html>"""

EXPIRED_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Expired</title><style>body{background:#0d0f14;color:#e2e8f0;display:flex;
align-items:center;justify-content:center;height:100vh;
font-family:system-ui,sans-serif;text-align:center}
h1{color:#e2e8f0;font-size:1.8rem;font-weight:700}
p{color:#4a5568;margin-top:1rem;line-height:1.6;font-size:.9rem}
</style></head><body><div><h1>Session Expired</h1>
<p>Your file manager session has expired.<br>Request a new link from the bot.</p>
</div></body></html>"""

@app.route("/fm/<token>/")
def file_manager(token):
    data = validate_token(token)
    if not data:
        return render_template_string(EXPIRED_HTML), 401
    expires_at_js  = int(data["expires_at"])
    session_total  = int(data.get("session_total", 1800))
    html = (FM_HTML
        .replace("__TOKEN__", token)
        .replace("__EXPIRES__", str(expires_at_js))
        .replace("__SESSION_TOTAL__", str(session_total)))
    return Response(html, mimetype="text/html")

# ─────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────
@app.route("/fm/<token>/api/list")
def api_list(token):
    td = get_token_data(token)
    base = td["project_dir"]
    rel  = request.args.get("dir", "")
    show_hidden = request.args.get("hidden") == "1"
    path = safe_path(base, rel)
    if not path or not os.path.isdir(path):
        return jsonify({"success": False, "error": "Invalid path"})
    items = []
    try:
        for entry in os.scandir(path):
            if not show_hidden and is_hidden(entry.name):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rel_path = os.path.relpath(os.path.join(path, entry.name), base).replace(os.sep, "/")
            row = {
                "name": entry.name,
                "path": rel_path,
                "type": "dir" if entry.is_dir() else "file",
                "size": st.st_size if entry.is_file() else 0,
                "mtime": int(st.st_mtime),
            }
            if entry.is_dir():
                try: row["count"] = sum(1 for _ in os.scandir(os.path.join(path, entry.name)))
                except OSError: row["count"] = None
            items.append(row)
    except PermissionError:
        return jsonify({"success": False, "error": "Permission denied"})
    return jsonify({"success": True, "items": items})

@app.route("/fm/<token>/api/stat")
def api_stat(token):
    td = get_token_data(token)
    base = td["project_dir"]
    rel  = request.args.get("path", "")
    path = safe_path(base, rel)
    if not path or not os.path.exists(path):
        return jsonify({"success": False, "error": "Not found"})
    try:
        st = os.stat(path)
        info = {
            "success": True,
            "name": os.path.basename(path),
            "type": "dir" if os.path.isdir(path) else "file",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "ctime": int(st.st_ctime),
            "perms": stat.filemode(st.st_mode),
            "mime": mimetypes.guess_type(path)[0] or "",
        }
        if os.path.isdir(path):
            try: info["count"] = sum(1 for _ in os.scandir(path))
            except OSError: info["count"] = None
        return jsonify(info)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/read")
def api_read(token):
    td = get_token_data(token)
    base = td["project_dir"]
    rel  = request.args.get("path", "")
    path = safe_path(base, rel)
    if not path or not os.path.isfile(path):
        return jsonify({"success": False, "error": "File not found"})
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return jsonify({"success": True, "binary": True, "content": ""})
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return jsonify({"success": True, "binary": False, "content": f.read()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/raw")
def api_raw(token):
    td = get_token_data(token)
    base = td["project_dir"]
    rel  = request.args.get("path", "")
    path = safe_path(base, rel)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)

@app.route("/fm/<token>/api/write", methods=["POST"])
def api_write(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    rel  = body.get("path", "")
    content = body.get("content", "")
    path = safe_path(base, rel)
    if not path:
        return jsonify({"success": False, "error": "Invalid path"})
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/mkdir", methods=["POST"])
def api_mkdir(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    path = safe_path(base, body.get("path", ""))
    if not path:
        return jsonify({"success": False, "error": "Invalid path"})
    if os.path.exists(path):
        return jsonify({"success": False, "error": "Already exists"})
    try:
        os.makedirs(path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/delete", methods=["POST"])
def api_delete(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    paths = body.get("paths") or ([body.get("path")] if body.get("path") else [])
    if not paths:
        return jsonify({"success": False, "error": "No paths"})
    errors = []
    for rel in paths:
        path = safe_path(base, rel)
        if not path or not os.path.exists(path):
            errors.append(f"{rel}: not found"); continue
        try:
            if os.path.isdir(path): shutil.rmtree(path)
            else: os.remove(path)
        except Exception as e:
            errors.append(f"{rel}: {e}")
    if errors:
        return jsonify({"success": False, "error": "; ".join(errors[:3])})
    return jsonify({"success": True})

@app.route("/fm/<token>/api/rename", methods=["POST"])
def api_rename(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    old = safe_path(base, body.get("old_path", ""))
    new_ = safe_path(base, body.get("new_path", ""))
    if not old or not new_:
        return jsonify({"success": False, "error": "Invalid path"})
    if not os.path.exists(old):
        return jsonify({"success": False, "error": "Source not found"})
    if os.path.exists(new_):
        return jsonify({"success": False, "error": "Target already exists"})
    try:
        os.rename(old, new_)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/duplicate", methods=["POST"])
def api_duplicate(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    src = safe_path(base, body.get("path", ""))
    if not src or not os.path.exists(src):
        return jsonify({"success": False, "error": "Not found"})
    dirname, name = os.path.split(src)
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        new_name = f"{stem} (copy{f' {i}' if i>1 else ''}){ext}"
        dst = os.path.join(dirname, new_name)
        if not os.path.exists(dst): break
        i += 1
    try:
        if os.path.isdir(src): shutil.copytree(src, dst)
        else: shutil.copy2(src, dst)
        return jsonify({"success": True, "new_name": new_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/fm/<token>/api/paste", methods=["POST"])
def api_paste(token):
    td = get_token_data(token)
    base = td["project_dir"]
    body = request.get_json(force=True) or {}
    mode = body.get("mode", "copy")
    items = body.get("items", [])
    dest_rel = body.get("dest", "")
    dest = safe_path(base, dest_rel)
    if not dest or not os.path.isdir(dest):
        return jsonify({"success": False, "error": "Invalid destination"})
    errors = []
    for rel in items:
        src = safe_path(base, rel)
        if not src or not os.path.exists(src):
            errors.append(f"{rel}: not found"); continue
        name = os.path.basename(src)
        target = os.path.join(dest, name)
        if os.path.realpath(target) == os.path.realpath(src) and mode == "cut":
            continue
        if os.path.commonpath([os.path.realpath(src), os.path.realpath(target)]) == os.path.realpath(src) and os.path.isdir(src) and mode == "cut":
            errors.append(f"{name}: cannot move into self"); continue
        if os.path.exists(target):
            stem, ext = os.path.splitext(name)
            i = 1
            while True:
                cand = os.path.join(dest, f"{stem} ({i}){ext}")
                if not os.path.exists(cand): target = cand; break
                i += 1
        try:
            if mode == "cut": shutil.move(src, target)
            else:
                if os.path.isdir(src): shutil.copytree(src, target)
                else: shutil.copy2(src, target)
        except Exception as e:
            errors.append(f"{name}: {e}")
    if errors:
        return jsonify({"success": False, "error": "; ".join(errors[:3])})
    return jsonify({"success": True})

@app.route("/fm/<token>/api/upload", methods=["POST"])
def api_upload(token):
    td = get_token_data(token)
    base = td["project_dir"]
    dest_dir = safe_path(base, request.form.get("dir", ""))
    if not dest_dir:
        return jsonify({"success": False, "error": "Invalid path"})
    os.makedirs(dest_dir, exist_ok=True)
    saved = []
    for f in request.files.getlist("file"):
        filename = os.path.basename(f.filename or "upload")
        dest = os.path.join(dest_dir, filename)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(filename)
            i = 1
            while os.path.exists(os.path.join(dest_dir, f"{stem} ({i}){ext}")):
                i += 1
            dest = os.path.join(dest_dir, f"{stem} ({i}){ext}")
        try:
            f.save(dest); saved.append(os.path.basename(dest))
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    return jsonify({"success": True, "saved": saved})

@app.route("/fm/<token>/api/download")
def api_download(token):
    td = get_token_data(token)
    base = td["project_dir"]
    path = safe_path(base, request.args.get("path", ""))
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))

@app.route("/fm/<token>/api/zip")
def api_zip(token):
    td = get_token_data(token)
    base = td["project_dir"]
    rels = request.args.getlist("paths")
    name = request.args.get("name", "archive.zip")
    if not rels:
        abort(400)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rels:
            src = safe_path(base, rel)
            if not src or not os.path.exists(src): continue
            if os.path.isfile(src):
                zf.write(src, arcname=os.path.basename(src))
            else:
                root_name = os.path.basename(src.rstrip("/"))
                for dp, _, files in os.walk(src):
                    for fn in files:
                        full = os.path.join(dp, fn)
                        rel_in = os.path.relpath(full, os.path.dirname(src))
                        try:
                            zf.write(full, arcname=rel_in)
                        except Exception:
                            pass
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=name)

# ─────────────────────────────────────────────────────────────
# Standalone runner — auto-create dev token
# ─────────────────────────────────────────────────────────────
def create_dev_token(project_dir: str = ".",
                     token: str = "dev",
                     duration_sec: int = 3600) -> str:
    project_dir = os.path.realpath(project_dir)
    os.makedirs(project_dir, exist_ok=True)
    token_store[token] = {
        "project_dir":   project_dir,
        "expires_at":    time.time() + duration_sec,
        "session_total": duration_sec,
    }
    return token

def start_flask(port: int = 8080):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    project_dir = os.environ.get("FM_ROOT", os.getcwd())
    token = create_dev_token(project_dir=project_dir, duration_sec=3600)
    print(f"\nGod Madara File Manager running")
    print(f"   Root : {os.path.realpath(project_dir)}")
    print(f"   Open : http://localhost:{port}/fm/{token}/\n")
    start_flask(port)
