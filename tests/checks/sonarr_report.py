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
        {'id':4,'title':'Dup','path':'/tv/y/Dup','tvdbId':34},
        {'id':5,'title':'Gamma (2019)','path':'/data/shows/Gamma','tvdbId':55},{'id':6,'title':'The Delta Show (2020)','path':'/data/x/delta-weird','tvdbId':66}]
puts=[]
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,obj):
        b=json.dumps(obj).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        assert self.headers['X-Api-Key']=='k'
        u=urlparse(self.path)
        if u.path=='/api/v3/system/status': self._send({'version':'4.0.1'})
        elif u.path=='/api/v3/series': self._send(SERIES)
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


conn=app.get_db()
cols=[r[1] for r in conn.execute("PRAGMA table_info(shows)")]
print(cols)
def add(name,pp):
    conn.execute("INSERT INTO shows (show_name, vault_path, plex_path, episodes_per_drop, release_days, release_day) VALUES (?,?,?,?,?,?)",(name,pp,pp,1,'0',0))
add('Alpha','/media/A/TV100/Alpha'); add('Beta','/media/vault/Beta'); add('Dup','/media/A/TV100/Dup'); add('Nope','/media/A/TV100/Nope'); add('Gamma','/media/A/TV100/Gamma'); add('The Delta Show','/media/A/TV100/The Delta Show')
conn.commit(); conn.close()
c=app.app.test_client()
r=c.post('/api/test-sonarr',data={'sonarr_url':f'http://127.0.0.1:{srv.server_port}','sonarr_api_key':'k'})
d=r.get_json(); print(r.status_code,d['message'],d['problems'])
st={x['name']:x['state'] for x in d['report']['shows']}; print(st)
assert st=={'Alpha':'ok','Beta':'path-differs','Dup':'ambiguous','Nope':'none','Gamma':'path-differs','The Delta Show':'path-differs'}, st
print([x['detail'] for x in d['report']['shows']])
r=c.post('/api/test-sonarr',data={'sonarr_url':'http://127.0.0.1:1','sonarr_api_key':'k'}); print(r.status_code); assert r.status_code==502

creds={'sonarr_url':f'http://127.0.0.1:{srv.server_port}','sonarr_api_key':'k'}
r=c.post('/api/sonarr/repoint',data=creds); d=r.get_json(); print(r.status_code,[(p['name'],p['to']) for p in d['plan']])
assert not puts, 'dry run must not write'
names={p['name'] for p in d['plan']}; assert names=={'Beta','Gamma','The Delta Show'}, names
r=c.post('/api/sonarr/repoint',data=dict(creds,confirm='1')); print(r.status_code,r.get_json())
assert len(puts)==3 and all(q[1]=={'moveFiles':['false']} for q in puts), puts
r=c.post('/api/sonarr/repoint',data=creds); assert r.get_json()['plan']==[], r.get_json()
print('OK')
