"""端点特征版本数据：旧清单迁移、清单确认、切换检查与特征值读取。

这里集中处理“特征身份”和“绑定版本”的拆分，供在线服务与版本管理共同调用。
仓库仍然是内存表，所有写入都在 ``INVENTORY_LOCK`` 内完成，保证迁移与切换并发时
同一端点只会确认一份可复查的清单，其他特征的绑定不受影响。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any

from app.domain.endpoint_versions import (
    KIND_FEATURE,
    KIND_LABEL,
    KIND_UNKNOWN,
    VERIFY_CONFIRMED,
    VERIFY_PENDING,
    InventoryItem,
    bind_verified_versions,
    classify_tokens,
    inventory_fingerprint,
    parse_label_sources,
    parse_legacy_list,
    serving_value,
)
from app.store import store

INVENTORY_KEY = "端点清单"
VERSIONS_KEY = "端点特征版本"
LEGACY_KEY = "历史旧清单"
CONFIRMED_KEY = "切换回执"

STATUS_RUNNING = "运行中"

INVENTORY_LOCK = threading.RLock()


class InventoryError(Exception):
    """清单操作无法继续时抛出，message 直接给运维看。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _known_features() -> set[str]:
    return {
        str(row.get("特征名称", "")).strip()
        for row in store.rows("feature_reg")
        if str(row.get("特征名称", "")).strip()
    }


def version_registry() -> dict[str, list[dict[str, Any]]]:
    """按特征身份汇总版本管理记录，保持登记顺序。"""
    registry: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("version_mgr"):
        feature = str(row.get("关联特征", "")).strip()
        if feature:
            registry.setdefault(feature, []).append(row)
    return registry


