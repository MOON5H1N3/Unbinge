import sys, types, os, tempfile, json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
for m in ['apscheduler','apscheduler.schedulers','apscheduler.schedulers.background']:
    sys.modules[m]=types.ModuleType(m)
class _S:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler=_S
tmp=tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp,'d.db'),POOL_DIR='/media/A/TV100',VAULT_DIR='/media/vault',CONFIG_DIR=tmp)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

SERIES=[{'id':1,'title':'Alpha','path':'/tv/pool/Alpha','tvdbId':11},
        {'id':2,'title':'Beta','path':'/tv/pool/Beta','tvdbId':22},
        {'id':3,'title':'Dup','path':'/tv/x/Dup','tvdbId':33},
        {'id':4,'title':'Dup','path':'/tv/y/Dup','tvdbId':34}]
puts=[]
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,obj):
        b=json.dumps(obj).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        assert self.headers['X-Api-Key']=='k'
        u=urlparse(self.path)
        if u.path=='/api/v3/series': self._send(SERIES)
        else:
            i=int(u.path.rsplit('/',1)[1]); self._send(next(s for s in SERIES if s['id']==i))
    def do_PUT(self):
        n=int(self.headers['Content-Length']); body=json.loads(self.rfile.read(n))
        u=urlparse(self.path); puts.append((u.path,parse_qs(u.query),body['path']))
        for s in SERIES:
            if s['id']==body['id']: s['path']=body['path']
        self._send(body)
srv=HTTPServer(('127.0.0.1',0),H); threading.Thread(target=srv.serve_forever,daemon=True).start()

import app
app.init_db()
app.set_setting('sonarr_url',f'http://127.0.0.1:{srv.server_port}'); app.set_setting('sonarr_api_key','k')
app.set_setting('sonarr_path_map','/media/A/TV100=/tv/pool\n/media/vault=/tv/vault')

# promote: pool -> vault, matched by (mapped) path, moveFiles=false
r=app.sonarr_set_series_path('Alpha',None,'/media/A/TV100/Alpha','/media/vault/Alpha'); print(r)
assert r[0]=='updated' and puts[-1]==('/api/v3/series/1',{'moveFiles':['false']},'/tv/vault/Alpha')
# already there -> unchanged
r=app.sonarr_set_series_path('Alpha',None,'/media/A/TV100/Alpha','/media/vault/Alpha'); print(r); assert r[0]=='unchanged'
# graduate back
r=app.sonarr_set_series_path('Alpha',None,'/media/vault/Alpha','/media/A/TV100/Alpha'); print(r)
assert r[0]=='updated' and puts[-1][2]=='/tv/pool/Alpha'
# fallback by tvdb id when the old path isn't what Sonarr has
r=app.sonarr_set_series_path('Whatever',22,'/media/A/TV100/Wrong','/media/vault/Beta'); print(r)
assert r[0]=='updated' and puts[-1][2]=='/tv/vault/Beta'
# ambiguous title -> skipped, nothing written
n=len(puts)
r=app.sonarr_set_series_path('Dup',None,'/media/A/TV100/Dup','/media/vault/Dup'); print(r)
assert r[0]=='skipped' and len(puts)==n
# no match
r=app.sonarr_set_series_path('Nope',None,'/media/A/TV100/Nope','/media/vault/Nope'); print(r); assert r[0]=='skipped'
# toggle off
app.set_setting('sonarr_sync_paths','0')
r=app.sonarr_set_series_path('Alpha',None,'/media/A/TV100/Alpha','/media/vault/Alpha'); print(r); assert r[0]=='skipped'
app.set_setting('sonarr_sync_paths','1')
# unreachable -> error, no raise
app.set_setting('sonarr_url','http://127.0.0.1:9')
r=app.sonarr_set_series_path('Alpha',None,'/media/A/TV100/Alpha','/media/vault/Alpha'); print(r[0]); assert r[0]=='error'
# async wrapper writes history on update
app.set_setting('sonarr_url',f'http://127.0.0.1:{srv.server_port}')
app.sync_sonarr_path_async(7,'Alpha',None,'/media/vault/Alpha','/media/A/TV100/Alpha'); 
# (Alpha currently at /tv/pool/Alpha so this is 'unchanged' -> no history) ; now one that updates:
app.sync_sonarr_path_async(8,'Alpha',None,'/media/A/TV100/Alpha','/media/vault/Alpha'); time.sleep(1.5)
with app.closing(app.get_db()) as c:
    print([tuple(x) for x in c.execute(f"SELECT show_id,action,detail FROM {app.HISTORY_TABLE}")])
print("ALL OK")
