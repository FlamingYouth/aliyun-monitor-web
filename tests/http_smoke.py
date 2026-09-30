"""Real HTTP smoke test. Synthetic credentials, no cloud calls unless MOCK_CLOUD=1."""
import json
import os
import time
import urllib.request
import http.cookiejar
from tests.fakes import SETUP, WEBHOOK, setup_copy

BASE=os.environ.get('TEST_URL','http://127.0.0.1:8080')
TOKEN=os.environ.get('TEST_SETUP_TOKEN','codex-preview-test-token')
jar=http.cookiejar.CookieJar()
http=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf=''
def call(path,body=None,method=None,setup=False):
    headers={}
    if body is not None:headers['Content-Type']='application/json'
    if csrf:headers['X-CSRF-Token']=csrf
    if setup:headers['X-Setup-Token']=TOKEN
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,
                               method=method,headers=headers)
    with http.open(req,timeout=60) as r:return json.load(r)
assert call('/healthz')['ok']
s=call('/api/status')
if s['setup_required']:assert call('/api/setup',setup_copy(),setup=True)['ok']
else:assert call('/api/login',SETUP['admin'])['ok']
csrf=call('/api/status')['csrf']
state=call('/api/state');assert len(state['accounts'])==1
assert state['settings']['dry_run'] is True
assert SETUP['account']['sk'] not in json.dumps(state)
i=state['instances'][0]
assert call('/api/instances/'+i['id'],{'paused':True},'PUT')['ok']
assert call('/api/instances/'+i['id'],{'paused':False},'PUT')['ok']
if os.environ.get('MOCK_CLOUD')=='1':
    assert call('/api/notifications/test',{'channel':'wecom','url':WEBHOOK})['ok']
    for kind in ('check','check','report'):
        job=call('/api/run',{'kind':kind})['job_id']
        for _ in range(50):
            result=call('/api/jobs/'+job)
            if result['status']!='running':break
            time.sleep(.1)
        assert result['status']=='done',result
    assert len(call('/api/instances/'+i['id']+'/history')['samples'])>=3
    assert call('/api/state')['last_report']['content']
assert call('/api/logout',{})['ok']
print('HTTP smoke PASS: setup/login/CSRF/state/CRUD/logout'+(' + jobs/history/report/test-send (mock services)' if os.environ.get('MOCK_CLOUD')=='1' else ' (no cloud calls)'))