def choose_version(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取当前应绑定的版本：优先全量生效，其次最后一条记录。"""
    if not versions:
        return None
    for row in reversed(versions):
        if row.get("status") == "全量生效":
            return row
    return versions[-1]


def raw_legacy_list(row: dict[str, Any]) -> str:
    """旧清单原文；迁移过的端点保留在“历史旧清单”，避免二次迁移丢项。"""
    return str(row.get(LEGACY_KEY) or row.get("特征列表", ""))


def _stored_bindings(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for entry in row.get(VERSIONS_KEY) or []:
        identity = str(entry.get("特征身份", "")).strip()
        if identity:
            bindings[identity] = entry
    return bindings


def _source_tokens(row: dict[str, Any]) -> list[str]:
    """旧清单原文，加上迁移后新出现在端点特征列表里的项。

    迁移不会丢项：``历史旧清单`` 保留原文；迁移后如果端点特征列表被人工改动，
    新出现的 token 也要进入清单，按特征/标签/未知项重新归类，而不是被无视。
    """
    tokens = parse_legacy_list(raw_legacy_list(row))
    seen = set(tokens)
    for token in parse_legacy_list(str(row.get("特征列表", ""))):
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _build_items(row: dict[str, Any], previous: dict[str, Any] | None) -> list[InventoryItem]:
    tokens = _source_tokens(row)
    items = classify_tokens(
        tokens,
        known_features=_known_features(),
        label_sources=parse_label_sources(str(row.get("旧标签来源", ""))),
    )

    # 迁移过的端点：已有绑定优先于按版本管理重新推导，避免覆盖切换结果。
    stored = _stored_bindings(row)
    for item in items:
        if item.条目类型 == KIND_FEATURE and item.特征身份 in stored:
            entry = stored[item.特征身份]
            item.绑定版本 = str(entry.get("绑定版本", ""))
            item.版本来源 = str(entry.get("版本来源", ""))
            item.可服务值 = serving_value(item.特征身份, item.绑定版本)
    bind_verified_versions(
        [item for item in items if item.特征身份 not in stored],
        version_registry=version_registry(),
    )

    # 草稿修订时保留已经做过的核实结论，不能因为重建清单又变回待核实。
    prior = {
        str(entry.get("原值", "")): entry
        for entry in (previous or {}).get("条目", [])
    }
    for item in items:
        entry = prior.get(item.原值)
        if entry is None or entry.get("核实状态") == VERIFY_PENDING:
            continue
        item.核实状态 = str(entry.get("核实状态", item.核实状态))
        item.核实结论 = str(entry.get("核实结论", ""))
        item.保留原因 = str(entry.get("保留原因", item.保留原因))
        item.补核方式 = str(entry.get("补核方式", item.补核方式))
    return items


def _snapshot(row: dict[str, Any], items: list[InventoryItem], *, previous: dict[str, Any] | None,
              source: str, endpoint_no: str) -> dict[str, Any]:
    revision = int((previous or {}).get("修订号", 0)) + 1
    batch = str((previous or {}).get("清单批次") or f"MIG-{endpoint_no}-1")
    rows = [item.as_row() for item in items]
    return {
        "清单批次": batch,
        "修订号": revision,
        "指纹": inventory_fingerprint(items),
        "状态": "草稿",
        "来源": source,
        "生成时间": _now(),
        "确认时间": "",
        "确认人": "",
        "历史旧清单": raw_legacy_list(row),
        "条目": rows,
    }


def summary_fields(row: dict[str, Any]) -> dict[str, Any]:
    """端点行上的迁移摘要：未迁移的端点也要能一眼看出来。"""
    inventory = row.get(INVENTORY_KEY)
    if not inventory:
        return {
            "迁移状态": "未迁移",
            "清单状态": "未迁移",
            "清单批次": "",
            "清单修订": 0,
            "清单指纹": "",
            "待核实项": 0,
            "清单来源": "",
        }
    pending = sum(1 for item in inventory["条目"] if item.get("核实状态") == VERIFY_PENDING)
    fields = {
        "迁移状态": "已迁移" if inventory["状态"] == "已确认" else "待补办迁移",
        "清单状态": inventory["状态"],
        "清单批次": inventory["清单批次"],
        "清单修订": inventory["修订号"],
        "清单指纹": inventory["指纹"],
        "待核实项": pending,
        "清单来源": inventory["来源"],
        "最近迁移": inventory.get("确认时间") or inventory.get("生成时间", ""),
    }
    if pending:
        fields["待核实提示"] = (
            "以下旧项尚未查明，切换前需逐项补核：" +
            "、".join(
                item["原值"] for item in inventory["条目"]
                if item.get("核实状态") == VERIFY_PENDING
            )
        )
    return fields


def apply_summary(row: dict[str, Any]) -> None:
    """把迁移摘要写回端点行；不创建清单，只反映当前已有状态。"""
    row.update(summary_fields(row))


def build_inventory(
    row: dict[str, Any], *, source: str = "迁移清单", force: bool = False
) -> dict[str, Any]:
    """生成或复用迁移样本；已确认且未过期的清单不会被悄悄替换。"""
    with INVENTORY_LOCK:
        existing = row.get(INVENTORY_KEY)
        if existing and not force:
            if existing.get("状态") == "已确认":
                if not _inventory_stale(row, existing):
                    apply_summary(row)
                    return existing
                # 端点清单已被改动，旧确认件移入历史后重新出一份草稿修订。
                history = row.setdefault("清单历史", [])
                history.append({**existing, "状态": "已废弃", "废弃原因": "端点特征列表已变化"})
            elif existing.get("历史旧清单") == raw_legacy_list(row):
                # 草稿可复用，但迁移后特征列表若又出现新 token，必须重新出修订，
                # 否则新项会被静默丢掉。
                if _draft_covers(row, existing):
                    apply_summary(row)
                    return existing

        inventory = _snapshot(
            row,
            _build_items(row, existing),
            previous=existing,
            source=source,
            endpoint_no=str(row.get("端点编号", row.get("id", ""))),
        )
        row[INVENTORY_KEY] = inventory
        apply_summary(row)
        return inventory


def _inventory_stale(row: dict[str, Any], inventory: dict[str, Any]) -> bool:
    """迁移后端点特征列表应等于清单中的真实特征身份集合。"""
    if not row.get(VERSIONS_KEY):
        return True
    current = {
        token for token in parse_legacy_list(str(row.get("特征列表", "")))
    }
    expected = {
        item["特征身份"] for item in inventory["条目"]
        if item["条目类型"] == KIND_FEATURE
    }
    return current != expected


def _draft_covers(row: dict[str, Any], inventory: dict[str, Any]) -> bool:
    """草稿清单是否已经覆盖端点当前清单里的所有 token。"""
    listed = {item.get("原值") for item in inventory.get("条目", [])}
    return set(_source_tokens(row)) <= listed


def pending_items(inventory: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not inventory:
        return []
    return [item for item in inventory["条目"] if item.get("核实状态") == VERIFY_PENDING]


def _apply_bindings(row: dict[str, Any], inventory: dict[str, Any]) -> None:
    """把清单中的特征身份与绑定版本落到第二版端点特征版本数据。"""
    versions: list[dict[str, Any]] = []
    for item in inventory["条目"]:
        if item["条目类型"] != KIND_FEATURE:
            continue
        versions.append({
            "特征身份": item["特征身份"],
            "绑定版本": item.get("绑定版本", ""),
            "版本来源": item.get("版本来源", ""),
            "值": item.get("可服务值"),
        })
    row[VERSIONS_KEY] = versions
    row["特征列表"] = ",".join(entry["特征身份"] for entry in versions)
    row.setdefault(LEGACY_KEY, inventory["历史旧清单"])


def refresh_fingerprint(inventory: dict[str, Any]) -> None:
    items = [_item_from_row(entry) for entry in inventory["条目"]]
    inventory["指纹"] = inventory_fingerprint(items)


def _item_from_row(entry: dict[str, Any]) -> InventoryItem:
    return InventoryItem(
        特征身份=str(entry.get("特征身份", "")),
        条目类型=str(entry.get("条目类型", "")),
        绑定版本=str(entry.get("绑定版本", "")),
        版本来源=str(entry.get("版本来源", "")),
        核实状态=str(entry.get("核实状态", "")),
        原值=str(entry.get("原值", "")),
        保留原因=str(entry.get("保留原因", "")),
        可服务值=entry.get("可服务值"),
        补核方式=str(entry.get("补核方式", "")),
        核实结论=str(entry.get("核实结论", "")),
    )


def confirm_inventory(
    row: dict[str, Any], *, source: str = "补办迁移", actor: str = "运维"
) -> dict[str, Any]:
    """确认唯一一份清单；仍有待核实项时拒绝确认。"""
    with INVENTORY_LOCK:
        inventory = row.get(INVENTORY_KEY) or build_inventory(row, source=source)
        pending = pending_items(inventory)
        if pending:
            apply_summary(row)
            raise InventoryError(
                f"清单 {inventory['清单批次']} 仍有 {len(pending)} 个待核实项，暂不能确认",
                details={
                    "清单批次": inventory["清单批次"],
                    "清单指纹": inventory["指纹"],
                    "待核实项": [
                        {"原值": item["原值"], "保留原因": item["保留原因"],
                         "补核方式": item["补核方式"]}
                        for item in pending
                    ],
                },
            )
        if inventory.get("状态") == "已确认":
            apply_summary(row)
            return inventory

        inventory["状态"] = "已确认"
        inventory["确认时间"] = _now()
        inventory["确认人"] = actor
        inventory["来源"] = source
        _apply_bindings(row, inventory)
        apply_summary(row)
        return inventory


def verify_item(
    row: dict[str, Any], token: str, *, resolution: str = "", actor: str = "运维"
) -> dict[str, Any]:
    """补核一个旧项：保留原值和原因，只更新核实状态与结论。"""
    with INVENTORY_LOCK:
        inventory = row.get(INVENTORY_KEY) or build_inventory(row, source="核实条目")
        if inventory.get("状态") == "已确认":
            raise InventoryError("清单已确认，如需补核请先重新迁移生成新的草稿修订")
        entry = None
        for candidate in inventory["条目"]:
            if token in {candidate.get("原值"), candidate.get("特征身份")}:
                entry = candidate
                break
        if entry is None:
            raise InventoryError(f"清单中没有项 {token}，无需补核")
        if entry.get("条目类型") == KIND_LABEL:
            raise InventoryError(
                f"{token} 是版本标签而非特征，来源已记录为 {entry.get('版本来源')}，无需核实")
        if entry.get("条目类型") == KIND_FEATURE and entry.get("核实状态") == VERIFY_CONFIRMED:
            raise InventoryError(f"{token} 已登记为真实特征，绑定可直接从版本管理读取")

        entry["核实状态"] = "已核实"
        entry["核实结论"] = resolution or "经人工核实为与本次切换无关的旧项，保留原值归档"
        entry["补核方式"] = ""
        entry["核实人"] = actor
        entry["核实时间"] = _now()
        if entry.get("条目类型") == KIND_UNKNOWN:
            entry["保留原因"] = (
                f"{entry.get('保留原因') or '原值已保留'}；已核实：{entry['核实结论']}（原值不删除）")
        refresh_fingerprint(inventory)
        apply_summary(row)
        return inventory


def read_inventory(row: dict[str, Any]) -> dict[str, Any]:
    """读取迁移样本；如果端点清单被外部改动，会重新出草稿修订而不是返回过期确认件。"""
    return build_inventory(row, source="迁移样本")


def _endpoint_contains(row: dict[str, Any], feature: str) -> bool:
    inventory = row.get(INVENTORY_KEY)
    if inventory:
        return any(
            item["条目类型"] == KIND_FEATURE and item["特征身份"] == feature
            for item in inventory["条目"]
        )
    return feature in parse_legacy_list(raw_legacy_list(row))


def endpoints_for_feature(feature: str, *, running_only: bool = True) -> list[dict[str, Any]]:
    rows = []
    for row in store.rows("online_serv"):
        if running_only and row.get("status") != STATUS_RUNNING:
            continue
        if _endpoint_contains(row, feature):
            rows.append(row)
    return rows


def current_binding(row: dict[str, Any], feature: str) -> dict[str, Any] | None:
    """读取端点当前对某特征的绑定：第二版版本数据优先，旧清单兜底。"""
    for entry in row.get(VERSIONS_KEY) or []:
        if entry.get("特征身份") == feature:
            return {**entry, "值来源": "端点特征版本数据"}

    inventory = row.get(INVENTORY_KEY)
    if inventory:
        for item in inventory["条目"]:
            if item["条目类型"] == KIND_FEATURE and item["特征身份"] == feature:
                return {
                    "特征身份": feature,
                    "绑定版本": item.get("绑定版本", ""),
                    "版本来源": item.get("版本来源", ""),
                    "值": item.get("可服务值"),
                    "值来源": "端点清单",
                }

    # 尚未迁移的历史端点：按旧清单 + 版本管理里已能确认的绑定读取旧值。
    if feature not in parse_legacy_list(raw_legacy_list(row)):
        return None
    if feature not in _known_features():
        return None
    versions = version_registry().get(feature, [])
    chosen = choose_version(versions)
    label = str(chosen.get("版本标签", "")) if chosen else ""
    return {
        "特征身份": feature,
        "绑定版本": label,
        "版本来源": f"version_mgr/{chosen.get('版本编号', '')}" if chosen else "旧清单",
        "值": serving_value(feature, label),
        "值来源": "旧清单兼容读取",
    }


def readiness(
    row: dict[str, Any], *, feature: str, version_label: str = ""
) -> dict[str, Any]:
    """判断此刻提交全量切换是否会生效，以及未知项怎样补核。"""
    inventory = row.get(INVENTORY_KEY) or build_inventory(row, source="切换预检")
    pending = pending_items(inventory)
    binding = current_binding(row, feature)
    target_value = serving_value(feature, version_label) if version_label else None

    blockers: list[str] = []
    if row.get("status") != STATUS_RUNNING:
        blockers.append(f"端点 {row.get('端点编号', '')} 当前状态为 {row.get('status')}，不在运行中")
    if binding is None:
        blockers.append(f"端点清单中不存在特征 {feature}")
    if inventory.get("状态") != "已确认":
        blockers.append(
            f"端点仍使用旧版特征清单（清单 {inventory['清单批次']} 为{inventory['状态']}），切换前需补办迁移")
    if pending:
        reasons = "；".join(
            f"{item['原值']}（{item['补核方式'] or '需人工补核'}）" for item in pending)
        blockers.append(f"存在 {len(pending)} 个未核实旧项：{reasons}")

    return {
        "端点编号": row.get("端点编号", ""),
        "端点状态": row.get("status", ""),
        "关联特征": feature,
        "目标版本": version_label,
        "可否生效": not blockers,
        "阻断原因": blockers,
        "清单批次": inventory["清单批次"],
        "清单状态": inventory["状态"],
        "清单指纹": inventory["指纹"],
        "待核实项": [
            {"原值": item["原值"], "保留原因": item["保留原因"],
             "补核方式": item["补核方式"]}
            for item in pending
        ],
        "当前绑定版本": (binding or {}).get("绑定版本", ""),
        "当前值": (binding or {}).get("值"),
        "目标版本值": target_value,
        "检查时间": _now(),
    }


def read_feature_value(
    row: dict[str, Any], feature: str, *, preview_version: str = ""
) -> tuple[dict[str, Any] | None, str]:
    """从端点读取实际特征值；preview_version 只做目标版本预览，不改绑定。"""
    if row.get("status") != STATUS_RUNNING:
        return None, f"端点 {row.get('端点编号', '')} 未运行（{row.get('status')}），无法提供在线特征值"

    inventory = row.get(INVENTORY_KEY)
    if inventory:
        types = {
            item["原值"]: item["条目类型"] for item in inventory["条目"]
        }
        if types.get(feature) == KIND_LABEL:
            return None, f"{feature} 是版本标签而非特征身份，已从特征列表中剥离"
        if feature not in {
            item["特征身份"] for item in inventory["条目"]
            if item["条目类型"] == KIND_FEATURE
        }:
            return None, f"端点清单中没有已核实的特征 {feature}"

    binding = current_binding(row, feature)
    if binding is None or binding.get("值") is None:
        return None, f"端点未登记特征 {feature} 的可服务绑定"

    payload = {
        "端点编号": row.get("端点编号", ""),
        "运行状态": row.get("运行状态", ""),
        "特征身份": feature,
        "绑定版本": binding.get("绑定版本", ""),
        "版本来源": binding.get("版本来源", ""),
        "值": binding.get("值"),
        "值来源": binding.get("值来源", ""),
        "预览": False,
    }
    if preview_version and preview_version != binding.get("绑定版本"):
        payload.update({
            "绑定版本": preview_version,
            "值": serving_value(feature, preview_version),
            "预览": True,
            "说明": f"目标版本 {preview_version} 预览值，尚未绑定；当前实际绑定为 "
                    f"{binding.get('绑定版本') or '无版本'}",
        })
    if row.get(INVENTORY_KEY):
        payload["清单指纹"] = row[INVENTORY_KEY]["指纹"]
    receipt = row.get("最近切换回执")
    if receipt:
        payload["切换回执结果"] = receipt.get("结果")
    return payload, ""


def rebind_feature(
    row: dict[str, Any], *, feature: str, version_label: str, version_no: str
) -> None:
    """切换生效后只改这一个特征的绑定，其余特征绑定保持原样。

    确认过的端点清单是本次迁移的复查凭据，切换不再改写它：清单保持“已确认”
    和原指纹，实际绑定只写入第二版端点特征版本数据，因此端点始终只有一份
    可复查清单，回执里的指纹与清单完全对得上。
    """
    with INVENTORY_LOCK:
        inventory = row.get(INVENTORY_KEY)
        if inventory is None or inventory.get("状态") != "已确认":
            raise InventoryError(f"端点 {row.get('端点编号', '')} 尚未确认端点清单，不能写入绑定")
        value = serving_value(feature, version_label)
        found = False
        for entry in row.get(VERSIONS_KEY) or []:
            if entry.get("特征身份") == feature:
                entry["绑定版本"] = version_label
                entry["版本来源"] = f"version_mgr/{version_no}"
                entry["值"] = value
                found = True
        if not found:
            raise InventoryError(f"端点特征版本数据中没有 {feature}，请先补办迁移")
        # 切换记录留在版本管理回执和端点字段里，清单只读。
        row["最近切换"] = _now()
