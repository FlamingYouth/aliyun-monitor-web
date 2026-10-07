"""Seed an isolated Docker browser preview with clearly synthetic data only."""
from datetime import datetime, timedelta
import os
import time
from zoneinfo import ZoneInfo

from app import create_app, validate_account, validate_instance, validate_settings
from tests.fakes import FakeCloud, FakeNotifier, SETUP, WEBHOOK, setup_copy


def main():
    app = create_app(os.environ.get('DATA_DIR', '/data'), FakeCloud(), FakeNotifier(), scheduler=False)
    store, engine = app.extensions['store'], app.extensions['engine']
    if not store.meta('admin'):
        draft = setup_copy()
        draft['settings'].update(wecom_enabled=True, wecom_url=WEBHOOK)
        with app.test_client() as client:
            token = (store.root / 'setup.token').read_text()
            response = client.post('/api/setup', json=draft, headers={'X-Setup-Token': token})
            assert response.status_code == 200, response.data
    primary = store.rows('accounts')[0]
    first = store.rows('instances')[0]
    if len(store.rows('accounts')) == 1:
        second = validate_account({'name': '国际站测试账号', 'site': 'international',
                                   'ak': 'synthetic-international-ak', 'sk': 'synthetic-international-sk'}, store)
        store.put('accounts', second)
        item = validate_instance({'name': '国际站测试节点', 'account_id': second['id'], 'region': 'ap-southeast-1',
                                  'instance_id': 'i-synthetic002', 'traffic_limit': 80, 'quota': 100,
                                  'paused': True}, store.rows('accounts'))
        store.put('instances', item)
    now = time.time()
    clock = datetime.now(ZoneInfo('Asia/Shanghai'))
    month = clock.strftime('%Y-%m')
    with store.db() as db:
        db.execute('DELETE FROM samples')
    for item in store.rows('instances'):
        last = 68.42 if item['id'] == first['id'] else 12.80
        for index in range(144):
            at = now - (143 - index) * 300
            usage = last * (.65 + .35 * index / 143)
            with store.db() as db:
                db.execute('INSERT INTO samples(at,instance,traffic,status) VALUES (?,?,?,?)',
                           (at, item['id'], usage, 'Running' if not item['paused'] else 'Stopped'))
        store.put('instances', {**item, 'traffic': last, 'status': 'Running' if not item['paused'] else 'Stopped',
                                'ip': '203.0.113.10', 'checked_at': now, 'error': ''})
    for account in store.rows('accounts'):
        currency = 'CNY' if account['site'] == 'china' else 'USD'
        fees = [3.42, 0, 8.13, 6.40, 10.26, 11.59] if currency == 'CNY' else [1.10, 1.50, 0, 2.80, 1.90, 2.30]
        total = 0
        for day in range(1, clock.day):
            value = fees[(day - 1) % len(fees)]
            total += value
            store.save_daily_bill(account['id'], f'{month}-{day:02d}', {'amount': value, 'currency': currency})
        store.put('accounts', {**account, 'balance': {'amount': 268.65 if currency == 'CNY' else 86.20, 'currency': currency},
                               'bill': {'amount': round(total, 2), 'currency': currency}, 'bill_at': now})
    for offset in range(min(clock.day, 3)):
        stamp = (clock - timedelta(days=offset)).replace(hour=9, minute=0, second=0).timestamp()
        store.save_report('📊 阿里云监控日报（模拟数据）\n账号 CDT 用量：68.42 GB\n本月账号账单：CNY 39.80\n运行模式：只监控', {'wecom': True}, stamp)
    with store.db() as db:
        db.execute('DELETE FROM jobs')
        for index in range(5):
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                       (f'synthetic-job-{index}', 'report' if index == 4 else 'check', 'done', now - (4 - index) * 300, now, '{}'))
    store.set_meta('settings', validate_settings({'telegram_enabled': False,
                    'telegram_token': '123456789:synthetic_test_telegram_token_001', 'telegram_chat_id': '1234567890'}, store))
    store.prune_history()
    store.set_meta('last_check', now)
    print('Synthetic Docker preview seeded: two accounts / 144 samples / daily bills / three jobs / current-month reports')


if __name__ == '__main__':
    main()
