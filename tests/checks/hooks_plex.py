import sys, types, os, tempfile, time
for m in ['apscheduler','apscheduler.schedulers','apscheduler.schedulers.background']:
    sys.modules[m]=types.ModuleType(m)
class _S:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler=_S
tmp=tempfile.mkdtemp()
pool,vault,plex=[os.path.join(tmp,x) for x in('pool','vault','plex')]
os.makedirs(os.path.join(pool,'Show')); open(os.path.join(pool,'Show','Show.S01E01.mkv'),'w').write('x')
os.environ.update(DB_PATH=os.path.join(tmp,'d.db'),POOL_DIR=pool,VAULT_DIR=vault,PLEX_BASE_DIR=plex,CONFIG_DIR=tmp)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import app
app.init_db()
calls=[]
app.sync_sonarr_path_async=lambda *a: calls.append(a)
app.send_notification=lambda *a,**k: None
ok,msg,sid,code=app.promote_show_internal('Show','2',1); print(ok,msg)
assert ok and calls[-1][1:]==('Show',None,os.path.join(pool,'Show'),os.path.join(plex,'Show')),calls
c=app.app.test_client()
with c.session_transaction() as s: s['csrf_token']='t'; s['authenticated']=True; s['logged_in']=True
base=dict(show_name='Show',release_days='2',current_season='1',current_episode='0',episodes_per_drop='1',csrf_token='t')
# editing only the vault path must NOT touch sonarr
r=c.post(f'/edit/{sid}',data=dict(base,vault_path=os.path.join(tmp,'vault2','Show'),plex_path=os.path.join(plex,'Show')))
assert len(calls)==1,calls
# editing the plex path does
r=c.post(f'/edit/{sid}',data=dict(base,vault_path=os.path.join(tmp,'vault2','Show'),plex_path=os.path.join(tmp,'plex2','Show')))
assert len(calls)==2 and calls[-1][3]==os.path.join(plex,'Show') and calls[-1][4].endswith('plex2/Show'),calls
# delete: from the plex folder back to the pool
r=c.post(f'/delete/{sid}',data={'csrf_token':'t'})
assert calls[-1][1]=='Show' and calls[-1][3].endswith('plex2/Show') and calls[-1][4]==os.path.join(pool,'Show'),calls
print("PLEX HOOKS OK")
