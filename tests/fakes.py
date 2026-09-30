import copy

SETUP = {
    'admin': {'username': 'admin', 'password': 'Test-only-password-2026!'},
    'account': {'name': '香港主账号', 'site': 'china', 'ak': 'test-ak-never-real', 'sk': 'test-sk-never-real'},
    'instance': {'name': '香港节点', 'region': 'cn-hongkong', 'instance_id': 'i-test001', 'traffic_limit': 180,
                 'quota': 200, 'auto_restore': True, 'paused': False},
    'settings': {'interval': 300, 'report_time': '09:00', 'timezone': 'Asia/Shanghai', 'dry_run': True,
                 'scheduler_enabled': False, 'wecom_enabled': False, 'bark_enabled': False}}
WEBHOOK = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-not-real'

class FakeCloud:
    def __init__(self):
        self.usage = 68.42
        self.states = {'i-test001': 'Running'}
        self.actions = []
        self.traffic_calls = 0
        self.error = None
        self.action_error = None
        self.bill_error = None
        self.metadata = {}
        self.action_params = []

    def traffic(self, account):
        self.traffic_calls += 1
        if self.error: raise self.error
        return self.usage

    def status(self, account, instance):
        if self.error: raise self.error
        return {'status': self.states.get(instance['instance_id'], 'Running'), 'ip': '203.0.113.10',
                **self.metadata.get(instance['instance_id'], {})}

    def action(self, account, instance, action):
        if self.action_error: raise self.action_error
        self.actions.append((instance['instance_id'], action))
        self.action_params.append({'action': action, 'stop_mode': 'KeepCharging' if action == 'StopInstance' else None})
        self.states[instance['instance_id']] = 'Stopped' if action == 'StopInstance' else 'Running'
        return {'RequestId': 'test-request'}

    def balance(self, account):
        if self.error: raise self.error
        return {'amount': 268.65, 'currency': 'CNY'}

    def bill(self, account, month):
        if self.bill_error: raise self.bill_error
        return {'amount': 39.80, 'currency': 'CNY', 'scope': 'account'}

    def discover(self, account, region, resgroup=''):
        return [{'instance_id': 'i-test002', 'name': '备用节点', 'status': 'Stopped'}]

class FakeNotifier:
    def __init__(self): self.messages, self.error = [], None
    def send(self, channel, settings, message):
        if self.error: raise self.error
        self.messages.append((channel, message))
        return True

def setup_copy(): return copy.deepcopy(SETUP)
