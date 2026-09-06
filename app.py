from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from datetime import datetime, timezone
import os, json, urllib.request, urllib.error, psycopg
from psycopg.rows import dict_row

app=FastAPI(title='UNG-INFRA-25',version='1.0.0')
DB=os.getenv('DATABASE_URL','')
JANUS_BASE_URL=os.getenv('JANUS_BASE_URL','https://ung-iam-production.up.railway.app').rstrip('/')

def conn(): return psycopg.connect(DB,row_factory=dict_row)
def auth(permission,authorization):
    if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'JANUS bearer token required')
    req=urllib.request.Request(JANUS_BASE_URL+'/v1/auth/introspect',data=b'',method='POST',headers={'Authorization':authorization})
    try:
        with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401,403): raise HTTPException(401,'JANUS token invalid or expired')
        raise HTTPException(503,'JANUS authorization unavailable')
    except Exception: raise HTTPException(503,'JANUS authorization unavailable')
    principal=data.get('principal') or {}; perms=set(principal.get('permissions') or [])
    if permission not in perms and 'ung.admin' not in perms: raise HTTPException(403,f'Missing JANUS permission: {permission}')
    return principal

@app.on_event('startup')
def init():
    if DB:
        with conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS infrastructure_assets(id UUID PRIMARY KEY,name TEXT,asset_type TEXT,site TEXT,status TEXT,criticality TEXT,owner_system TEXT,created_at TIMESTAMPTZ,updated_at TIMESTAMPTZ)')
            c.execute('CREATE TABLE IF NOT EXISTS infrastructure_checks(id UUID PRIMARY KEY,asset_id UUID,check_type TEXT,status TEXT,details TEXT,checked_at TIMESTAMPTZ)')
            c.execute('CREATE TABLE IF NOT EXISTS infrastructure_changes(id UUID PRIMARY KEY,asset_id UUID,change_type TEXT,description TEXT,status TEXT,requested_by TEXT,created_at TIMESTAMPTZ,completed_at TIMESTAMPTZ)')

class AssetIn(BaseModel): name:str; asset_type:str; site:str; criticality:str='normal'; owner_system:str='UNG-INFRA-25'
class CheckIn(BaseModel): asset_id:str; check_type:str; status:str; details:str=''
class ChangeIn(BaseModel): asset_id:str; change_type:str; description:str; requested_by:str='UNG-INFRA-25'

@app.get('/health')
def health(): return {'status':'ok','service':'UNG-INFRA-25','version':'1.0.0'}
@app.get('/ready')
def ready():
    try:
        with conn() as c:c.execute('SELECT 1')
        return {'status':'ready','database':'connected','janus':JANUS_BASE_URL}
    except Exception:return {'status':'degraded','database':'unavailable','janus':JANUS_BASE_URL}
@app.get('/v1/system')
def system(): return {'system_id':'UNG-INFRA-25','domain':'infrastructure-operations','capabilities':['asset-registry','health-checks','change-control','criticality','readiness','janus-auth']}
@app.get('/v1/assets')
def assets(authorization:str|None=Header(None)):
    auth('infra.assets.read',authorization)
    with conn() as c:return c.execute('SELECT * FROM infrastructure_assets ORDER BY updated_at DESC').fetchall()
@app.post('/v1/assets',status_code=201)
def create_asset(b:AssetIn,authorization:str|None=Header(None)):
    auth('infra.assets.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:return c.execute('INSERT INTO infrastructure_assets VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),b.name,b.asset_type,b.site,'active',b.criticality,b.owner_system,now,now)).fetchone()
@app.post('/v1/checks',status_code=201)
def create_check(b:CheckIn,authorization:str|None=Header(None)):
    auth('infra.checks.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        if not c.execute('SELECT id FROM infrastructure_assets WHERE id=%s',(b.asset_id,)).fetchone(): raise HTTPException(404,'asset_not_found')
        return c.execute('INSERT INTO infrastructure_checks VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),b.asset_id,b.check_type,b.status,b.details,now)).fetchone()
@app.get('/v1/checks')
def checks(authorization:str|None=Header(None)):
    auth('infra.checks.read',authorization)
    with conn() as c:return c.execute('SELECT * FROM infrastructure_checks ORDER BY checked_at DESC').fetchall()
@app.post('/v1/changes',status_code=201)
def request_change(b:ChangeIn,authorization:str|None=Header(None)):
    auth('infra.changes.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        if not c.execute('SELECT id FROM infrastructure_assets WHERE id=%s',(b.asset_id,)).fetchone(): raise HTTPException(404,'asset_not_found')
        return c.execute('INSERT INTO infrastructure_changes VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),b.asset_id,b.change_type,b.description,'requested',b.requested_by,now,None)).fetchone()
@app.post('/v1/changes/{change_id}/complete')
def complete_change(change_id:str,authorization:str|None=Header(None)):
    auth('infra.changes.write',authorization); now=datetime.now(timezone.utc)
    with conn() as c:
        row=c.execute("UPDATE infrastructure_changes SET status='completed',completed_at=%s WHERE id=%s RETURNING *",(now,change_id)).fetchone()
        if not row: raise HTTPException(404,'change_not_found')
        c.execute('UPDATE infrastructure_assets SET updated_at=%s WHERE id=%s',(now,row['asset_id'])); return row
@app.get('/v1/summary')
def summary(authorization:str|None=Header(None)):
    auth('infra.assets.read',authorization)
    with conn() as c:return {'assets':c.execute('SELECT COUNT(*) n FROM infrastructure_assets').fetchone()['n'],'critical':c.execute("SELECT COUNT(*) n FROM infrastructure_assets WHERE criticality='critical'").fetchone()['n'],'open_changes':c.execute("SELECT COUNT(*) n FROM infrastructure_changes WHERE status<>'completed'").fetchone()['n'],'failed_checks':c.execute("SELECT COUNT(*) n FROM infrastructure_checks WHERE status IN ('failed','down','degraded')").fetchone()['n']}
