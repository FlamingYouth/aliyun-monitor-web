import json
import math

from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest


class Cloud:
    def request(self, account, region, domain, version, action, params=None):
        client = AcsClient(account["ak"], account["sk"], region, auto_retry=False, connect_timeout=5, timeout=15)
        req = CommonRequest()
        req.set_domain(domain)
        req.set_version(version)
        req.set_action_name(action)
        req.set_method("POST")
        req.set_protocol_type("https")
        req.set_connect_timeout(5)
        req.set_read_timeout(15)
        for key, value in (params or {}).items():
            req.add_query_param(key, value)
        response = json.loads(client.do_action_with_exception(req).decode())
        if not isinstance(response, dict) or response.get("Code") not in (None, "Success", "200", 200):
            raise ValueError("阿里云 API 未返回有效成功数据")
        return response

    def traffic(self, account):
        result = self.request(account, "cn-hangzhou", "cdt.aliyuncs.com", "2021-08-13", "ListCdtInternetTraffic")
        details = result.get("TrafficDetails")
        if not isinstance(details, list):
            raise ValueError("CDT 流量数据缺少 TrafficDetails，停止自动决策")
        values = [float(item["Traffic"]) for item in details]
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ValueError("CDT 流量数据无效，停止自动决策")
        return sum(values) / (1024 ** 3)

    def status(self, account, instance):
        params = {"RegionId": instance["region"], "InstanceIds": json.dumps([instance["instance_id"]])}
        if instance.get("resgroup"):
            params["ResourceGroupId"] = instance["resgroup"]
        result = self.request(account, instance["region"], "ecs.aliyuncs.com", "2014-05-26", "DescribeInstances", params)
        items = result.get("Instances", {}).get("Instance", [])
        if len(items) != 1 or items[0].get("InstanceId") != instance["instance_id"]:
            raise ValueError("实例不存在、区域错误或 RAM 没有权限")
        item = items[0]
        locks = item.get("OperationLocks", {}).get("LockReason", [])
        if not isinstance(locks, list):
            raise ValueError("实例锁定信息无效，停止自动决策")
        return {"status": item.get("Status", "Unknown"),
                "spot_strategy": item.get("SpotStrategy", ""),
                "spot_interruption": item.get("SpotInterruptionBehavior", ""),
                "stopped_mode": item.get("StoppedMode", ""),
                "lock_reasons": [entry.get("LockReason", "Unknown") for entry in locks],
                "ip": item.get("EipAddress", {}).get("IpAddress") or next(iter(item.get("PublicIpAddress", {}).get("IpAddress", [])), "—")}

    def discover(self, account, region, resgroup=""):
        items = []
        for page in range(1, 21):
            params = {"RegionId": region, "PageSize": 100, "PageNumber": page}
            if resgroup:
                params["ResourceGroupId"] = resgroup
            data = self.request(account, region, "ecs.aliyuncs.com", "2014-05-26", "DescribeInstances", params)
            batch = data.get("Instances", {}).get("Instance", [])
            items.extend({"instance_id": i["InstanceId"], "name": i.get("InstanceName", ""), "status": i.get("Status", "Unknown")} for i in batch)
            if len(batch) < 100:
                break
        return items

    def balance(self, account):
        endpoint = "business.aliyuncs.com" if account["site"] == "china" else "business.ap-southeast-1.aliyuncs.com"
        result = self.request(account, "cn-hangzhou", endpoint, "2017-12-14", "QueryAccountBalance")
        if result.get("Success") is not True:
            raise ValueError("余额查询失败或 RAM 缺少账单权限")
        data = result.get("Data") or {}
        amount = float(str(data["AvailableAmount"]).replace(",", ""))
        if not math.isfinite(amount):
            raise ValueError("余额数据无效")
        return {"amount": amount, "currency": data.get("Currency", "CNY" if account["site"] == "china" else "USD")}

    def bill(self, account, month):
        endpoint = "business.aliyuncs.com" if account["site"] == "china" else "business.ap-southeast-1.aliyuncs.com"
        result = self.request(account, "cn-hangzhou", endpoint, "2017-12-14", "QueryBillOverview", {"BillingCycle": month})
        if result.get("Success") is not True:
            raise ValueError("账单查询失败或 RAM 缺少权限")
        items = result.get("Data", {}).get("Items", {}).get("Item", [])
        amount = sum(float(i.get("PretaxAmount", 0)) for i in items)
        if not math.isfinite(amount):
            raise ValueError("账单数据无效")
        return {"amount": amount,
                "currency": next(iter(items), {}).get("Currency", "CNY" if account["site"] == "china" else "USD"), "scope": "account"}

    def action(self, account, instance, action):
        if action not in ("StartInstance", "StopInstance"):
            raise ValueError("只允许启动或停止实例，不支持释放实例")
        params = {"InstanceId": instance["instance_id"], "RegionId": instance["region"]}
        if action == "StopInstance":
            params.update(StoppedMode="KeepCharging", ForceStop="false")
        result = self.request(account, instance["region"], "ecs.aliyuncs.com", "2014-05-26", action, params)
        if not result.get("RequestId"):
            raise ValueError("启停请求缺少阿里云确认标识，不能确认操作成功")
        return result

    def daily_bill(self, account, day):
        endpoint = "business.aliyuncs.com" if account["site"] == "china" else "business.ap-southeast-1.aliyuncs.com"
        amount, currency = 0.0, "CNY" if account["site"] == "china" else "USD"
        observed_currency = None
        has_entries = False
        for page in range(1, 101):
            result = self.request(account, "cn-hangzhou", endpoint, "2017-12-14", "QueryAccountBill",
                                  {"BillingCycle": day[:7], "Granularity": "DAILY", "BillingDate": day,
                                   "PageSize": 300, "PageNum": page})
            if result.get("Success") is not True:
                raise ValueError("每日账单查询失败或 RAM 缺少 QueryAccountBill 权限")
            data = result.get("Data")
            if not isinstance(data, dict) or not isinstance(data.get("Items", {}).get("Item"), list):
                raise ValueError("每日账单数据不完整")
            items = data["Items"]["Item"]
            has_entries = has_entries or bool(items)
            for item in items:
                value = float(item["PretaxAmount"])
                if not math.isfinite(value):
                    raise ValueError("每日账单金额无效")
                current_currency = item.get("Currency") or currency
                if observed_currency and observed_currency != current_currency:
                    raise ValueError("每日账单返回多个币种，无法直接汇总")
                observed_currency = currency = current_currency
                amount += value
            if len(items) < 300:
                if page * 300 < int(data.get("TotalCount", len(items))):
                    raise ValueError("每日账单分页数据不完整")
                break
        else:
            raise ValueError("每日账单超过查询上限，未保存不完整金额")
        if not math.isfinite(amount):
            raise ValueError("每日账单金额无效")
        return {"amount": amount, "currency": currency, "scope": "account", "has_entries": has_entries}
