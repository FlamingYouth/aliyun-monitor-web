from datetime import datetime
import json
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from notify import CHANNELS


class BusyError(Exception):
    pass


class Engine:
    def __init__(self, store, cloud, notifier):
        self.store, self.cloud, self.notifier = store, cloud, notifier
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def notify(self, message):
        settings = self.store.settings(True)
        result = {}
        for channel in CHANNELS:
            if settings[channel + "_enabled"]:
                try:
                    self.notifier.send(channel, settings, self.store.redact(message, limit=None))
                    result[channel] = True
                except Exception as error:
                    result[channel] = False
                    self.store.event("error", "notify", f"{channel} 推送失败：{error}")
        return result

    def manual_control(self, item_id, action):
        """The authenticated route holds the shared mutation/job lock throughout."""
        if action not in ("start", "stop"):
            raise ValueError("请选择开机或普通关机")
        item = self.store.get("instances", item_id)
        if not item:
            raise ValueError("监控实例不存在")
        account = self.store.account(item["account_id"])
        if not account:
            raise ValueError("关联账号不存在")
        snapshot = self.cloud.status(account, item)
        status = snapshot["status"]
        if status not in ("Running", "Stopped"):
            raise ValueError("实例状态为启动中、关闭中或未知，请等待云端完成后再操作")
        if snapshot.get("lock_reasons"):
            raise ValueError("实例当前存在云端锁定，不能手动启停，请检查阿里云控制台")
        if action == "start" and not self.store.settings()["dry_run"]:
            traffic = self.cloud.traffic(account)
            if traffic >= item["traffic_limit"]:
                raise ValueError("账号流量已达到止损阈值，拒绝启动；请先核对流量与监控策略")
        target = "Running" if action == "start" else "Stopped"
        stamp = "start_requested_at" if action == "start" else "stop_requested_at"
        if status != target and time.time() - item.get(stamp, 0) < 60:
            raise ValueError("刚刚已尝试同一操作，请至少等待 60 秒并到云端核实状态")
        item.update(snapshot)
        if action == "stop":
            # Persist the user's intent BEFORE the request: a timeout may still stop it.
            item.update(manual_hold=True, stopped_by_monitor=False, spot_recovery_pending=False)
            self.store.put("instances", item)
        if status == target:
            if action == "start":
                item.update(manual_hold=False, stopped_by_monitor=False, spot_recovery_pending=False)
                self.store.put("instances", item)
            message = "实例已运行，无须重复开机" if action == "start" else "实例已停止，已设为手动保持关闭"
            self.store.event("info", "control", f"{item['name']}：{message}", item_id)
            return {"message": message, "submitted": False, "status": status}
        item[stamp] = time.time()
        self.store.put("instances", item)
        try:
            result = self.cloud.action(account, item, "StartInstance" if action == "start" else "StopInstance")
        except Exception as error:
            safe = self.store.redact(error)
            self.store.event("error", "control", f"{item['name']}：手动启停请求未确认，请到云端核实；{safe}", item_id)
            raise ValueError(f"请求未确认，请到阿里云核实状态；{safe}") from error
        if action == "start":
            item.update(manual_hold=False, stopped_by_monitor=False, spot_recovery_pending=False, status="Starting")
        else:
            item["status"] = "Stopping"
        item.update(error="", last_control={"action": action, "at": time.time(), "request_id": result["RequestId"]})
        self.store.put("instances", item)
        message = "已提交开机请求，请刷新状态确认" if action == "start" else "已提交普通关机请求：保留资源、继续计费；不会自动拉起"
        self.store.event("info", "control", f"{item['name']}：{message}；RequestId={result['RequestId']}", item_id)
        self.notify(f"{'✅ 手动开机' if action == 'start' else '⏸ 手动普通关机'}\n实例：{item['name']}\n{message}")
        return {"message": message, "submitted": True, "status": item["status"], "request_id": result["RequestId"]}

    def check(self):
        summaries, traffic_cache = [], {}
        settings = self.store.settings(True)
        for original in self.store.rows("instances"):
            item = dict(original)
            action_attempted = False
            if item["paused"]:
                summaries.append({"name": item["name"], "result": "已暂停"})
                continue
            try:
                account = self.store.account(item["account_id"])
                if not account:
                    raise ValueError("关联账号不存在")
                aid = account["id"]
                if aid not in traffic_cache:
                    try:
                        traffic_cache[aid] = self.cloud.traffic(account)
                    except Exception as error:
                        traffic_cache[aid] = error
                traffic = traffic_cache[aid]
                if isinstance(traffic, Exception):
                    raise traffic
                previous_status = item.get("status")
                snapshot = self.cloud.status(account, item)
                item.update(snapshot, traffic=round(traffic, 4), checked_at=time.time(), error="", failures=0)
                self.store.sample(item["id"], traffic, snapshot["status"])
                action = "无须操作"
                status = snapshot["status"]
                locks = snapshot.get("lock_reasons", [])
                spot = snapshot.get("spot_strategy") in ("SpotWithPriceLimit", "SpotAsPriceGo") and snapshot.get("spot_interruption") == "Stop"
                if spot and "Recycling" in locks and not item.get("manual_hold") and not item.get("stopped_by_monitor"):
                    if not item.get("spot_recovery_pending"):
                        self.store.event("warning", "spot", f"{item['name']}：识别到阿里云抢占回收标记，等待停止后按策略恢复", item["id"])
                    item.update(spot_recovery_pending=True, spot_notice_at=time.time())
                if status == "Running" and "Recycling" not in locks and (previous_status in ("Stopped", "Starting") or
                                                                        time.time() - item.get("spot_notice_at", 0) > 600):
                    item["spot_recovery_pending"] = False
                if status == "Running" and traffic < item["traffic_limit"]:
                    item["stopped_by_monitor"] = False
                if traffic >= item["traffic_limit"]:
                    if status == "Running":
                        if locks:
                            action = "保持现状：实例处于云端锁定状态"
                        elif settings["dry_run"]:
                            action = "只监控：达到关机阈值"
                        else:
                            action_attempted = True
                            item["stop_requested_at"] = time.time()
                            self.store.put("instances", item)
                            self.cloud.action(account, item, "StopInstance")
                            item["stopped_by_monitor"] = True
                            item["spot_recovery_pending"] = False
                            action = "已提交超限普通关机请求（保留资源、继续计费）"
                        self.store.event("warning", "threshold", f"{item['name']}：{traffic:.2f} GB，{action}", item["id"])
                    if time.time() - item.get("overlimit_notified_at", 0) >= 86400:
                        result = self.notify(f"⚠ 阿里云流量预警\n实例：{item['name']}\n账号 CDT 累积流量：{traffic:.2f} GB\n阈值：{item['traffic_limit']:g} GB\n{action}\n模式：{'只监控' if settings['dry_run'] else '自动止损'}")
                        if result and all(result.values()):
                            item["overlimit_notified_at"] = time.time()
                elif status == "Stopped" and item.get("manual_hold"):
                    action = "保持关闭：已手动关机；只有手动开机可解除"
                elif status == "Stopped" and ((item.get("stopped_by_monitor") and item["auto_restore"]) or
                                             (item.get("spot_auto_restore") and spot and item.get("spot_recovery_pending") and
                                              not item.get("stopped_by_monitor") and snapshot.get("stopped_mode") == "StopCharging")):
                    is_spot_restore = not item.get("stopped_by_monitor")
                    if locks:
                        action = "等待恢复：云端仍锁定实例"
                    elif settings["dry_run"]:
                        action = "只监控：符合抢占恢复条件" if is_spot_restore else "只监控：符合自动恢复条件"
                    elif time.time() - item.get("start_requested_at", 0) >= (300 if is_spot_restore else 1800):
                        action_attempted = True
                        item["start_requested_at"] = time.time()
                        self.store.put("instances", item)
                        self.cloud.action(account, item, "StartInstance")
                        item["status"] = "Starting"
                        action = "已提交抢占恢复启动请求" if is_spot_restore else "已提交恢复启动请求"
                        self.store.event("info", "restore", f"{item['name']}：{action}", item["id"])
                        self.notify(f"✅ 阿里云自动恢复\n实例：{item['name']}\n账号 CDT 累积流量：{traffic:.2f} GB\n已提交启动请求，下一次巡检确认状态。")
                    else:
                        action = "等待恢复：启动重试冷却中"
                elif status == "Stopped" and not item.get("stopped_by_monitor"):
                    action = "保持关闭：未识别可信抢占标记或未启用恢复"
                self.store.put("instances", item)
                summaries.append({"name": item["name"], "result": action, "traffic": traffic, "status": status})
            except Exception as error:
                item.update(error=self.store.redact(error), checked_at=time.time(), failures=item.get("failures", 0) + 1)
                self.store.put("instances", item)
                safety = "动作请求未确认，请到云端核实状态" if action_attempted else "未提交启停请求"
                self.store.event("error", "check", f"{item['name']}：巡检失败，{safety}；{error}", item["id"])
                if item["failures"] >= 3 and time.time() - item.get("failure_notified_at", 0) >= 3600:
                    result = self.notify(f"🚨 监控异常\n实例：{item['name']}\n连续 {item['failures']} 次巡检失败\n自动止损暂不可用，请人工检查。\n原因：{item['error']}")
                    if result and all(result.values()):
                        item["failure_notified_at"] = time.time()
                        self.store.put("instances", item)
                summaries.append({"name": item["name"], "error": item["error"]})
        self.store.set_meta("last_check", time.time())
        return summaries

    def report(self):
        settings = self.store.settings(True)
        now = datetime.now(ZoneInfo(settings["timezone"]))
        lines = ["📊 阿里云监控日报", now.strftime("%Y-%m-%d %H:%M"),
                 "运行模式：" + ("只监控" if settings["dry_run"] else "自动止损"), ""]
        errors = []
        for original in self.store.rows("accounts"):
            account = self.store.account(original["id"])
            data = dict(original)
            lines.append(f"账号：{account['name']}")
            for key, call in (("balance", lambda: self.cloud.balance(account)),
                              ("bill", lambda: self.cloud.bill(account, now.strftime("%Y-%m")))):
                try:
                    value = call()
                    data[key] = value
                    data[key + "_at"] = time.time()
                    data.pop(key + "_error", None)
                    lines.append(f"{'余额' if key == 'balance' else '本月账号账单'}：{value['currency']} {value['amount']:.2f}")
                except Exception as error:
                    data.pop(key, None)
                    data[key + "_error"] = "查询失败，请查看事件日志"
                    errors.append(f"{account['name']}：{key} 查询失败")
                    lines.append(f"{'余额' if key == 'balance' else '账单'}：查询失败")
                    self.store.event("warning", "report", f"{account['name']} {key} 查询失败：{error}")
            self.store.put("accounts", data)
            self.refresh_daily_bills(account, data, now, errors)
            lines.append("")
        for item in self.store.rows("instances"):
            lines.append(f"{item['name']} · {item['instance_id']}\n状态：{'已暂停' if item['paused'] else item.get('status', '未巡检')}\n"
                         f"账号 CDT 流量：{item.get('traffic', 0):.2f} / {item['traffic_limit']:g} GB\n"
                         f"{'巡检异常：' + item['error'] if item.get('error') else ''}")
        report = self.store.redact("\n".join(lines), limit=None)
        result = self.notify(report)
        self.store.save_report(report, result)
        return {"content": report, "delivery": result, "errors": errors}

    def refresh_daily_bills(self, account, data, now, errors):
        month = now.strftime("%Y-%m")
        cached = {bill["day"]: bill for bill in self.store.daily_bills(account["id"], month)}
        try:
            # Include today's provisional bill. Empty billing responses today
            # mean not issued yet, so preserve an existing/manual amount.
            for number in range(1, now.day + 1):
                day = f"{month}-{number:02d}"
                if day in cached and number < now.day - 3:
                    continue
                bill = self.cloud.daily_bill(account, day)
                if number == now.day and bill.get("has_entries") is False:
                    continue
                self.store.save_daily_bill(account["id"], day, bill)
            data.pop("daily_bill_error", None)
        except Exception as error:
            data["daily_bill_error"] = "每日账单查询失败，请检查 QueryAccountBill 读取权限或稍后重试"
            errors.append(f"{account['name']}：每日账单查询失败")
            self.store.event("warning", "report", f"{account['name']} 每日账单查询失败：{error}")
        self.store.put("accounts", data)

    def launch(self, kind, background=True):
        if kind not in ("check", "report"):
            raise ValueError("未知任务")
        if not self.lock.acquire(blocking=False):
            raise BusyError("已有任务运行中，请等待完成")
        job_id = uuid.uuid4().hex
        try:
            with self.store.db() as db:
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (job_id, kind, "running", time.time(), None, None))
            self.store.prune_history()
        except BaseException:
            self.lock.release()
            raise
        def execute():
            try:
                if kind == "report":
                    checks = self.check()
                    result = self.report()
                    result["checks"] = checks
                    partial = any("error" in row for row in checks) or any(not ok for ok in result["delivery"].values()) or bool(result["errors"])
                else:
                    result = self.check()
                    partial = any("error" in row for row in result)
                status = "partial" if partial else "done"
                self.store.event("warning" if partial else "info", "task", f"{'巡检' if kind == 'check' else '日报'}任务{'部分失败' if partial else '完成'}")
            except Exception as error:
                status, result = "failed", {"error": self.store.redact(error)}
                self.store.event("error", "task", f"任务失败：{error}")
            finally:
                try:
                    with self.store.db() as db:
                        db.execute("UPDATE jobs SET status=?,finished=?,result=? WHERE id=?", (status, time.time(), json.dumps(result, ensure_ascii=False), job_id))
                    self.store.prune_history()
                finally:
                    self.lock.release()
        if background:
            threading.Thread(target=execute, daemon=True, name="monitor-job").start()
        else:
            execute()
        return job_id

    def schedule(self):
        while not self.stop.wait(5):
            try:
                self.store.prune_history()
                if not self.store.meta("admin"):
                    continue
                settings = self.store.settings()
                if not settings.get("scheduler_enabled", True) or self.lock.locked():
                    continue
                now = datetime.now(ZoneInfo(settings["timezone"]))
                day = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") >= settings["report_time"] and self.store.meta("daily_report_day") != day:
                    self.store.set_meta("daily_report_day", day)
                    self.launch("report")
                elif time.time() - self.store.meta("last_check", 0) >= settings["interval"]:
                    self.launch("check")
            except Exception as error:
                self.store.event("error", "scheduler", f"调度错误：{error}")

    def start(self):
        threading.Thread(target=self.schedule, daemon=True, name="monitor-scheduler").start()
