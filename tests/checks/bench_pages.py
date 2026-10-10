import sys, types, os, tempfile, time, cProfile, pstats, io
for m in ['apscheduler','apscheduler.schedulers','apscheduler.schedulers.background']:
    sys.modules[m]=types.ModuleType(m)
class _S:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler=_S
tmp=tempfile.mkdtemp()
pool,vault,plex=[os.path.join(tmp,x) for x in('pool','vault','plex')]
N=int(os.environ.get('N','30')); EPS=int(os.environ.get('EPS','40'))
os.makedirs(pool)
os.environ.update(DB_PATH=os.path.join(tmp,'d.db'),POOL_DIR=pool,VAULT_DIR=vault,PLEX_BASE_DIR=plex,CONFIG_DIR=tmp)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import app
app.init_db()
app.send_notification=lambda *a,**k: None
app.sync_sonarr_path_async=lambda *a,**k: None
app.sync_discord_schedule_message_async=lambda *a,**k: None
for i in range(N):
    d=os.path.join(pool,f'Show {i}')
    for s in (1,2):
        os.makedirs(os.path.join(d,f'Season {s:02d}'))
        for e in range(1,EPS//2+1):
            open(os.path.join(d,f'Season {s:02d}',f'Show {i} - S{s:02d}E{e:02d}.mkv'),'w').write('x')
    ok,msg,sid,code=app.promote_show_internal(f'Show {i}',str(i%7),1)
    assert ok,msg
c=app.app.test_client()
with c.session_transaction() as s: s['csrf_token']='t'; s['authenticated']=True; s['logged_in']=True
print(f'{N} shows x {EPS} eps')
for path in ['/','/schedule','/stats','/history']:
    t=time.time(); r=c.get(path); print(f'{path:10} {r.status_code} {time.time()-t:.3f}s')
t=time.time(); app.load_shows(); print(f'load_shows {time.time()-t:.3f}s')
pr=cProfile.Profile(); pr.enable(); c.get('/'); pr.disable()
st=io.StringIO(); pstats.Stats(pr,stream=st).sort_stats('cumulative').print_stats(14); print(st.getvalue()[:2600])
