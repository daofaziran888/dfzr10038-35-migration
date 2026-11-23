"""版本管理领域服务：全量切换前的预检、切换回执和按回执补办迁移。

切换只有一个判定来源：运行中端点的端点清单与端点特征版本数据。存在未识别旧项或
端点尚未迁移时，切换不会“看起来成功”，而是留下一条可复查的未生效回执。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services import endpoint_inventory as endp
from app.services.base import BaseModuleService
from app.store import store

RECEIPT_KIND_READY = "已生效"
RECEIPT_KIND_BLOCKED = "未生效"
RECEIPT_KIND_PARTIAL = "部分生效"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class VersionMgrService(BaseModuleService):
    MODULE = "version_mgr"
    ENTITY = "特征版本"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["版本编号", "关联特征", "版本标签", "计算逻辑", "数据窗口", "变更原因", "灰度比例", "版本状态"]
    REQUIRED_FIELDS = ["版本编号", "关联特征", "版本标签"]
    STATUS_ORDER = ["草稿", "灰度中", "全量生效", "已回滚"]
    ACTION_RULES = {"灰度发布": "灰度中", "全量切换": "全量生效", "回滚版本": "已回滚"}
    NEGATIVE_ACTIONS = ["回滚版本"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "全量切换":
            return self._switch(entry_id)
        return super().run_action(entry_id, action)

    def perform_action(
        self, entry_id: int, action: str, remark: str | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        entry, message = self.run_action(entry_id, action)
        if entry is None:
            return entry, message, False
        if action == "全量切换":
            # 只有本次切换的回执是“未生效”才报 ok=false，其他动作沿用旧状态语义。
            ok = entry.get("最近切换回执", {}).get("结果") != RECEIPT_KIND_BLOCKED
            return entry, message, ok
        return entry, message, True

    # ------------------------------------------------------------------
    # 切换
    # ------------------------------------------------------------------

    def preflight(self, entry_id: int) -> dict[str, Any]:
        version = store.find(self.MODULE, entry_id)
        if version is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        feature = str(version.get("关联特征", "")).strip()
        label = str(version.get("版本标签", "")).strip()
        endpoints = endp.endpoints_for_feature(feature, running_only=False)
        checks = [
            endp.readiness(row, feature=feature, version_label=label)
            for row in endpoints
        ]
        return {
            "版本编号": version.get("版本编号", ""),
            "版本标签": label,
            "关联特征": feature,
            "可否生效": bool(checks) and all(check["可否生效"] for check in checks),
            "端点检查": checks,
            "说明": "只有运行中且清单已确认、无待核实项的端点才会真正切换",
            "检查时间": _now(),
        }

    def _switch(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        version = store.find(self.MODULE, entry_id)
        if version is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        with endp.INVENTORY_LOCK:
            feature = str(version.get("关联特征", "")).strip()
            label = str(version.get("版本标签", "")).strip()
            version_no = str(version.get("版本编号", ""))

            running = endp.endpoints_for_feature(feature, running_only=True)
            if not running:
                return self._record_blocked(
                    version, feature,
                    "没有运行中的端点提供该特征，切换不会改变任何在线值",
                    next_action="补办迁移",
                )

            receipts: list[dict[str, Any]] = []
            switched: list[dict[str, Any]] = []
            for endpoint in running:
                check = endp.readiness(endpoint, feature=feature, version_label=label)
                if not check["可否生效"]:
                    receipts.append(self._receipt(
                        endpoint, version, result=RECEIPT_KIND_BLOCKED,
                        reason="；".join(check["阻断原因"]),
                        next_action="补办迁移",
                        readiness=check,
                    ))
                    continue
                endp.rebind_feature(
                    endpoint, feature=feature, version_label=label, version_no=version_no)
                receipts.append(self._receipt(
                    endpoint, version, result=RECEIPT_KIND_READY,
                    reason=f"已切换，端点特征版本数据中 {feature} 绑定 {label}",
                    next_action="无",
                    readiness=check,
                ))
                switched.append(endpoint)

            if not switched:
                return self._record_blocked(
                    version, feature,
                    receipts[0]["原因"] if receipts else "端点清单未就绪",
                    next_action="补办迁移",
                    receipts=receipts,
                )

            version["status"] = self.ACTION_RULES["全量切换"]
            version["pending"] = False
            version["abnormal"] = False
            version["版本状态"] = "全量生效"
            version["灰度比例"] = "100%"
            version["切换时间"] = _now()
            result = RECEIPT_KIND_READY if not receipts or all(
                item["结果"] == RECEIPT_KIND_READY for item in receipts
            ) else RECEIPT_KIND_PARTIAL
            summary = {
                "版本编号": version_no,
                "版本标签": label,
                "关联特征": feature,
                "结果": result,
                "端点": receipts,
                "时间": _now(),
                "影响其他绑定": "否：只更新该特征的绑定，其他特征绑定保持原样",
            }
            version["最近切换回执"] = summary
            version["切换回执结果"] = result
            version.setdefault("切换回执历史", []).append(summary)
            message = (
                f"版本 {version_no}({label}) 全量切换{result}："
                f"已更新 {len(switched)} 个运行中端点的 {feature} 绑定")
            if result != RECEIPT_KIND_READY:
                message += "；部分端点需按回执补办迁移后重提"
            return version, message

    def _record_blocked(
        self,
        version: dict[str, Any],
        feature: str,
        reason: str,
        *,
        next_action: str,
        receipts: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """切换未生效：保留版本原状态，只落一条回执，绝不假装已切换。"""
        summary = {
            "版本编号": version.get("版本编号", ""),
            "版本标签": version.get("版本标签", ""),
            "关联特征": feature,
            "结果": RECEIPT_KIND_BLOCKED,
            "原因": reason,
            "下一步": next_action,
            "端点": receipts or [],
            "当前版本状态": version.get("status", ""),
            "时间": _now(),
            "影响其他绑定": "否：未写入任何端点绑定",
        }
        version["最近切换回执"] = summary
        version["切换回执结果"] = RECEIPT_KIND_BLOCKED
        version.setdefault("切换回执历史", []).append(summary)
        return version, f"全量切换未生效：{reason}；请按回执「{next_action}」后重提"

    def _receipt(
        self,
        endpoint: dict[str, Any],
        version: dict[str, Any],
        *,
        result: str,
        reason: str,
        next_action: str,
        readiness: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = {
            "端点编号": endpoint.get("端点编号", ""),
            "端点状态": endpoint.get("status", ""),
            "清单批次": readiness.get("清单批次", ""),
            "清单状态": readiness.get("清单状态", ""),
            "清单指纹": readiness.get("清单指纹", ""),
            "切换前绑定版本": readiness.get("当前绑定版本", ""),
            "切换前值": readiness.get("当前值"),
            "切换后值": readiness.get("目标版本值"),
            "结果": result,
            "原因": reason,
            "下一步": next_action,
            "时间": _now(),
        }
        endpoint["最近切换回执"] = {
            "版本编号": version.get("版本编号", ""),
            "版本标签": version.get("版本标签", ""),
            "结果": result,
            "原因": reason,
            "清单指纹": receipt["清单指纹"],
            "时间": receipt["时间"],
        }
        return receipt

    def receipts(self, entry_id_or_row: int | dict[str, Any]) -> list[dict[str, Any]]:
        row = entry_id_or_row if isinstance(entry_id_or_row, dict) else store.find(
            self.MODULE, entry_id_or_row)
        if row is None:
            return []
        history = list(row.get("切换回执历史") or [])
        latest = row.get("最近切换回执")
        if latest and latest not in history:
            history.append(latest)
        return history

    def receipt(self, entry_id: int) -> dict[str, Any]:
        version = store.find(self.MODULE, entry_id)
        if version is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        latest = version.get("最近切换回执")
        if not latest:
            raise endp.InventoryError(f"版本 {version.get('版本编号', '')} 尚无切换回执，请先提交全量切换")
        return latest


service = VersionMgrService()
