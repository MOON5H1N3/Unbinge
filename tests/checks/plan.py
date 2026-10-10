import sys, types, os, tempfile
from datetime import timedelta
for m in ['apscheduler','apscheduler.schedulers','apscheduler.schedulers.background']:
    sys.modules[m]=types.ModuleType(m)
class _S:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler=_S
tmp=tempfile.mkdtemp()
pool,vault,plex=[os.path.join(tmp,x) for x in('pool','vault','plex')]
def mk(root,name,n):
    d=os.path.join(root,name,'Season 01'); os.makedirs(d)
    for i in range(1,n+1): open(os.path.join(d,f'{name}.S01E{i:02d}.mkv'),'w').write('x')
mk(pool,'Big',24); mk(pool,'Tiny',2)
os.environ.update(DB_PATH=os.path.join(tmp,'d.db'),POOL_DIR=pool,VAULT_DIR=vault,PLEX_BASE_DIR=plex,CONFIG_DIR=tmp)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import app
app.init_db()
app.send_notification=lambda *a,**k: None
today=app.today_local()
load={d:0 for d in range(7)}

# --- pure planner: 24 eps, thu only, 1/drop -> ~24 weeks. target 10 weeks out
wd=3
tgt=today+timedelta(weeks=10)
opts=app.build_plan_options(24,tgt,[wd],1,load)
for o in opts: print(o['key'],o['days'],o['per_drop'],o['end'])
assert {o['key'] for o in opts}>={'per_drop','days'}
for o in opts: assert o['end']<=tgt
pd=[o for o in opts if o['key']=='per_drop'][0]; assert pd['days']==[wd] and pd['per_drop']==3   # ceil(24/3)=8 weeks
dy=[o for o in opts if o['key']=='days'][0]; assert dy['per_drop']==1 and len(dy['days'])>1
# spread: second day should be far from thursday (gap 3)
assert min(app._circular_gap(a,b) for a in dy['days'] for b in dy['days'] if a!=b)>=2
# already on track -> single 'hold'
far=today+timedelta(weeks=40)
h=app.build_plan_options(24,far,[wd],1,load); assert [o['key'] for o in h]==['hold'],h
# impossible with 'days' (tomorrow, 24 eps, 1/drop) but per_drop works only if first drop <= target
print("planner ok")

# --- API
c=app.app.test_client()
with c.session_transaction() as s: s['authenticated']=True; s['logged_in']=True; s['csrf_token']='t'
r=c.get(f'/api/plan-options?show=Big&target={tgt.isoformat()}&days={wd}&per_drop=1'); j=r.get_json(); print(r.status_code,j['remaining'],[o['key'] for o in j['options']])
assert r.status_code==200 and j['remaining']==24
assert c.get('/api/plan-options?show=Big&target=2000-01-01&days=3&per_drop=1').status_code==400
assert c.get(f'/api/plan-options?show=Big&target={tgt.isoformat()}&days=&per_drop=1').status_code==400
assert c.get(f'/api/plan-options?show=../etc&target={tgt.isoformat()}&days=3').status_code==400
print(c.get(f'/api/plan-options?show=Big&target={today.isoformat()}&days={(today.weekday()+3)%7}&per_drop=1').get_json())

# --- promote with a target (balanced style)
ok,msg,sid,code=app.promote_show_internal('Big',str(wd),1,tgt.isoformat(),'per_drop'); assert ok,msg
conn=app.get_db(); row=dict(conn.execute(f"SELECT * FROM {app.TABLE_NAME} WHERE id=?",(sid,)).fetchone())
assert row['target_end_date']==tgt.isoformat() and row['plan_mode']=='per_drop'
# on track at 1/drop? 24 weeks > 10 weeks -> behind (promote set per_drop=1 deliberately) -> replan tightens per_drop only
ch=app.replan_target_shows(conn); print(ch)
row=dict(conn.execute(f"SELECT * FROM {app.TABLE_NAME} WHERE id=?",(sid,)).fetchone())
assert row['episodes_per_drop']==3 and row['release_days']==str(wd),row
end=app.estimate_end_date(24,3,str(wd)); assert end<=tgt
assert app.replan_target_shows(conn)==[]          # already on track -> no churn
print([tuple(x) for x in conn.execute(f"SELECT action,detail FROM {app.HISTORY_TABLE}")])
# more episodes arrive -> tightens again, never relaxes
mk(vault,'Big',0) if False else None
d=os.path.join(vault,'Big','Season 02'); os.makedirs(d)
for i in range(1,13): open(os.path.join(d,f'Big.S02E{i:02d}.mkv'),'w').write('x')
ch=app.replan_target_shows(conn); print(ch); assert ch and conn.execute(f"SELECT episodes_per_drop FROM {app.TABLE_NAME}").fetchone()[0]>3
# paused -> skipped
conn.execute(f"UPDATE {app.TABLE_NAME} SET paused=1, episodes_per_drop=1"); conn.commit()
assert app.replan_target_shows(conn)==[]
# no date given on promote -> behaves as before
ok,msg,sid2,_=app.promote_show_internal('Tiny','1',1); assert ok
r2=dict(conn.execute(f"SELECT target_end_date,plan_mode FROM {app.TABLE_NAME} WHERE id=?",(sid2,)).fetchone()); assert r2=={'target_end_date':None,'plan_mode':None},r2
# edit page without the field keeps the target; with blank clears it
form=dict(show_name='Big',vault_path=row['vault_path'],plex_path=row['plex_path'],release_days='3',current_season='1',current_episode='0',episodes_per_drop='1',csrf_token='t')
c.post(f'/edit/{sid}',data=form)
assert conn.execute(f"SELECT target_end_date FROM {app.TABLE_NAME} WHERE id=?",(sid,)).fetchone()[0]==tgt.isoformat()
c.post(f'/edit/{sid}',data={**form,'target_end_date':'','plan_mode':''})
assert conn.execute(f"SELECT target_end_date FROM {app.TABLE_NAME} WHERE id=?",(sid,)).fetchone()[0] is None
c.post(f'/edit/{sid}',data={**form,'target_end_date':tgt.isoformat(),'plan_mode':'days'})
assert tuple(conn.execute(f"SELECT target_end_date,plan_mode FROM {app.TABLE_NAME} WHERE id=?",(sid,)).fetchone())==(tgt.isoformat(),'days')
print("ALL OK")
