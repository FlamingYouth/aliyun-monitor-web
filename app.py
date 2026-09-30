from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from flask import Flask, jsonify, request, send_file, make_response
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from cloud import Cloud
from engine import BusyError, Engine
from notify import Notifier, validate_url
from store import Store, DEFAULTS

VERSION = "1.1.0"


def required(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label}不能为空，且不能超过 {maximum} 字符")
    return value.strip()


def positive(value, label, minimum=1, maximum=1000000):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须为数字")
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label}须在 {minimum} 到 {maximum} 之间")
    return number


def bool_field(data, key, default=False):
    value = data.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} 必须为布尔值")
    return value


def validate_admin(data):
    username = required(data.get("username"), "管理员用户名", 32)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("用户名需为 3–32 位字母、数字、下划线或短横线")
    password = required(data.get("password"), "管理员密码", 128)
    if len(password) < 12:
        raise ValueError("管理员密码至少需要 12 个字符")
    return {"username": username, "password_hash": generate_password_hash(password, method="pbkdf2:sha256:600000")}


def validate_account(data, store, existing=None):
    old = existing or {}
    ak = data.get("ak") or (store.unseal(old["ak"]) if old else "")
    sk = data.get("sk") or (store.unseal(old["sk"]) if old else "")
    ak, sk = required(ak, "AccessKey ID", 256), required(sk, "AccessKey Secret", 256)
    if any(ch.isspace() for ch in ak + sk):
        raise ValueError("AccessKey 中不能有空格或换行")
    site = data.get("site", old.get("site"))
    if site not in ("china", "international"):
        raise ValueError("请选择国内站或国际站")
    return {**old, "id": old.get("id", uuid.uuid4().hex),
            "name": required(data.get("name", old.get("name")), "账号备注", 80),
            "site": site, "ak": store.seal(ak), "sk": store.seal(sk)}


def validate_instance(data, accounts, existing=None):
    old = existing or {}
    merged = {**old, **data}
    account_id = merged.get("account_id")
    if account_id not in {a["id"] for a in accounts}:
        raise ValueError("请选择有效的阿里云账号")
    region = required(merged.get("region"), "区域", 60)
    instance_id = required(merged.get("instance_id"), "ECS 实例 ID", 80)
    if not re.fullmatch(r"[a-z][a-z0-9-]+", region) or not re.fullmatch(r"i-[A-Za-z0-9]+", instance_id):
        raise ValueError("区域代码或实例 ID 格式不正确")
    limit = positive(merged.get("traffic_limit"), "关机阈值", .01)
    quota = positive(merged.get("quota"), "流量额度", .01)
    if limit > quota:
        raise ValueError("关机阈值不能高于流量额度")
    resgroup = merged.get("resgroup", "")
    if not isinstance(resgroup, str) or len(resgroup) > 80:
        raise ValueError("资源组格式不正确")
    return {**old, "id": old.get("id", uuid.uuid4().hex), "account_id": account_id,
            "name": required(merged.get("name"), "实例备注", 80), "region": region, "instance_id": instance_id,
            "traffic_limit": limit, "quota": quota, "resgroup": resgroup.strip(),
            "paused": bool_field(merged, "paused"), "auto_restore": bool_field(merged, "auto_restore", True),
            "spot_auto_restore": bool_field(merged, "spot_auto_restore", False),
            "stopped_by_monitor": old.get("stopped_by_monitor", False)}


