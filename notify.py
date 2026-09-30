from collections import deque
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse, parse_qs

import requests


def validate_url(channel, value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("通知地址必须是完整 HTTPS 地址")
    if channel == "wecom":
        if parsed.hostname != "qyapi.weixin.qq.com" or parsed.path != "/cgi-bin/webhook/send" or not parse_qs(parsed.query).get("key"):
            raise ValueError("企业微信地址须为 qyapi.weixin.qq.com 的群机器人 Webhook")
        if parsed.port not in (None, 443):
            raise ValueError("企业微信 Webhook 端口不正确")
    elif channel == "bark":
        if not parsed.hostname or not parsed.path.strip("/") or parsed.query:
            raise ValueError("请输入包含设备推送密钥的 Bark HTTPS 地址")
        if parsed.hostname == "localhost":
            raise ValueError("Bark 地址须为公网 HTTPS 服务")
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            if not ip.is_global:
                raise ValueError("Bark 地址须为公网 HTTPS 服务")
    else:
        raise ValueError("未知通知渠道")


def chunks(text, limit=1900):
    result, current, size = [], "", 0
    for char in str(text):
        length = len(char.encode("utf-8"))
        if size + length > limit:
            result.append(current)
            current, size = "", 0
        current += char
        size += length
    if current:
        result.append(current)
    return result


class Notifier:
    def __init__(self):
        self.lock = threading.Lock()
        self.sent = deque()

    def send(self, channel, settings, message):
        url = settings[channel + "_url"]
        validate_url(channel, url)
        if channel == "bark":
            # Validate resolved addresses before contacting a custom Bark host.
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in socket.getaddrinfo(urlparse(url).hostname, 443)):
                raise ValueError("Bark 域名指向非公网地址")
        for index, chunk in enumerate(chunks(message)):
            with self.lock:
                now = time.time()
                while self.sent and self.sent[0] <= now - 60:
                    self.sent.popleft()
                if len(self.sent) >= 18:
                    raise ValueError("通知达到每分钟限额，部分分片未发送，请稍后重试")
                self.sent.append(now)
            if channel == "wecom":
                text = {"content": chunk}
                if settings.get("mention_all") and index == 0:
                    text["mentioned_list"] = ["@all"]
                payload = {"msgtype": "text", "text": text}
                response = requests.post(url, json=payload, timeout=(5, 10), allow_redirects=False)
                if response.status_code != 200:
                    raise ValueError(f"企业微信 HTTP {response.status_code}")
                data = response.json()
                if not isinstance(data, dict) or data.get("errcode") != 0:
                    raise ValueError(f"企业微信拒绝消息，错误码 {data.get('errcode') if isinstance(data, dict) else '未知'}")
            else:
                base, key = url.rstrip("/").rsplit("/", 1)
                response = requests.post(base + "/push", json={"device_key": key, "title": "阿里云监控", "body": chunk}, timeout=(5, 10), allow_redirects=False)
                if response.status_code != 200 or response.json().get("code") != 200:
                    raise ValueError("Bark 推送接口未确认成功")
        return True
