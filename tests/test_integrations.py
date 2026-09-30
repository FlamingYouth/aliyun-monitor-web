import json
import unittest
from unittest.mock import Mock, patch
from cloud import Cloud
from notify import Notifier, chunks, validate_url
from tests.fakes import SETUP, WEBHOOK

class NotificationTests(unittest.TestCase):
    def setUp(self):self.settings={'wecom_url':WEBHOOK,'mention_all':True};self.n=Notifier()
    def test_chunks_preserve_multibyte_content(self):
        text='中文😀'*3000; parts=chunks(text)
        self.assertEqual(''.join(parts),text);self.assertTrue(all(len(p.encode())<=1900 for p in parts))
    def test_only_explicit_success_accepted(self):
        for code,value in [(500,{'errcode':0}),(200,{'errcode':40058}),(200,{}),(302,{'errcode':0}),(200,[])]:
            with self.subTest(code=code,value=value),patch('notify.requests.post',return_value=Mock(status_code=code,json=lambda:value)):
                with self.assertRaises(Exception):self.n.send('wecom',self.settings,'测试')
        with patch('notify.requests.post',return_value=Mock(status_code=200,json=lambda:{'errcode':0})) as post:
            self.n.send('wecom',self.settings,'成功')
            self.assertEqual(post.call_args.kwargs['json']['text']['mentioned_list'],['@all'])
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
    def test_non_json_200_not_success(self):
        response=Mock(status_code=200);response.json.side_effect=ValueError('HTML')
        with patch('notify.requests.post',return_value=response),self.assertRaises(ValueError):self.n.send('wecom',self.settings,'测试')
    def test_splitting_and_rate_limit(self):
        with patch('notify.requests.post',return_value=Mock(status_code=200,json=lambda:{'errcode':0})) as post:
            self.n.send('wecom',self.settings,'😀'*1000)
            self.assertGreater(post.call_count,1)
            self.assertNotIn('mentioned_list',post.call_args.kwargs['json']['text'])
            for n in range(18-post.call_count):self.n.send('wecom',self.settings,'短消息')
            with self.assertRaises(ValueError):self.n.send('wecom',self.settings,'限额')
    def test_webhook_validation_and_bark_private_address(self):
        for channel,url in [('wecom','http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x'),('wecom','https://evil.test/cgi-bin/webhook/send?key=x'),('wecom','https://qyapi.weixin.qq.com/cgi-bin/webhook/send'),('bark','https://127.0.0.1/key'),('bark','https://localhost/key'),('bark','https://192.168.0.1/key'),('bark','https://[::1]/key')]:
            with self.subTest(url=url),self.assertRaises(ValueError):validate_url(channel,url)
        with patch('notify.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),self.assertRaises(ValueError):
            self.n.send('bark',{'bark_url':'https://public.test/key'},'测试')
    def test_bark_success_and_fail(self):
        with patch('notify.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]):
            for code in (200,500):
                with patch('notify.requests.post',return_value=Mock(status_code=200,json=lambda:{'code':code})) as post:
                    if code==200:self.n.send('bark',{'bark_url':'https://api.day.app/test-device'},'测试')
                    else:
                        with self.assertRaises(ValueError):self.n.send('bark',{'bark_url':'https://api.day.app/test-device'},'测试')
                    self.assertEqual(post.call_args.args[0],'https://api.day.app/push')

class CloudTests(unittest.TestCase):
    def setUp(self):self.c=Cloud();self.a=SETUP['account'];self.i=SETUP['instance']
    def test_traffic_units_invalid_and_missing(self):
        with patch.object(self.c,'request',return_value={'TrafficDetails':[{'Traffic':1024**3},{'Traffic':2*1024**3}]}):self.assertEqual(self.c.traffic(self.a),3)
        for data in ({},{'TrafficDetails':None},{'TrafficDetails':[{'Traffic':-1}]},{'TrafficDetails':[{'Traffic':'NaN'}]}):
            with self.subTest(data=data),patch.object(self.c,'request',return_value=data),self.assertRaises(Exception):self.c.traffic(self.a)
    def test_instance_filter_and_region(self):
        data={'Instances':{'Instance':[{'InstanceId':self.i['instance_id'],'Status':'Running'}]}}
        with patch.object(self.c,'request',return_value=data) as req:
            self.assertEqual(self.c.status(self.a,self.i)['status'],'Running')
            self.assertEqual(json.loads(req.call_args.args[5]['InstanceIds']),[self.i['instance_id']])
        with patch.object(self.c,'request',return_value={'Instances':{'Instance':[]}}),self.assertRaises(ValueError):self.c.status(self.a,self.i)
    def test_billing_endpoint_and_finite_data(self):
        with patch.object(self.c,'request',return_value={'Success':True,'Data':{'AvailableAmount':'1,234.5','Currency':'USD'}}) as req:
            self.assertEqual(self.c.balance({**self.a,'site':'international'})['amount'],1234.5)
            self.assertEqual(req.call_args.args[2],'business.ap-southeast-1.aliyuncs.com')
        with patch.object(self.c,'request',return_value={'Success':True,'Data':{'Items':{'Item':[{'PretaxAmount':'NaN'}]}}}),self.assertRaises(ValueError):self.c.bill(self.a,'2026-09')
    def test_action_requires_acknowledgement(self):
        with patch.object(self.c,'request',return_value={}),self.assertRaises(ValueError):self.c.action(self.a,self.i,'StopInstance')
    def test_real_sdk_request_configuration_no_network(self):
        with patch('cloud.AcsClient') as cls:
            cls.return_value.do_action_with_exception.return_value=b'{"RequestId":"fake"}'
            self.c.action(self.a,self.i,'StopInstance')
            self.assertEqual(cls.call_args.kwargs['connect_timeout'],5)
            self.assertEqual(cls.call_args.kwargs['timeout'],15)
            self.assertFalse(cls.call_args.kwargs['auto_retry'])
            req=cls.return_value.do_action_with_exception.call_args.args[0]
            self.assertEqual(req.get_protocol_type(),'https')
            self.assertEqual(req.get_query_params()['InstanceId'],self.i['instance_id'])

if __name__=='__main__':unittest.main()
