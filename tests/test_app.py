import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from app import create_app
from tests.fakes import FakeCloud, FakeNotifier, SETUP, WEBHOOK, setup_copy

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud, self.notify = FakeCloud(), FakeNotifier()
        self.app = create_app(self.tmp.name, self.cloud, self.notify, scheduler=False)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.engine = self.app.extensions['engine']
        self.token = (Path(self.tmp.name) / 'setup.token').read_text()
        self.header = {'X-Setup-Token': self.token}
    def tearDown(self): self.tmp.cleanup()
    def setup(self):
        r = self.client.post('/api/setup', json=setup_copy(), headers=self.header)
        self.assertEqual(r.status_code, 200, r.data)
        self.csrf = self.client.get('/api/status').json['csrf']
        self.a = self.store.rows('accounts')[0]
        self.i = self.store.rows('instances')[0]
    def post(self, path, body=None, method='POST'):
        return self.client.open('/api'+path, method=method, json=body or {}, headers={'X-CSRF-Token': self.csrf})
    def enable(self):
        self.assertEqual(self.post('/settings', {'dry_run':False}, 'PUT').status_code,200)
    def test_01_setup_token_and_atomic_validation(self):
        self.assertEqual(self.client.post('/api/setup',json=setup_copy()).status_code,403)
        invalid=setup_copy(); invalid['instance']['traffic_limit']=999
        self.assertEqual(self.client.post('/api/setup',json=invalid,headers=self.header).status_code,400)
        self.assertEqual(self.store.rows('accounts'),[])
        self.assertFalse(self.store.meta('admin'))
        self.setup()
        self.assertEqual(self.client.post('/api/setup',json=setup_copy(),headers=self.header).status_code,409)
    def test_02_setup_tests_read_only(self):
        for kind in ('account','instance'):
            body={'kind':kind,'account':SETUP['account'],'instance':SETUP['instance']}
            self.assertEqual(self.client.post('/api/setup/test',json=body).status_code,403)
            r=self.client.post('/api/setup/test',json=body,headers=self.header)
            self.assertTrue(r.json['ok'],r.data)
        self.assertEqual(self.cloud.actions,[])
        self.assertFalse(self.store.meta('admin'))
    def test_03_required_and_invalid_values(self):
        for section,key,value in [('account','ak',''),('account','sk',''),('account','site','other'),('instance','region','香港'),('instance','instance_id','bad'),('instance','quota',0),('instance','traffic_limit','NaN'),('settings','interval',59),('settings','report_time','24:00'),('settings','timezone','Bad/Zone'),('admin','password','short')]:
            with self.subTest(section=section,key=key):
                body=setup_copy();body[section][key]=value
                self.assertEqual(self.client.post('/api/setup',json=body,headers=self.header).status_code,400)
    def test_04_login_csrf_origin_cookie(self):
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.setup()
        self.assertEqual(self.client.put('/api/settings',json={'dry_run':False}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={'Origin':'https://evil.test'}).status_code,403)
        r=self.post('/logout');self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':'bad'}).status_code,401)
        r=self.client.post('/api/login',json=SETUP['admin']);self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['Set-Cookie']);self.assertIn('SameSite=Strict',r.headers['Set-Cookie'])
    def test_05_login_rate_limit(self):
        self.setup();self.post('/logout')
        for n in range(10):self.assertEqual(self.client.post('/api/login',json={'username':'no','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json=SETUP['admin']).status_code,429)
    def test_06_encrypted_secrets_and_masked_export(self):
        self.setup()
        for secret in (SETUP['account']['ak'],SETUP['account']['sk']):
            self.assertNotIn(secret,self.client.get('/api/state').text)
            self.assertNotIn(secret,self.client.get('/api/export').text)
            self.assertNotIn(secret,Path(self.store.path).read_bytes().decode(errors='ignore'))
        self.assertEqual(self.store.account(self.a['id'])['sk'],SETUP['account']['sk'])
        self.post('/accounts/'+self.a['id'],{'name':'新备注','site':'china','ak':'','sk':''},'PUT')
        self.assertEqual(self.store.account(self.a['id'])['sk'],SETUP['account']['sk'])
    def test_07_instances_crud_duplicate_and_account_protection(self):
        self.setup()
        body={**SETUP['instance'],'account_id':self.a['id']}
        self.assertEqual(self.post('/instances',body).status_code,400)
        body['instance_id']='i-test002';self.assertEqual(self.post('/instances',body).status_code,200)
        self.assertEqual(self.post('/accounts/'+self.a['id'],method='DELETE').status_code,400)
        self.assertEqual(self.post('/instances/'+self.i['id'],{'paused':True},'PUT').status_code,200)
        self.assertEqual(self.post('/instances/'+self.i['id'],method='DELETE').status_code,200)
        self.assertEqual(len(self.store.rows('instances')),1)
    def test_08_default_dry_run_never_stops(self):
        self.setup();self.cloud.usage=190
        self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.actions,[])
        self.assertFalse(self.store.get('instances',self.i['id'])['stopped_by_monitor'])
    def test_09_stop_then_restore_and_restart_persistence(self):
        self.setup();self.enable();self.cloud.usage=190
        self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.actions,[('i-test001','StopInstance')])
        self.assertTrue(self.store.get('instances',self.i['id'])['stopped_by_monitor'])
        app2=create_app(self.tmp.name,self.cloud,self.notify,scheduler=False)
        self.cloud.usage=2
        app2.extensions['engine'].launch('check',background=False)
        self.assertEqual(self.cloud.actions[-1],('i-test001','StartInstance'))
        app2.extensions['engine'].launch('check',background=False)
        self.assertFalse(self.store.get('instances',self.i['id'])['stopped_by_monitor'])
    def test_10_manual_stopped_instance_never_starts(self):
        self.setup();self.enable();self.cloud.states['i-test001']='Stopped'
        self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.actions,[])
    def test_11_paused_instance_no_queries(self):
        self.setup();self.enable();self.post('/instances/'+self.i['id'],{'paused':True},'PUT')
        self.cloud.usage=190;self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.traffic_calls,0);self.assertEqual(self.cloud.actions,[])
    def test_12_query_failure_no_actions_and_redaction(self):
        self.setup();self.enable()
        self.cloud.error=ValueError('key=leak '+SETUP['account']['ak']+' '+SETUP['account']['sk'])
        j=self.engine.launch('check',background=False)
        self.assertEqual(self.client.get('/api/jobs/'+j).json['status'],'partial')
        self.assertEqual(self.cloud.actions,[])
        data=self.client.get('/api/state').text
        for secret in ('leak',SETUP['account']['ak'],SETUP['account']['sk']):self.assertNotIn(secret,data)
    def test_13_same_account_only_one_traffic_call(self):
        self.setup();body={**SETUP['instance'],'account_id':self.a['id'],'instance_id':'i-test002'}
        self.post('/instances',body);self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.traffic_calls,1)
    def test_14_notification_optional_success_and_failure(self):
        self.setup();self.engine.launch('report',background=False)
        self.assertEqual(self.notify.messages,[])
        self.assertEqual(self.post('/notifications/test',{'channel':'wecom','url':WEBHOOK}).status_code,200)
        self.assertEqual(len(self.notify.messages),1)
        self.notify.error=ValueError('connection '+WEBHOOK)
        r=self.post('/notifications/test',{'channel':'wecom','url':WEBHOOK})
        self.assertEqual(r.status_code,400);self.assertNotIn('test-not-real',r.text)
    def test_15_notification_settings_preserve_and_clear(self):
        self.setup()
        self.assertEqual(self.post('/settings',{'wecom_enabled':True,'wecom_url':WEBHOOK},'PUT').status_code,200)
        self.assertNotIn('test-not-real',self.client.get('/api/state').text)
        self.post('/settings',{'wecom_url':''},'PUT');self.assertEqual(self.store.settings(True)['wecom_url'],WEBHOOK)
        self.assertEqual(self.post('/settings',{'wecom_clear':True},'PUT').status_code,400)
        self.post('/settings',{'wecom_enabled':False,'wecom_clear':True},'PUT');self.assertEqual(self.store.settings(True)['wecom_url'],'')
    def test_16_failed_notifications_partial_and_cooldown(self):
        self.setup();self.post('/settings',{'wecom_enabled':True,'wecom_url':WEBHOOK},'PUT')
        self.cloud.usage=190;self.notify.error=ValueError('failed')
        j=self.engine.launch('report',background=False)
        self.assertEqual(self.client.get('/api/jobs/'+j).json['status'],'partial')
        self.assertFalse(self.store.get('instances',self.i['id']).get('overlimit_notified_at'))
        self.notify.error=None;self.engine.launch('check',background=False)
        count=len(self.notify.messages);self.engine.launch('check',background=False)
        self.assertEqual(len(self.notify.messages),count)
    def test_17_long_report_not_truncated(self):
        self.setup();self.post('/settings',{'wecom_enabled':True,'wecom_url':WEBHOOK},'PUT')
        for n in range(25):self.post('/instances',{**SETUP['instance'],'account_id':self.a['id'],'name':'较长实例备注'+str(n),'instance_id':'i-test'+str(n+2)})
        self.engine.launch('report',background=False)
        text=self.notify.messages[-1][1];self.assertGreater(len(text),1500);self.assertIn('较长实例备注24',text)
    def test_18_legacy_import_idempotent(self):
        self.setup();body={'users':[{**SETUP['account'],**SETUP['instance']}],'wecom':{'webhook_url':WEBHOOK,'mention_all':True}}
        for n in range(2):self.assertEqual(self.post('/import',body).status_code,200)
        self.assertEqual(len(self.store.rows('accounts')),1);self.assertEqual(len(self.store.rows('instances')),1)
        self.assertTrue(self.store.settings()['mention_all'])
    def test_19_import_invalid_no_partial_writes(self):
        self.setup();body={'users':[{**SETUP['account'],**SETUP['instance'],'ak':'new-key','sk':'new-secret'},{}]}
        self.assertEqual(self.post('/import',body).status_code,400);self.assertEqual(len(self.store.rows('accounts')),1)
    def test_20_backup_restore_password_sessions_and_recovery(self):
        self.setup()
        self.assertEqual(self.post('/backup',{'password':'wrong'}).status_code,400)
        backup=self.post('/backup',{'password':SETUP['admin']['password']});self.assertEqual(backup.status_code,200)
        self.post('/instances/'+self.i['id'],{'name':'改过的名称'},'PUT')
        r=self.client.post('/api/backup/restore',data={'password':SETUP['admin']['password'],'file':(io.BytesIO(backup.data),'backup.zip')},headers={'X-CSRF-Token':self.csrf})
        self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(self.store.rows('instances')[0]['name'],SETUP['instance']['name'])
        self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertTrue((Path(self.tmp.name)/'before-restore'/'monitor.db').exists())
        self.assertEqual(self.store.account(self.a['id'])['ak'],SETUP['account']['ak'])
    def test_21_malicious_backup_rejected_without_overwrite(self):
        self.setup();output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:z.writestr('../../escape','bad')
        r=self.client.post('/api/backup/restore',data={'password':SETUP['admin']['password'],'file':(io.BytesIO(output.getvalue()),'bad.zip')},headers={'X-CSRF-Token':self.csrf})
        self.assertEqual(r.status_code,400);self.assertEqual(self.store.rows('instances')[0]['name'],SETUP['instance']['name'])
    def test_22_password_change_revokes_session(self):
        self.setup();r=self.post('/password',{'old_password':SETUP['admin']['password'],'password':'Brand-new-test-password!'})
        self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/state').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':'Brand-new-test-password!'}).status_code,200)
    def test_23_busy_config_and_jobs_rejected(self):
        self.setup();self.engine.lock.acquire()
        try:
            self.assertEqual(self.post('/settings',{'dry_run':False},'PUT').status_code,409)
            self.assertEqual(self.post('/run',{'kind':'check'}).status_code,409)
        finally:self.engine.lock.release()
    def test_24_assets_headers_and_history(self):
        self.setup();self.engine.launch('check',background=False)
        self.assertEqual(len(self.client.get('/api/instances/'+self.i['id']+'/history').json['samples']),1)
        for path in ('/','/static/app.js','/static/style.css','/static/extras.css','/healthz'):
            r=self.client.get(path);self.assertEqual(r.status_code,200)
            self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
            r.close()
    def test_25_discovery_and_tests_never_act(self):
        self.setup();self.enable()
        self.assertTrue(self.client.get('/api/accounts/'+self.a['id']+'/discover?region=cn-hongkong').json['instances'])
        self.assertTrue(self.post('/accounts/test',{**SETUP['account'],'id':self.a['id']}).json['ok'])
        self.assertTrue(self.post('/instances/test',{**self.i}).json['ok']);self.assertEqual(self.cloud.actions,[])
    def test_26_action_failure_does_not_set_ownership(self):
        self.setup();self.enable();self.cloud.usage=190;self.cloud.action_error=ValueError('RAM denied')
        self.engine.launch('check',background=False)
        self.assertFalse(self.store.get('instances',self.i['id'])['stopped_by_monitor'])
        self.assertIn('RAM denied',self.store.get('instances',self.i['id'])['error'])
        self.assertIn('动作请求未确认',self.store.events()[1]['message'])
    def test_27_identity_change_resets_old_ownership(self):
        self.setup();self.i['stopped_by_monitor']=True;self.store.put('instances',self.i)
        self.post('/instances/'+self.i['id'],{'instance_id':'i-another'},'PUT')
        self.assertFalse(self.store.get('instances',self.i['id'])['stopped_by_monitor'])
    def test_28_auto_restore_disabled(self):
        self.setup();self.enable();self.i.update(stopped_by_monitor=True,auto_restore=False);self.store.put('instances',self.i)
        self.cloud.states['i-test001']='Stopped';self.engine.launch('check',background=False)
        self.assertEqual(self.cloud.actions,[])
    def test_29_failed_bill_marks_partial_and_clears_stale_amount(self):
        self.setup();self.engine.launch('report',background=False)
        self.assertIn('bill',self.store.get('accounts',self.a['id']))
        self.cloud.bill_error=ValueError('RAM denied')
        job=self.engine.launch('report',background=False)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['status'],'partial')
        self.assertNotIn('bill',self.store.get('accounts',self.a['id']))
    def test_30_https_proxy_origin_and_secure_cookie(self):
        with patch.dict('os.environ',{'TRUST_PROXY':'1'}):
            app=create_app(self.tmp.name,self.cloud,self.notify,scheduler=False)
        c=app.test_client()
        r=c.post('/api/setup',json=setup_copy(),base_url='http://monitor.example',
                 headers={**self.header,'Origin':'https://monitor.example','X-Forwarded-Proto':'https'})
        self.assertEqual(r.status_code,200,r.data);self.assertIn('Secure;',r.headers['Set-Cookie'])
    def test_31_scheduler_daily_report_and_interval(self):
        self.setup();self.post('/settings',{'scheduler_enabled':True,'report_time':'00:00'},'PUT')
        with patch.object(self.engine.stop,'wait',side_effect=[False,True]),patch.object(self.engine,'launch') as launch:
            self.engine.schedule();launch.assert_called_once_with('report')
        self.store.set_meta('last_check',0)
        with patch.object(self.engine.stop,'wait',side_effect=[False,True]),patch.object(self.engine,'launch') as launch:
            self.engine.schedule();launch.assert_called_once_with('check')
    def test_32_scheduler_disabled_no_jobs(self):
        self.setup()
        with patch.object(self.engine.stop,'wait',side_effect=[False,True]),patch.object(self.engine,'launch') as launch:
            self.engine.schedule();launch.assert_not_called()

if __name__=='__main__':unittest.main()
