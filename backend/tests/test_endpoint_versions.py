"""端点特征版本迁移/切换的端到端校验脚本（仅用标准库）。

跑法：``.venv/bin/python tests/test_endpoint_versions.py``

脚本会自己拉起一个临时端口上的 uvicorn，逐条验证：
旧清单兼容读取、迁移样本、未知项补核、确认清单、并发确认只留一份、
切换回执、切换后金额特征值以及其他绑定不受影响。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
BASE = ""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_ready(proc: subprocess.Popen) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败")
        try:
            status, _ = request("GET", "/api/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("服务健康检查超时")


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))


def find_items(inventory: dict, token: str) -> dict:
    return next(item for item in inventory["条目"] if item["原值"] == token)


def main() -> int:
    global BASE
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PYTHONPATH=str(APP_DIR))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(APP_DIR), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(proc)

        # 1. 切换前：兼容历史已部署端点，金额特征已核实旧绑定读到 10。
        _, value = request("GET", "/api/online_serv/2/features/order_amount_30d/value")
        check("切换前金额特征读到旧值 10", value.get("值") == 10, str(value))
        check("旧值读取保留原绑定版本 v2", value.get("绑定版本") == "v2", str(value))

        # 2. 迁移样本：身份与绑定分开，旧标签来源可查，真实特征 v3 保留。
        _, inv = request("GET", "/api/online_serv/2/feature-inventory")
        items = {item["原值"]: item for item in inv["条目"]}
        features = {item["特征身份"] for item in inv["条目"] if item["条目类型"] == "特征"}
        check("迁移样本拆出真实特征身份", {"user_age_7d", "order_amount_30d", "v3"} <= features, str(features))
        check("v3 按真实特征保留", items["v3"]["条目类型"] == "特征", str(items["v3"]))
        check("v2 识别为旧版本标签", items["v2"]["条目类型"] == "版本标签", str(items["v2"]))
        check("v1 旧标签来源可查", items["v1"]["版本来源"] == "手动备注", str(items["v1"]))
        check("金额特征绑定可从版本管理读出",
              inv["条目"][[i["原值"] for i in inv["条目"]].index("order_amount_30d")]["可服务值"] == 10)
        unknown = items["legacy_unknown_flag"]
        check("未知项保留原值和原因", unknown["条目类型"] == "未知项"
              and unknown["核实状态"] == "待核实" and bool(unknown["保留原因"]), str(unknown))
        check("未知项给出补核方式", bool(unknown["补核方式"]), str(unknown))

        # 3. 未识别项未查明时：切换预检明确不可生效。
        _, ready = request("GET", "/api/online_serv/2/readiness?feature=order_amount_30d&version_label=v3")
        check("待核实项阻断切换", ready["可否生效"] is False, str(ready))
        check("预检给出目标版本值 12", ready["目标版本值"] == 12, str(ready))

        # 4. 此时提交全量切换：不生效，只留回执。
        _, action = request("POST", "/api/version_mgr/4/actions", {"action": "全量切换"})
        check("未知项未查明时切换被拒绝", action["ok"] is False, str(action.get("message")))
        _, version = request("GET", "/api/version_mgr/4")
        check("切换未生效不改版本状态", version["status"] == "草稿", version["status"])
        receipt = version["最近切换回执"]
        check("回执结果未生效", receipt["结果"] == "未生效", str(receipt))
        check("回执指向补办迁移", receipt["下一步"] == "补办迁移", str(receipt))

        # 5. 随即从运行中端点请求：仍是旧值 10，其他绑定不变。
        _, value = request("GET", "/api/online_serv/2/features/order_amount_30d/value")
        check("切换未生效后金额特征仍为 10", value.get("值") == 10, str(value))
        check("回执结果随读取返回", value.get("切换回执结果") == "未生效", str(value))
        _, age = request("GET", "/api/online_serv/2/features/user_age_7d/value")
        check("其他特征绑定继续有效", age.get("值") == 8 and age.get("绑定版本") == "v2", str(age))
        _, real_v3 = request("GET", "/api/online_serv/2/features/v3/value")
        check("真实特征 v3 仍可读", real_v3.get("值") == 3, str(real_v3))

        # 6. 未知项未查明时不能确认清单，也不能补办迁移。
        status, blocked = request("POST", "/api/online_serv/2/feature-inventory/confirm")
        check("待核实项阻止确认清单", status == 409, str(blocked))
        _, make_up = request("POST", "/api/online_serv/2/actions", {"action": "补办迁移"})
        check("待核实项阻止补办迁移", make_up["ok"] is False, str(make_up.get("message")))

        # 7. 补核未知项：保留原值和原因。
        _, verified = request(
            "POST", "/api/online_serv/2/feature-inventory/verify",
            {"token": "legacy_unknown_flag", "resolution": "历史实验遗留，与本次切换无关"},
        )
        entry = find_items(verified["inventory"], "legacy_unknown_flag")
        check("补核后保留原值", entry["原值"] == "legacy_unknown_flag", str(entry))
        check("补核后保留原因", "原值不删除" in entry["保留原因"], str(entry))
        check("补核后状态为已核实", entry["核实状态"] == "已核实", str(entry))

        # 8. 并发确认：只留一份可复查清单。
        results: list[tuple[int, dict]] = []

        def confirm() -> None:
            results.append(request("POST", "/api/online_serv/2/feature-inventory/confirm"))

        threads = [threading.Thread(target=confirm) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        confirmed = [body for status, body in results if status == 200]
        fingerprints = {body["entry"]["指纹"] for body in confirmed}
        check("并发确认全部成功且幂等", len(confirmed) == len(results), str([s for s, _ in results]))
        check("并发确认只产生一份清单", len(fingerprints) == 1, str(fingerprints))
        _, after = request("GET", "/api/online_serv/2/feature-inventory")
        check("清单状态已确认", after["状态"] == "已确认", str(after))
        check("清单指纹稳定", after["指纹"] in fingerprints, str(after["指纹"]))
        _, endpoint = request("GET", "/api/online_serv/2")
        check("端点特征列表不再含版本标签",
              "v2" not in endpoint["特征列表"].split(",") and "v1" not in endpoint["特征列表"].split(","),
              endpoint["特征列表"])
        check("端点特征列表保留真实特征 v3", "v3" in endpoint["特征列表"].split(","), endpoint["特征列表"])

        # 9. 迁移完成后切换生效。
        _, preflight = request("GET", "/api/version_mgr/4/switch-preflight")
        check("迁移确认后预检可生效", preflight["可否生效"] is True, str(preflight))
        _, action = request("POST", "/api/version_mgr/4/actions", {"action": "全量切换"})
        check("迁移后切换成功", action["ok"] is True, str(action.get("message")))
        _, receipt = request("GET", "/api/version_mgr/4/receipt")
        check("切换回执已生效", receipt["结果"] == "已生效", str(receipt))
        check("回执记录切换前后值", receipt["端点"][0]["切换前值"] == 10
              and receipt["端点"][0]["切换后值"] == 12, str(receipt["端点"][0]))

        # 10. 运行中端点读到新值 12，其他绑定仍有效。
        _, value = request("GET", "/api/online_serv/2/features/order_amount_30d/value")
        check("切换后金额特征读到 12", value.get("值") == 12, str(value))
        check("切换后绑定版本为 v3", value.get("绑定版本") == "v3", str(value))
        check("值来自端点特征版本数据", value.get("值来源") == "端点特征版本数据", str(value))
        for feature, expected in (("user_age_7d", 8), ("user_age_bucket_7d", 3), ("v3", 3)):
            _, other = request("GET", f"/api/online_serv/2/features/{feature}/value")
            check(f"切换后 {feature} 绑定仍然有效", other.get("值") == expected, str(other))
        _, endpoint = request("GET", "/api/online_serv/2")
        check("切换后仍只有一份清单", endpoint["清单状态"] == "已确认"
              and endpoint["清单指纹"] == after["指纹"], str(endpoint.get("清单指纹")))
        check("切换不改写已确认清单的修订号", endpoint["清单修订"] == after["修订号"],
              str(endpoint["清单修订"]))

        # 11. 版本标签不再是特征身份。
        status, label_read = request("GET", "/api/online_serv/2/features/v2/value")
        check("版本标签不能被当作特征读取", status == 409, str(label_read))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("失败：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
