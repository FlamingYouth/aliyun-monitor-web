"""Regression coverage for charts, Telegram, retention, and older deployments."""
from datetime import datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from zoneinfo import ZoneInfo

from app import create_app
from cloud import Cloud
from notify import Notifier, validate_proxy, validate_telegram
from store import Store
from tests.fakes import FakeCloud, FakeNotifier, SETUP, WEBHOOK, setup_copy

TOKEN = '123456789:synthetic_test_telegram_token_001'
CHAT = '1234567890'


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud, self.notifier = FakeCloud(), FakeNotifier()
        self.app = create_app(self.tmp.name, self.cloud, self.notifier, scheduler=False)
        self.client = self.app.test_client()
        self.store, self.engine = self.app.extensions['store'], self.app.extensions['engine']
        token = (Path(self.tmp.name) / 'setup.token').read_text()
        response = self.client.post('/api/setup', json=setup_copy(), headers={'X-Setup-Token': token})
        self.assertEqual(response.status_code, 200)
        self.csrf = self.client.get('/api/status').json['csrf']

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, body=None, method='POST'):
        return self.client.open('/api' + path, json=body or {}, method=method,
                                headers={'X-CSRF-Token': self.csrf})

    def telegram(self):
        return self.post('/settings', {'telegram_enabled': True, 'telegram_token': TOKEN,
                                      'telegram_chat_id': CHAT, 'telegram_proxy': 'http://127.0.0.1:7897'}, 'PUT')

    def test_telegram_secret_preserved_encrypted_and_clearable(self):
        self.assertEqual(self.telegram().status_code, 200)
        state = self.client.get('/api/state').json
        self.assertTrue(state['settings']['telegram_token_configured'])
        self.assertEqual(state['settings']['telegram_token'], '')
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertNotIn(TOKEN, self.client.get('/api/export').get_data(as_text=True))
        self.assertNotIn(TOKEN, json.dumps(self.store.meta('settings')))
        self.assertEqual(self.post('/settings', {'interval': 600}, 'PUT').status_code, 200)
        self.assertEqual(self.store.settings(True)['telegram_token'], TOKEN)
        self.assertEqual(self.post('/settings', {'telegram_clear': True}, 'PUT').status_code, 400)
        self.assertEqual(self.post('/settings', {'telegram_enabled': False, 'telegram_clear': True}, 'PUT').status_code, 200)
        self.assertFalse(self.store.settings()['telegram_token_configured'])

    def test_multiple_channels_continue_after_one_channel_fails(self):
        self.assertEqual(self.telegram().status_code, 200)
        self.assertEqual(self.post('/settings', {'wecom_enabled': True, 'wecom_url': WEBHOOK,
                                               'bark_enabled': True, 'bark_url': 'https://api.day.app/synthetic-device-key'}, 'PUT').status_code, 200)
        def deliver(channel, settings, message):
            if channel == 'wecom':
                raise ValueError('synthetic failure')
            return True
        with patch.object(self.notifier, 'send', side_effect=deliver) as send:
            self.assertEqual(self.engine.notify('test'), {'wecom': False, 'bark': True, 'telegram': True})
            self.assertEqual([call.args[0] for call in send.call_args_list], ['wecom', 'bark', 'telegram'])

    def test_telegram_test_uses_unsaved_values_and_redacts_failures(self):
        payload = {'channel': 'telegram', 'token': TOKEN, 'chat_id': CHAT, 'proxy': ''}
        self.assertEqual(self.post('/notifications/test', payload).status_code, 200)
        self.assertFalse(self.store.settings()['telegram_token_configured'])
        self.notifier.error = ValueError('failed https://api.telegram.org/bot' + TOKEN + '/sendMessage')
        response = self.post('/notifications/test', payload)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(TOKEN, response.get_data(as_text=True))
        self.assertEqual(self.post('/notifications/test', {**payload, 'chat_id': '@synthetic_user'}).status_code, 400)

    def test_latest_three_jobs_and_independent_daily_archive(self):
        month = self.store.month()
        for day in range(1, 7):
            stamp = datetime.fromisoformat(f'{month}-{day:02d}T12:00:00+08:00').timestamp()
            self.store.save_report(f'daily report {day}', {}, stamp)
        for _ in range(6):
            self.engine.launch('check', background=False)
        state = self.client.get('/api/state').json
        self.assertEqual(len(state['jobs']), 3)
        self.assertEqual(len(state['reports']), 6)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 3)
        self.assertEqual(self.client.get('/api/reports/' + month + '-01').json['content'], 'daily report 1')

    def test_rollover_uses_calendar_month_and_configured_timezone(self):
        before = datetime(2028, 2, 29, 23, 59, 58, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        after = before + 3
        # Advancing the wall clock must not expire the authenticated test session.
        with self.store.db() as db:
            db.execute('UPDATE sessions SET expires=?', (after + 600,))
        with patch('store.time.time', return_value=before):
            self.store.save_report('February report', {})
            self.store.save_daily_bill('account', '2028-02-29', {'amount': 1, 'currency': 'CNY'})
            self.assertEqual(len(self.store.reports()), 1)
        with patch('store.time.time', return_value=after):
            state = self.client.get('/api/state').json
            self.assertEqual(state['report_month'], '2028-03')
            self.assertEqual(state['reports'], [])
            self.assertIsNone(state['last_report'])
            self.assertEqual(self.store.daily_bills('account', '2028-02'), [])
            self.store.save_report('March report', {})
            self.assertEqual(self.store.reports()[0]['day'], '2028-03-01')
        # At the same instant, UTC still belongs to the previous month.
        settings = self.store.meta('settings')
        self.store.set_meta('settings', {**settings, 'timezone': 'UTC'})
        self.assertEqual(self.store.month(after), '2028-02')

    def test_account_chart_does_not_depend_on_first_instance_or_double_count(self):
        original = self.store.rows('instances')[0]
        self.store.put('instances', {**original, 'id': 'second', 'instance_id': 'i-test002', 'paused': True})
        import time
        base = int(time.time() / 60) * 60 - 60
        with self.store.db() as db:
            for at, instance, usage in [(base - 60, 'second', 68), (base - 55, original['id'], 68),
                                        (base, 'second', 73), (base + 5, original['id'], 73)]:
                db.execute('INSERT INTO samples(at,instance,traffic,status) VALUES (?,?,?,?)', (at, instance, usage, 'Running'))
        response = self.client.get('/api/dashboard/charts')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['traffic'] for row in self.store.account_samples(original['account_id'])], [68, 73])
        self.assertEqual([row['traffic'] for row in response.json['accounts'][0]['samples']], [73])
        self.assertNotIn('ak', response.json['accounts'][0])

    def test_manual_daily_traffic_keeps_raw_samples_and_today_updates(self):
        import time
        original = self.store.rows('instances')[0]
        now = time.time()
        clock = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
        today = clock.strftime('%Y-%m-%d')
        with self.store.db() as db:
            db.execute('INSERT INTO samples(at,instance,traffic,status) VALUES (?,?,?,?)',
                       (now - 60, original['id'], 21, 'Running'))
        self.store.set_meta('traffic_history', {original['account_id']: {today: {'at': now, 'traffic': 20.5}}})
        self.assertEqual(self.store.account_daily_samples(original['account_id'])[-1]['traffic'], 20.5)
        with patch('store.time.time', return_value=now + 60):
            self.store.sample(original['id'], 24.5, 'Running')
            self.assertEqual(self.store.account_daily_samples(original['account_id'])[-1]['traffic'], 24.5)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 2)

    def test_today_bill_refreshes_and_pending_response_preserves_value(self):
        clock = datetime.now(ZoneInfo('Asia/Shanghai'))
        account = self.store.rows('accounts')[0]
        day = clock.strftime('%Y-%m-%d')
        self.store.save_daily_bill(account['id'], day, {'amount': 0.02, 'currency': 'USD'})
        with patch.object(self.cloud, 'daily_bill', return_value={'amount': 0, 'currency': 'USD', 'has_entries': False}):
            self.engine.report()
        self.assertEqual(self.store.daily_bills(account['id'], clock.strftime('%Y-%m'))[-1]['amount'], 0.02)
        with patch.object(self.cloud, 'daily_bill', return_value={'amount': 0.03, 'currency': 'USD', 'has_entries': True}):
            self.engine.report()
        self.assertEqual(self.store.daily_bills(account['id'], clock.strftime('%Y-%m'))[-1]['amount'], 0.03)

    def test_daily_bill_backfill_cache_preserves_original_monthly_total(self):
        frozen = datetime.now(ZoneInfo('Asia/Shanghai')).replace(day=7)
        with patch('engine.datetime') as clock, patch.object(self.cloud, 'daily_bill', wraps=self.cloud.daily_bill) as daily:
            clock.now.return_value = frozen
            self.engine.report()
            self.assertEqual(daily.call_count, 7)
            daily.reset_mock()
            self.engine.report()
            self.assertEqual([call.args[1][-2:] for call in daily.call_args_list], ['04', '05', '06', '07'])
        account = self.store.rows('accounts')[0]
        self.assertEqual(account['bill']['amount'], 39.80)
        self.assertEqual(len(self.store.daily_bills(account['id'], frozen.strftime('%Y-%m'))), 7)
        self.assertEqual(self.cloud.actions, [])

    def test_restore_old_backup_creates_new_tables_and_migrates_report(self):
        self.store.set_meta('last_report', {'at': __import__('time').time(), 'content': 'legacy daily report', 'delivery': {}})
        backup = self.post('/backup', {'password': SETUP['admin']['password']}).data
        with zipfile.ZipFile(io.BytesIO(backup)) as archive:
            manifest, key, data = archive.read('manifest.json'), archive.read('secret.key'), archive.read('monitor.db')
        db_path = Path(self.tmp.name) / 'legacy.db'
        db_path.write_bytes(data)
        with sqlite3.connect(db_path) as db:
            db.execute('DROP TABLE reports')
            db.execute('DROP TABLE daily_bills')
        for version in ('1.0.0', '1.1.0', '1.10', '1.20'):
            with self.subTest(version=version):
                output = io.BytesIO()
                old_manifest = {**json.loads(manifest), 'version': version}
                with zipfile.ZipFile(output, 'w') as archive:
                    archive.writestr('manifest.json', json.dumps(old_manifest))
                    archive.writestr('secret.key', key)
                    archive.writestr('monitor.db', db_path.read_bytes())
                output.seek(0)
                response = self.client.post('/api/backup/restore', data={'password': SETUP['admin']['password'],
                                            'file': (output, 'legacy.zip')}, headers={'X-CSRF-Token': self.csrf})
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(self.post('/login', SETUP['admin']).status_code, 200)
                self.csrf = self.client.get('/api/status').json['csrf']
                self.assertEqual(self.client.get('/api/state').json['reports'][0]['day'], self.store.month() + '-' + datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%d'))
                self.assertEqual(self.client.get('/api/dashboard/charts').status_code, 200)


class TelegramAndBillingTests(unittest.TestCase):
    def test_proxy_file_environment_and_web_override(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'notification-config.json'
            config.write_text(json.dumps({'telegram_proxy': 'http://127.0.0.1:7897'}))
            with patch.dict('os.environ', {'NOTIFICATION_CONFIG': str(config)}):
                store = Store(Path(root) / 'data')
                self.assertEqual(store.settings()['telegram_proxy'], 'http://127.0.0.1:7897')
                with patch.dict('os.environ', {'TELEGRAM_PROXY': 'http://host.docker.internal:7897'}):
                    override = Store(Path(root) / 'data')
                    self.assertEqual(override.settings()['telegram_proxy'], 'http://host.docker.internal:7897')
                store.set_meta('settings', {**store.meta('settings'), 'telegram_proxy': ''})
                self.assertEqual(store.settings()['telegram_proxy'], '')

    def test_telegram_send_proxy_payload_explicit_success_and_failure(self):
        settings = {'telegram_token': TOKEN, 'telegram_chat_id': CHAT, 'telegram_proxy': 'http://127.0.0.1:7897'}
        notifier = Notifier()
        with patch('notify.requests.post', return_value=Mock(status_code=200, json=lambda: {'ok': True})) as post:
            self.assertTrue(notifier.send('telegram', settings, '连接测试'))
            self.assertEqual(post.call_args.kwargs['json'], {'chat_id': CHAT, 'text': '连接测试'})
            self.assertEqual(post.call_args.kwargs['proxies'], {'http': settings['telegram_proxy'], 'https': settings['telegram_proxy']})
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
        for status, data in [(200, {'ok': False}), (403, {'ok': False, 'error_code': 403}), (200, []), (302, {'ok': True})]:
            with self.subTest(status=status, data=data), patch('notify.requests.post', return_value=Mock(status_code=status, json=lambda: data)):
                with self.assertRaises(ValueError):
                    notifier.send('telegram', settings, 'test')

    def test_invalid_proxy_and_personal_username_rejected(self):
        validate_telegram(TOKEN, CHAT, 'http://127.0.0.1:7897')
        validate_proxy('socks5h://127.0.0.1:7897')
        for proxy in ['ftp://127.0.0.1:7897', 'http://127.0.0.1', 'http://host:99999', 'http://user:pass@host:7897', 'http://host:7897/path']:
            with self.subTest(proxy=proxy), self.assertRaises(ValueError):
                validate_proxy(proxy)
        with self.assertRaises(ValueError):
            validate_telegram(TOKEN, '@synthetic_user', '')

    def test_daily_bill_pagination_currency_endpoint_and_finite_values(self):
        cloud = Cloud()
        account = {'site': 'international'}
        page = {'Success': True, 'Data': {'Items': {'Item': [{'PretaxAmount': '1.5', 'Currency': 'USD'}] * 300}, 'TotalCount': 301}}
        final = {'Success': True, 'Data': {'Items': {'Item': [{'PretaxAmount': '-1', 'Currency': 'USD'}]}, 'TotalCount': 301}}
        with patch.object(cloud, 'request', side_effect=[page, final]) as request:
            self.assertEqual(cloud.daily_bill(account, '2026-10-01')['amount'], 449)
            self.assertEqual(request.call_args.args[2], 'business.ap-southeast-1.aliyuncs.com')
            self.assertEqual(request.call_args.args[-1]['Granularity'], 'DAILY')
            self.assertEqual(request.call_args.args[-1]['BillingDate'], '2026-10-01')
            self.assertEqual(request.call_args.args[-1]['PageNum'], 2)
        with patch.object(cloud, 'request', return_value={'Success': True, 'Data': {'Items': {'Item': []}, 'TotalCount': 0}}):
            self.assertFalse(cloud.daily_bill(account, '2026-10-07')['has_entries'])
        for items in [[{'PretaxAmount': 'NaN'}], [{'PretaxAmount': '1', 'Currency': 'CNY'}, {'PretaxAmount': '1', 'Currency': 'USD'}]]:
            with self.subTest(items=items), patch.object(cloud, 'request', return_value={'Success': True, 'Data': {'Items': {'Item': items}}}), self.assertRaises(ValueError):
                cloud.daily_bill(account, '2026-10-01')


if __name__ == '__main__':
    unittest.main()
