import time

import pytest
from fastapi.testclient import TestClient

from radar.dashboard import Dashboard
from radar.domain import Config
from radar.web import create_app

PASSWORD = 'test-only-long-password'
ORIGIN = 'https://testserver'


@pytest.fixture
def setup(tmp_path):
    def collector(e,c,stop_event,on_health):
        on_health({'solana':{'ok':True,'at':int(time.time()),'observations':0}})
        stop_event.wait(5)
    m=Dashboard(tmp_path,Config(),collector=collector)
    with TestClient(create_app(m,PASSWORD,ORIGIN),base_url=ORIGIN) as client:
        yield m,client


def login(client):
    r=client.post('/api/login',json={'password':PASSWORD},headers={'Origin':ORIGIN})
    assert r.status_code==200
    csrf=client.get('/api/status').json()['csrf']
    return {'Origin':ORIGIN,'X-CSRF-Token':csrf}


def test_private_routes_require_auth(setup):
    m,c=setup
    assert c.get('/api/status').status_code==401
    assert c.get('/api/export/trades').status_code==401
    assert c.get('/',follow_redirects=False).status_code==303
    assert c.get('/healthz').json()=={'ok':True}
    assert c.get('/assets/../config.toml').status_code==404


def test_cookie_and_security_headers(setup):
    _,c=setup
    r=c.post('/api/login',json={'password':PASSWORD},headers={'Origin':ORIGIN})
    cookie=r.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    assert "frame-ancestors 'none'" in r.headers['content-security-policy']
    assert r.headers['cache-control']=='no-store'


def test_csrf_and_origin_rejected(setup):
    m,c=setup
    headers=login(c)
    assert c.post('/api/control/start',json={}).status_code==403
    assert c.post('/api/control/start',json={},headers={'Origin':ORIGIN}).status_code==403
    headers['Origin']='https://attacker.invalid'
    assert c.post('/api/control/start',json={},headers=headers).status_code==403
    assert not m.running()


def test_bad_password_throttled(setup):
    _,c=setup
    for _ in range(10):
        assert c.post('/api/login',json={'password':'wrong'},headers={'Origin':ORIGIN}).status_code==401
    assert c.post('/api/login',json={'password':PASSWORD},headers={'Origin':ORIGIN}).status_code==429


def test_controls_persist_and_demo_is_isolated(setup):
    m,c=setup
    headers=login(c)
    assert c.get('/api/status').json()['paused']
    before=c.get('/api/status').json()['observation_count']
    d=c.get('/api/status?demo=true').json()
    assert d['demo'] and d['accounts']['solana']['closed_trades']==2
    assert c.get('/api/status').json()['observation_count']==before
    assert c.post('/api/control/start',json={},headers=headers).status_code==200
    assert m.running()
    assert c.post('/api/control/pause',json={},headers=headers).status_code==200
    assert c.get('/api/status').json()['paused']
    assert c.post('/api/control/stop',json={},headers=headers).status_code==200
    m.thread.join(2)
    assert not m.running()
    assert m.meta['collect_requested'] is False


def test_new_experiment_preserves_previous_and_validates(setup):
    m,c=setup
    h=login(c)
    assert c.post('/api/experiments',json={'max_market_cap':900000},headers=h).status_code==409
    assert c.post('/api/experiments',json={'order_usd':'huge'},headers=h).status_code==409
    assert c.post('/api/experiments',json={'allow_fdv_proxy':'false'},headers=h).status_code==409
    r=c.post('/api/experiments',json={'order_usd':30,'strategy':'pullback'},headers=h)
    assert r.status_code==200
    assert m.meta['active']!='paper'
    assert c.get('/api/status?experiment=paper').json()['settings']['order_usd']==25
    assert c.get('/api/status').json()['settings']['order_usd']==30
    assert c.get('/api/status?experiment=../../config').status_code==400


def test_logout_invalidates_cookie(setup):
    _,c=setup
    h=login(c)
    old=c.cookies.get('radar_session')
    assert c.post('/api/logout',json={},headers=h).status_code==200
    c.cookies.set('radar_session',old)
    assert c.get('/api/status').status_code==401


def test_no_credentials_in_public_or_private_responses(setup):
    _,c=setup
    login(c)
    for route in ['/','/api/status','/api/status?demo=true','/assets/app.js']:
        assert PASSWORD not in c.get(route).text


def test_body_limit(setup):
    _,c=setup
    assert c.post('/api/login',content='x'*9000,headers={'Origin':ORIGIN}).status_code==413


def test_public_http_and_weak_password_fail(tmp_path):
    m=Dashboard(tmp_path,Config())
    with pytest.raises(ValueError): create_app(m,'short',ORIGIN)
    with pytest.raises(ValueError): create_app(m,PASSWORD,'http://public.example')


def test_restart_loads_config_and_pause_state(tmp_path):
    m=Dashboard(tmp_path,Config())
    m.new_experiment({'order_usd':35})
    restored=Dashboard(tmp_path,Config())
    assert restored.config.order_usd==35
    assert restored.status()['paused']


def test_session_expires(setup, monkeypatch):
    _,c=setup
    login(c)
    future=time.time()+43201
    monkeypatch.setattr('radar.web.time.time',lambda:future)
    assert c.get('/api/status').status_code==401


def test_cannot_stop_collection_with_open_positions(setup):
    m,c=setup
    h=login(c)
    e=m.engine()
    state=e.state()
    state['positions']['solana:test']={'chain':'solana','token':'test','units':1,'cost':25}
    with e.conn:e._save(state)
    e.close()
    assert c.post('/api/control/stop',json={},headers=h).status_code==409


def test_fractional_position_limit_rejected(setup):
    _,c=setup
    h=login(c)
    assert c.post('/api/experiments',json={'max_positions_per_chain':2.5},headers=h).status_code==409
