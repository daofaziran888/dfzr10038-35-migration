"""在线服务领域服务：端点清单迁移、端点特征版本数据与在线特征值读取。

兼容历史已部署端点：迁移只生成草稿清单，确认后才写入第二版端点特征版本数据；
金额特征的已核实旧绑定在迁移前仍可按旧清单读到 10。
"""
from __future__ import annotations

from typing import Any

from app.services import endpoint_inventory as endp
from app.services.base import BaseModuleService
from app.store import store


class OnlineServService(BaseModuleService):
    MODULE = "online_serv"
    ENTITY = "服务端点"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["端点编号", "特征列表", "QPS上限", "延迟SLA", "缓存策略", "热加载", "服务端口", "运行状态",
                   "迁移状态", "清单状态", "清单指纹", "待核实项"]
    REQUIRED_FIELDS = ["端点编号", "特征列表", "QPS上限"]
    STATUS_ORDER = ["待部署", "运行中", "降级中", "已停用"]
    ACTION_RULES = {"部署服务": "运行中", "降级服务": "降级中", "停用服务": "已停用"}
    NEGATIVE_ACTIONS = ["停用服务"]
    CUSTOM_ACTIONS = ["迁移清单", "确认清单", "核实条目", "重提切换", "补办迁移"]

    def list_entries(self, **kwargs):
        items, total = super().list_entries(**kwargs)
        # 列表只反映已有迁移状态，不因为翻页而给端点凭空造清单。
        for row in items:
            endp.apply_summary(row)
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        if row is not None:
            # 读取端点只同步摘要；迁移样本要显式生成，GET 不产生新清单。
            endp.apply_summary(row)
        return row

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 新登记的端点同样先出迁移样本，确认后才切换到第二版端点特征版本数据。
            entry[endp.LEGACY_KEY] = str(values.get("特征列表", ""))
            entry["旧标签来源"] = str(values.get("旧标签来源", "") or "")
            endp.build_inventory(entry, source="新登记端点")
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action in self.CUSTOM_ACTIONS:
            return entry, f"{self.ENTITY}动作「{action}」需要调用专用接口"
        return super().run_action(entry_id, action)

    def perform_action(
        self, entry_id: int, action: str, remark: str | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档", False
        if action not in self.CUSTOM_ACTIONS:
            return super().perform_action(entry_id, action, remark)
        try:
            if action == "迁移清单":
                inventory = self.migrate(entry_id, force=False, source="迁移清单")
                return entry, (
                    f"已生成端点清单 {inventory['清单批次']} 第 {inventory['修订号']} 版草稿，"
                    f"指纹 {inventory['指纹']}，待核实 {entry['待核实项']} 项"), True
            if action == "确认清单":
                inventory = endp.confirm_inventory(entry, source="确认清单")
                return entry, (
                    f"端点清单 {inventory['清单批次']} 已确认，指纹 {inventory['指纹']}，"
                    f"已写入 {len(entry.get(endp.VERSIONS_KEY) or [])} 条端点特征版本数据"), True
            if action == "补办迁移":
                return entry, self._make_up(entry, remark), True
            return entry, f"{self.ENTITY}动作「{action}」缺少条目参数", False
        except endp.InventoryError as exc:
            return entry, exc.message, False

    # ------------------------------------------------------------------
    # 专用操作
    # ------------------------------------------------------------------

    def migrate(self, entry_id: int, *, force: bool = False, source: str = "迁移清单") -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        return endp.build_inventory(entry, source=source, force=force)

    def inventory(self, entry_id: int) -> dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        return endp.read_inventory(entry)

    def confirm(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        return endp.confirm_inventory(entry, source="确认清单")

    def verify(self, entry_id: int, token: str, *, resolution: str = "") -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        return endp.verify_item(entry, token, resolution=resolution)

    def readiness(self, entry_id: int, *, feature: str, version_label: str = "") -> dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        return endp.readiness(entry, feature=feature, version_label=version_label)

    def feature_value(
        self, entry_id: int, feature: str, *, preview_version: str = ""
    ) -> dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry is None:
            raise endp.InventoryError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        payload, reason = endp.read_feature_value(entry, feature, preview_version=preview_version)
        if payload is None:
            raise endp.InventoryError(reason)
        return payload

    def _make_up(self, entry: dict[str, Any], remark: str | None) -> str:
        """切换未生效时的补办：先生成迁移样本，再尝试确认。"""
        inventory = endp.build_inventory(entry, source="补办迁移")
        pending = endp.pending_items(inventory)
        if pending:
            raise endp.InventoryError(
                f"补办迁移生成清单 {inventory['清单批次']}，但仍有 {len(pending)} 个待核实项，"
                f"请先补核：{'、'.join(item['原值'] for item in pending)}")
        endp.confirm_inventory(entry, source="补办迁移")
        # 补办只建立清单与绑定，不擅自改变版本状态；需要由版本管理重新提交切换。
        return (
            f"已补办迁移并确认清单 {inventory['清单批次']}（指纹 {inventory['指纹']}），"
            f"请在版本管理重新提交全量切换")


service = OnlineServService()