def validate_settings(data, store):
    old = store.settings(True)
    out = {**old, **{key: value for key, value in data.items() if key in DEFAULTS or key == "scheduler_enabled"}}
    for key in ("wecom_enabled", "bark_enabled", "mention_all", "dry_run", "scheduler_enabled"):
        out[key] = bool_field(out, key, True if key in ("dry_run", "scheduler_enabled") else False)
    interval = positive(out["interval"], "巡检间隔", 60, 3600)
    if interval != int(interval):
        raise ValueError("巡检间隔必须为整数秒")
    out["interval"] = int(interval)
    if not isinstance(out["report_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", out["report_time"]):
        raise ValueError("日报时间格式须为 HH:MM")
    try:
        ZoneInfo(out["timezone"])
    except Exception:
        raise ValueError("请选择有效时区")
    for channel in ("wecom", "bark"):
        key = channel + "_url"
        value = data.get(key) or old[key]
        if data.get(channel + "_clear"):
            value = ""
        if out[channel + "_enabled"]:
            validate_url(channel, required(value, "通知地址", 1000))
        elif value:
            validate_url(channel, value)
        out[key] = store.seal(value)
        out.pop(key + "_configured", None)
    return out


def create_app(data_dir=None, cloud=None, notifier=None, scheduler=True):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    # Opt-in only behind a controlled reverse proxy. Never trust forwarded client IPs.
    if os.environ.get("TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    store = Store(data_dir or os.environ.get("DATA_DIR", "/data"))
    store.set_meta("version", VERSION)
    engine = Engine(store, cloud or Cloud(), notifier or Notifier())
    app.extensions.update(store=store, engine=engine)
    token_path = store.root / "setup.token"
    if not token_path.exists():
        token = os.environ.get("SETUP_TOKEN") or secrets.token_urlsafe(24)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token)
    bootstrap_token = token_path.read_text().strip()
    attempts, attempts_lock = {}, threading.Lock()

    def session():
        raw = request.cookies.get("am_session", "")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with store.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE token=? AND expires>?", (digest, time.time())).fetchone()
        return dict(row) if row else None

    def new_session(response):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), csrf, time.time() + 43200))
        response.set_cookie("am_session", raw, httponly=True, samesite="Strict", secure=request.is_secure, max_age=43200)
        return response

    @contextmanager
    def mutation():
        if not engine.lock.acquire(blocking=False):
            raise BusyError("巡检任务运行中，请完成后再修改配置")
        try:
            yield
        finally:
            engine.lock.release()

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("请提交有效 JSON 对象")
        return value

    @app.before_request
    def guard():
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                return jsonify(error="拒绝跨站请求"), 403
            if request.path not in ("/api/status", "/api/setup", "/api/setup/test", "/api/login"):
                current = session()
                if not current:
                    return jsonify(error="请先登录"), 401
                if request.method != "GET" and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), current["csrf"]):
                    return jsonify(error="会话校验失败，请刷新页面"), 403

    @app.after_request
    def security_headers(response):
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=store.redact(error)), 400

    @app.errorhandler(BusyError)
    def busy(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="上传文件过大，最多 12 MB"), 413

    @app.errorhandler(Exception)
    def unexpected(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return error
        store.event("error", "web", f"请求失败：{error}")
        return jsonify(error="操作未完成，请查看事件日志"), 500

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/healthz")
    def health():
        with store.db() as db:
            db.execute("SELECT 1").fetchone()
        return jsonify(ok=True, version=VERSION)

    @app.get("/api/status")
    def status():
        current = session()
        return jsonify(setup_required=not bool(store.meta("admin")), authenticated=bool(current),
                       csrf=current["csrf"] if current else "", version=VERSION)

    @app.post("/api/setup")
    def setup():
        with mutation():
            if store.meta("admin"):
                return jsonify(error="初始化已完成"), 409
            if not hmac.compare_digest(request.headers.get("X-Setup-Token", ""), bootstrap_token):
                return jsonify(error="初始化口令不正确，请查看部署时显示的口令"), 403
            body = data()
            admin = validate_admin(body.get("admin", {}))
            account = validate_account(body.get("account", {}), store)
            instance_data = {**body.get("instance", {}), "account_id": account["id"]}
            instance = validate_instance(instance_data, [account])
            settings = validate_settings(body.get("settings", {}), store)
            store.write_config([account], [instance], {"settings": settings, "last_check": time.time(), "admin": admin})
            store.event("info", "setup", "首次配置已完成")
        return new_session(jsonify(ok=True))

    @app.post("/api/setup/test")
    def setup_test():
        if store.meta("admin"):
            return jsonify(error="初始化已完成，请登录后测试"), 409
        if not hmac.compare_digest(request.headers.get("X-Setup-Token", ""), bootstrap_token):
            return jsonify(error="初始化口令不正确"), 403
        body = data()
        kind = body.get("kind")
        if kind == "notification":
            channel = body.get("channel", "wecom")
            if channel not in ("wecom", "bark"):
                raise ValueError("未知通知渠道")
            url = required(body.get("url"), "通知地址", 1000)
            validate_url(channel, url)
            settings = store.settings(True)
            settings[channel + "_url"] = url
            settings["mention_all"] = bool_field(body, "mention_all")
            try:
                engine.notifier.send(channel, settings, "✅ 云巡初始化向导测试\n这是一条主动发送的测试消息。")
            except Exception as error:
                raise ValueError(store.redact(str(error).replace(url, "[通知地址已隐藏]")))
            return jsonify(ok=True, message="推送接口已确认发送成功")
        if kind not in ("account", "instance"):
            raise ValueError("未知测试类型")
        account = validate_account(body.get("account", {}), store)
        clear = {**account, "ak": store.unseal(account["ak"]), "sk": store.unseal(account["sk"])}
        try:
            traffic = engine.cloud.traffic(clear)
            if kind == "account":
                balance = engine.cloud.balance(clear)
                return jsonify(ok=True, message=f"CDT 查询与余额查询通过，当前账号流量 {traffic:.2f} GB", balance=balance)
            instance = validate_instance({**body.get("instance", {}), "account_id": account["id"]}, [account])
            snapshot = engine.cloud.status(clear, instance)
            return jsonify(ok=True, message=f"实例查询通过，当前状态 {snapshot['status']}，账号流量 {traffic:.2f} GB", **snapshot)
        except Exception as error:
            raise ValueError(store.redact(str(error).replace(clear["ak"], "[已隐藏]").replace(clear["sk"], "[已隐藏]")))

    @app.post("/api/login")
    def login():
        body = data()
        ip = request.remote_addr
        with attempts_lock:
            values = [t for t in attempts.get(ip, []) if t > time.time() - 300]
            if len(values) >= 10:
                return jsonify(error="登录尝试过多，请 5 分钟后重试"), 429
            attempts[ip] = values + [time.time()]
        admin = store.meta("admin", {})
        if not admin or body.get("username") != admin["username"] or not check_password_hash(admin["password_hash"], str(body.get("password", ""))):
            return jsonify(error="用户名或密码错误"), 401
        with attempts_lock:
            attempts.pop(ip, None)
        return new_session(jsonify(ok=True))

    @app.post("/api/logout")
    def logout():
        with store.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (session()["token"],))
        response = jsonify(ok=True)
        response.delete_cookie("am_session")
        return response

    @app.get("/api/state")
    def state():
        accounts = []
        for original in store.rows("accounts"):
            item = {key: value for key, value in original.items() if key not in ("ak", "sk")}
            ak = store.unseal(original["ak"])
            item["ak_mask"] = ak[:3] + "••••" + ak[-3:] if len(ak) >= 8 else "••••"
            accounts.append(item)
        with store.db() as db:
            jobs = [dict(row) for row in db.execute("SELECT id,kind,status,at,finished FROM jobs ORDER BY at DESC LIMIT 8")]
        return jsonify(accounts=accounts, instances=store.rows("instances"), settings=store.settings(),
                       events=store.events(), jobs=jobs, busy=engine.lock.locked(), last_check=store.meta("last_check"),
                       last_report=store.meta("last_report"), username=store.meta("admin")["username"], version=VERSION,
                       test_mode=app.config.get("TESTING", False))

    @app.route("/api/accounts", methods=["POST"])
    @app.route("/api/accounts/<item_id>", methods=["PUT", "DELETE"])
    def accounts(item_id=None):
        with mutation():
            old = store.get("accounts", item_id) if item_id else None
            if item_id and not old:
                return jsonify(error="账号不存在"), 404
            if request.method == "DELETE":
                if any(i["account_id"] == item_id for i in store.rows("instances")):
                    raise ValueError("请先移除或转移该账号关联的实例")
                store.delete("accounts", item_id)
            else:
                item = validate_account(data(), store, old)
                store.put("accounts", item)
            store.event("info", "config", "账号配置已更新")
        return jsonify(ok=True)

    @app.post("/api/accounts/test")
    def test_account():
        body = data()
        old = store.get("accounts", body.get("id")) if body.get("id") else None
        item = validate_account(body, store, old)
        clear = {**item, "ak": store.unseal(item["ak"]), "sk": store.unseal(item["sk"])}
        results = []
        for label, call in (("CDT 流量权限", lambda: engine.cloud.traffic(clear)), ("账单余额权限", lambda: engine.cloud.balance(clear))):
            try:
                call()
                results.append({"name": label, "ok": True, "message": "查询通过"})
            except Exception as error:
                # Unsaved credentials must also be removed from error messages.
                message = str(error).replace(clear["ak"], "[已隐藏]").replace(clear["sk"], "[已隐藏]")
                results.append({"name": label, "ok": False, "message": store.redact(message)})
        return jsonify(results=results, ok=all(row["ok"] for row in results))

    @app.get("/api/accounts/<account_id>/discover")
    def discover(account_id):
        account = store.account(account_id)
        if not account:
            raise ValueError("账号不存在")
        region = required(request.args.get("region"), "区域", 60)
        if not re.fullmatch(r"[a-z][a-z0-9-]+", region):
            raise ValueError("区域代码不正确")
        return jsonify(instances=engine.cloud.discover(account, region, request.args.get("resgroup", "")))

    @app.route("/api/instances", methods=["POST"])
    @app.route("/api/instances/<item_id>", methods=["PUT", "DELETE"])
    def instances(item_id=None):
        with mutation():
            old = store.get("instances", item_id) if item_id else None
            if item_id and not old:
                return jsonify(error="实例不存在"), 404
            if request.method == "DELETE":
                store.delete("instances", item_id)
            else:
                item = validate_instance(data(), store.rows("accounts"), old)
                if any(i["id"] != item["id"] and i["account_id"] == item["account_id"] and i["instance_id"] == item["instance_id"] for i in store.rows("instances")):
                    raise ValueError("这个账号下的实例已经在监控列表中")
                if old and any(item[key] != old[key] for key in ("account_id", "instance_id", "region")):
                    item = {key: value for key, value in item.items() if key in ("id", "account_id", "instance_id", "region", "name", "traffic_limit", "quota", "resgroup", "paused", "auto_restore", "spot_auto_restore")}
                    item["stopped_by_monitor"] = False
                store.put("instances", item)
            store.event("info", "config", "实例配置已更新", item_id or "")
        return jsonify(ok=True)

    @app.post("/api/instances/test")
    def test_instance():
        body = data()
        item = validate_instance(body, store.rows("accounts"), store.get("instances", body.get("id")))
        account = store.account(item["account_id"])
        return jsonify(ok=True, **engine.cloud.status(account, item), traffic=engine.cloud.traffic(account))

    @app.post("/api/instances/<item_id>/control")
    def control_instance(item_id):
        body = data()
        with mutation():
            item = store.get("instances", item_id)
            if not item:
                return jsonify(error="监控实例不存在"), 404
            if body.get("confirmed") is not True or body.get("confirm_instance_id") != item["instance_id"]:
                raise ValueError("请明确确认操作，并输入完整 ECS 实例 ID")
            return jsonify(ok=True, **engine.manual_control(item_id, body.get("action")))

    @app.get("/api/instances/<item_id>/history")
    def history(item_id):
        return jsonify(samples=store.samples(item_id))

    @app.route("/api/settings", methods=["PUT"])
    def settings():
        with mutation():
            store.set_meta("settings", validate_settings(data(), store))
            store.event("info", "config", "调度与通知配置已更新")
        return jsonify(ok=True)

    @app.post("/api/notifications/test")
    def test_notification():
        body = data()
        channel = body.get("channel", "wecom")
        if channel not in ("wecom", "bark"):
            raise ValueError("未知通知渠道")
        settings = store.settings(True)
        url = body.get("url") or settings[channel + "_url"]
        validate_url(channel, required(url, "通知地址", 1000))
        settings[channel + "_url"] = url
        settings["mention_all"] = bool_field(body, "mention_all")
        try:
            engine.notifier.send(channel, settings, "✅ 阿里云监控连接测试成功\n这是一条网页主动发送的测试消息。")
        except Exception as error:
            safe = str(error).replace(url, "[通知地址已隐藏]")
            raise ValueError(store.redact(safe))
        store.event("info", "notify", f"{channel} 测试消息发送成功")
        return jsonify(ok=True, message="推送接口已确认发送成功")

    @app.post("/api/run")
    def run():
        return jsonify(job_id=engine.launch(data().get("kind", "check"))), 202

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        with store.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return jsonify(error="任务不存在"), 404
        value = dict(row)
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return jsonify(value)

    @app.post("/api/password")
    def password():
        body = data()
        admin = store.meta("admin")
        if not check_password_hash(admin["password_hash"], str(body.get("old_password", ""))):
            raise ValueError("原密码不正确")
        new = validate_admin({"username": admin["username"], "password": body.get("password")})
        store.set_meta("admin", new)
        with store.db() as db:
            db.execute("DELETE FROM sessions")
        store.event("info", "security", "管理员密码已更改，所有会话已退出")
        response = jsonify(ok=True)
        response.delete_cookie("am_session")
        return response

    @app.get("/api/export")
    def export():
        safe_accounts = [{"id": a["id"], "name": a["name"], "site": a["site"]} for a in store.rows("accounts")]
        instances = [{k: i.get(k, False if k == "spot_auto_restore" else "") for k in ("id", "name", "account_id", "region", "instance_id", "traffic_limit", "quota", "resgroup", "paused", "auto_restore", "spot_auto_restore")} for i in store.rows("instances")]
        response = jsonify(format="aliyun-monitor-web", version=VERSION, accounts=safe_accounts, instances=instances, settings=store.settings())
        response.headers["Content-Disposition"] = 'attachment; filename="monitor-config-without-secrets.json"'
        return response

    @app.post("/api/import")
    def import_config():
        with mutation():
            legacy = data()
            users = legacy.get("users")
            if not isinstance(users, list) or not 1 <= len(users) <= 100:
                raise ValueError("请导入原脚本 config.json，users 须含 1–100 条实例配置")
            accounts_map, new_accounts, new_instances = {}, [], []
            for account in store.rows("accounts"):
                accounts_map[(store.unseal(account["ak"]), store.unseal(account["sk"]))] = account
            known_instances = {(i["account_id"], i["instance_id"]): i for i in store.rows("instances")}
            for user in users:
                if not isinstance(user, dict):
                    raise ValueError("users 格式不正确")
                key = (required(user.get("ak"), "AccessKey ID", 256), required(user.get("sk"), "AccessKey Secret", 256))
                if key not in accounts_map:
                    account = validate_account({"name": user.get("name") or "导入账号", "ak": user.get("ak"), "sk": user.get("sk"),
                                                "site": "international" if "ap-southeast-1" in user.get("bill_endpoint", "") else "china"}, store)
                    new_accounts.append(account)
                    accounts_map[key] = account
                account = accounts_map[key]
                limit = user.get("traffic_limit", 180)
                instance = validate_instance({"name": user.get("name") or user.get("instance_id"), "account_id": account["id"],
                                              "region": user.get("region"), "instance_id": user.get("instance_id"), "traffic_limit": limit,
                                              "quota": max(float(user.get("quota", 200)), float(limit)), "resgroup": user.get("resgroup", ""),
                                              "paused": bool_field(user, "paused") or bool_field(user, "disabled"), "auto_restore": True},
                                             list(accounts_map.values()), known_instances.get((account["id"], user.get("instance_id"))))
                new_instances.append(instance)
                known_instances[(account["id"], instance["instance_id"])] = instance
            notification = {}
            for channel in ("wecom", "bark"):
                old_url = legacy.get(channel, {}).get("webhook_url" if channel == "wecom" else "bark_url", "")
                if old_url:
                    notification[channel + "_url"] = old_url
                    notification[channel + "_enabled"] = True
            notification["mention_all"] = bool_field(legacy.get("wecom", {}), "mention_all")
            settings = validate_settings(notification, store)
            store.write_config(new_accounts, new_instances, {"settings": settings})
            store.event("info", "import", f"已导入 {len(new_accounts)} 个账号、{len(new_instances)} 个实例；原 cron 请手动停用以免重复控制")
        return jsonify(ok=True, accounts=len(new_accounts), instances=len(new_instances))

    @app.post("/api/backup")
    def backup():
        if not check_password_hash(store.meta("admin")["password_hash"], str(data().get("password", ""))):
            raise ValueError("管理员密码不正确")
        with mutation(), tempfile.TemporaryDirectory(dir=store.root) as scratch:
            path = Path(scratch) / "monitor.db"
            with store.db() as db:
                target = sqlite3.connect(path)
                db.backup(target)
                target.close()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(path, "monitor.db")
                archive.write(store.root / "secret.key", "secret.key")
                archive.writestr("manifest.json", json.dumps({"format": "aliyun-monitor-web", "version": VERSION}))
        output.seek(0)
        return send_file(output, mimetype="application/zip", as_attachment=True, download_name="aliyun-monitor-backup.zip")

    @app.post("/api/backup/restore")
    def restore():
        if not check_password_hash(store.meta("admin")["password_hash"], request.form.get("password", "")):
            raise ValueError("管理员密码不正确")
        upload = request.files.get("file")
        if not upload:
            raise ValueError("请上传本应用导出的备份 ZIP")
        with mutation(), tempfile.TemporaryDirectory(dir=store.root) as scratch:
            try:
                with zipfile.ZipFile(upload.stream) as archive:
                    if sorted(archive.namelist()) != ["manifest.json", "monitor.db", "secret.key"] or sum(i.file_size for i in archive.infolist()) > 30 * 1024 * 1024:
                        raise ValueError("备份文件内容不符合要求")
                    manifest = json.loads(archive.read("manifest.json"))
                    if manifest.get("format") != "aliyun-monitor-web" or manifest.get("version") not in ("1.0.0", VERSION):
                        raise ValueError("备份格式或版本不匹配")
                    key = archive.read("secret.key")
                    cipher = Fernet(key)
                    path = Path(scratch) / "restore.db"
                    path.write_bytes(archive.read("monitor.db"))
                check = sqlite3.connect(path)
                try:
                    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("备份数据库损坏")
                    settings_row = check.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
                    for column in ("wecom_url", "bark_url"):
                        secret = json.loads(settings_row[0]).get(column)
                        if secret:
                            cipher.decrypt(secret.encode())
                    for payload, in check.execute("SELECT payload FROM accounts"):
                        for column in ("ak", "sk"):
                            cipher.decrypt(json.loads(payload)[column].encode())
                    check.execute("DELETE FROM sessions")
                    check.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
                    check.commit()
                finally:
                    check.close()
            except ValueError:
                raise
            except Exception:
                raise ValueError("无法验证备份，请确认 ZIP 来自本应用且未损坏")
            # Keep a recoverable pre-restore copy.
            recovery = store.root / "before-restore"
            recovery.mkdir(exist_ok=True, mode=0o700)
            with store.db() as db:
                target = sqlite3.connect(recovery / "monitor.db")
                db.backup(target)
                target.close()
            shutil.copy2(store.root / "secret.key", recovery / "secret.key")
            with store.lock:
                with store.db() as db:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                for suffix in ("-wal", "-shm"):
                    Path(str(store.path) + suffix).unlink(missing_ok=True)
                os.replace(path, store.path)
                (store.root / "secret.key").write_bytes(key)
                os.chmod(store.root / "secret.key", 0o600)
                os.chmod(store.path, 0o600)
                store.cipher = cipher
            store.event("warning", "restore", "备份已恢复；所有会话退出；恢复前副本保存在数据目录 before-restore")
        response = jsonify(ok=True)
        response.delete_cookie("am_session")
        return response

    if scheduler:
        engine.start()
    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=8080)
