from collections import deque
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urlparse, parse_qs

import requests


CHANNELS = ("wecom", "bark", "telegram")


def validate_proxy(value):
    if not isinstance(value, str):
        raise ValueError("Telegram 代理须为 HTTP 或 SOCKS5 地址，或留空直连")
    if not value:
        return
    parsed = urlparse(value)
    if (parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Telegram 代理格式应为 http://127.0.0.1:7897 或 socks5h://127.0.0.1:7897")
    try:
        if not parsed.port or any(char.isspace() for char in value):
            raise ValueError
    except ValueError:
        raise ValueError("Telegram 代理端口不正确")


def validate_telegram(token, chat_id, proxy=""):
    if not isinstance(token, str) or not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("请输入有效的 Telegram Bot Token")
    if not isinstance(chat_id, str) or not re.fullmatch(r"-?[0-9]{1,20}", chat_id) or int(chat_id) == 0:
        raise ValueError("Telegram 接收目标须为数字 Chat ID；私聊用户名不能代替 ID")
    validate_proxy(proxy)


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
        if channel == "telegram":
            token, chat_id = settings["telegram_token"], settings["telegram_chat_id"]
            proxy = settings.get("telegram_proxy") or ""
            validate_telegram(token, chat_id, proxy)
            url = "https://api.telegram.org/bot" + token + "/sendMessage"
        else:
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
            if channel == "telegram":
                try:
                    response = requests.post(url, json={"chat_id": chat_id, "text": chunk},
                                             proxies={"http": proxy, "https": proxy},
                                             timeout=(5, 15), allow_redirects=False)
                    data = response.json()
                except requests.RequestException as error:
                    raise ValueError("Telegram 连接失败，请检查代理、网络和 Bot Token") from error
                except ValueError as error:
                    raise ValueError("Telegram 未返回有效的确认数据") from error
                if response.status_code != 200 or not isinstance(data, dict) or data.get("ok") is not True:
                    code = data.get("error_code", response.status_code) if isinstance(data, dict) else response.status_code
                    raise ValueError(f"Telegram 未确认发送成功，错误码 {code}；请检查 Token、Chat ID 和机器人私聊权限")
            elif channel == "wecom":
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
