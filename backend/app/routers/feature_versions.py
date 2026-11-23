"""端点特征版本：端点清单迁移、切换预检、在线特征值读取和切换回执。

这些接口不替代通用资源路由，只承接“旧清单 → 端点特征版本数据”的迁移语义，
让运维在不改状态机的前提下完成迁移、核实、切换与补办。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, VerifyItemPayload
from app.services import endpoint_inventory as endp
from app.services.online_serv import service as online_service
from app.services.version_mgr import service as version_service

router = APIRouter(prefix="/api", tags=["端点特征版本"])


def _guard(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except endp.InventoryError as exc:
        raise HTTPException(status_code=409, detail={"message": exc.message, **exc.details})


# ---------------------------------------------------------------------------
# 在线服务端点
# ---------------------------------------------------------------------------


@router.get("/online_serv/{entry_id}/feature-inventory")
def get_feature_inventory(entry_id: int) -> dict[str, Any]:
    """迁移样本：身份与绑定分开的端点清单（幂等，已确认的不会被悄悄替换）。"""
    return _guard(online_service.inventory, entry_id)


@router.post("/online_serv/{entry_id}/feature-inventory/migrate")
def migrate_feature_inventory(entry_id: int, force: bool = Query(default=False)) -> dict[str, Any]:
    inventory = _guard(online_service.migrate, entry_id, force=force)
    return {
        "ok": True,
        "message": (
            f"已生成端点清单 {inventory['清单批次']} 第 {inventory['修订号']} 版草稿，"
            f"待核实 {sum(1 for i in inventory['条目'] if i['核实状态'] == '待核实')} 项"),
        "inventory": inventory,
    }


@router.post("/online_serv/{entry_id}/feature-inventory/confirm")
def confirm_feature_inventory(entry_id: int) -> ActionResult:
    inventory = _guard(online_service.confirm, entry_id)
    return ActionResult(
        ok=True,
        message=(f"端点清单 {inventory['清单批次']} 已确认，指纹 {inventory['指纹']}，"
                 f"端点特征版本数据已写入"),
        entry=inventory,
    )


@router.post("/online_serv/{entry_id}/feature-inventory/verify")
def verify_feature_inventory_item(entry_id: int, payload: VerifyItemPayload) -> dict[str, Any]:
    inventory = _guard(online_service.verify, entry_id, payload.token, resolution=payload.resolution or "")
    return {"ok": True, "message": f"已补核 {payload.token}，原值与原因保留", "inventory": inventory}


@router.get("/online_serv/{entry_id}/feature-inventory/pending")
def pending_feature_inventory_items(entry_id: int) -> dict[str, Any]:
    inventory = _guard(online_service.inventory, entry_id)
    pending = [item for item in inventory["条目"] if item["核实状态"] == "待核实"]
    return {
        "端点编号": inventory.get("清单批次", ""),
        "清单批次": inventory["清单批次"],
        "清单状态": inventory["状态"],
        "清单指纹": inventory["指纹"],
        "待核实项": pending,
        "补核方式": "逐项核实后调用 /feature-inventory/verify，再确认清单",
    }


@router.get("/online_serv/{entry_id}/features/{feature}/value")
def read_online_feature_value(
    entry_id: int, feature: str, preview_version: str | None = Query(default=None)
) -> dict[str, Any]:
    """在线服务按端点特征版本数据返回实际特征值；preview_version 只预览不绑定。"""
    return _guard(online_service.feature_value, entry_id, feature,
                  preview_version=preview_version or "")


@router.get("/online_serv/{entry_id}/readiness")
def endpoint_readiness(
    entry_id: int,
    feature: str = Query(...),
    version_label: str = Query(default=""),
) -> dict[str, Any]:
    """切换前判断此刻提交是否会生效，并列出未知项的补核方式。"""
    return _guard(online_service.readiness, entry_id,
                  feature=feature, version_label=version_label)


# ---------------------------------------------------------------------------
# 版本管理
# ---------------------------------------------------------------------------


@router.get("/version_mgr/{entry_id}/switch-preflight")
def switch_preflight(entry_id: int) -> dict[str, Any]:
    return _guard(version_service.preflight, entry_id)


@router.get("/version_mgr/{entry_id}/receipt")
def switch_receipt(entry_id: int) -> dict[str, Any]:
    return _guard(version_service.receipt, entry_id)


@router.get("/version_mgr/{entry_id}/receipts")
def switch_receipts(entry_id: int) -> dict[str, Any]:
    return {"版本编号": entry_id, "回执": _guard(version_service.receipts, entry_id)}
