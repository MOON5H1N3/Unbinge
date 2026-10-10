import sys, types, os, tempfile, sqlite3
from datetime import date, timedelta
for m in ['apscheduler','apscheduler.schedulers','apscheduler.schedulers.background']:
    sys.modules[m]=types.ModuleType(m)
class _S:
    def __init__(self,*a,**k): pass
    def __getattr__(self,n): return lambda *a,**k: None
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler=_S
tmp=tempfile.mkdtemp()
os.environ.update(DB_PATH=os.path.join(tmp,'d.db'),POOL_DIR=os.path.join(tmp,'pool'),VAULT_DIR=os.path.join(tmp,'vault'),
                  CONFIG_DIR=tmp)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import app
app.init_db()
print("init ok")

# estimate_end_date
today=app.today_local()
wd=today.weekday()
d=app.estimate_end_date(3,1,str(wd))            # 3 drops on today's weekday -> today, +7, +14
assert d==today+timedelta(days=14),d
d=app.estimate_end_date(3,1,str(wd),ran_today=True)
assert d==today+timedelta(days=21),d
assert app.estimate_end_date(4,2,str(wd))==today+timedelta(days=7)
assert app.estimate_end_date(0,1,'1') is None
assert app.estimate_end_date(5,1,'') is None
print("estimate ok")

# cooldown wake-up
vault=os.path.join(tmp,'vault','Show'); plex=os.path.join(tmp,'plex','Show')
os.makedirs(vault); os.makedirs(plex)
conn=app.get_db()
cols=[r[1] for r in conn.execute(f"PRAGMA table_info({app.TABLE_NAME})")]
conn.execute(f"INSERT INTO {app.TABLE_NAME} (show_name,vault_path,plex_path,release_days,release_day,current_season,current_episode,episodes_per_drop,completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
  ('Show',vault,plex,'2','2',1,0,1,(app.now_local()-timedelta(days=2)).isoformat()))
conn.commit()
sent=[]
app.send_notification=lambda *a,**k: sent.append(a)
assert app.resume_cooled_down_shows(conn)==[]          # empty vault: stays in cooldown
open(os.path.join(vault,'Show.S01E01.mkv'),'w').write('x')
open(os.path.join(vault,'Show.S01E02.mkv'),'w').write('x')
res=app.resume_cooled_down_shows(conn)
assert res==[('Show',2)],res
row=conn.execute(f"SELECT completed_at FROM {app.TABLE_NAME}").fetchone()
assert row[0] is None
h=conn.execute(f"SELECT action,detail FROM {app.HISTORY_TABLE}").fetchall()
print([tuple(x) for x in h], len(sent))

# graduation grace
conn.execute(f"UPDATE {app.TABLE_NAME} SET completed_at=?",((app.now_local()-timedelta(days=2)).isoformat(),))
for f in os.listdir(vault): os.remove(os.path.join(vault,f))
conn.commit()
show=dict(conn.execute(f"SELECT * FROM {app.TABLE_NAME}").fetchone())
msg=app.process_show_drip(conn,show)
print(msg); assert 'cooling down until' in msg
assert conn.execute(f"SELECT COUNT(*) FROM {app.TABLE_NAME}").fetchone()[0]==1
conn.execute(f"UPDATE {app.TABLE_NAME} SET completed_at=?",((app.now_local()-timedelta(days=8)).isoformat(),))
conn.commit()
show=dict(conn.execute(f"SELECT * FROM {app.TABLE_NAME}").fetchone())
os.makedirs(app.POOL_DIR,exist_ok=True)
msg=app.process_show_drip(conn,show); print(msg)
assert 'graduated' in msg
print("ALL OK")
