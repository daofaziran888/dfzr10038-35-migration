"""端点特征版本解析：把旧在线端点里逗号拼接的特征/版本标签拆成可分别读取的身份与绑定。

旧版端点把版本标签（如 v1、v2）直接拼进 ``特征列表``，导致：
1. 特征身份和绑定版本混在一个字符串里，无法分别读取；
2. ``v3`` 这类真实特征名与版本标签长得一样，简单按前缀删除会误删真实特征；
3. 来自不同来源的旧标签、与本次切换无关的未知项无法追溯。

这里的规则是纯函数式的：输入旧清单和登记信息，输出条目，不直接改写仓库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 旧格式里“看起来像版本标签”的写法。是否真是标签还要结合特征注册判断，
# 因为 v3 也可能是真实在用的特征名。
VERSION_LABEL_PATTERN = re.compile(r"^[vV]\d+(?:\.\d+)*$")

KIND_FEATURE = "特征"
KIND_LABEL = "版本标签"
KIND_UNKNOWN = "未知项"

VERIFY_CONFIRMED = "已核实"
VERIFY_LABEL = "来源可查"
VERIFY_PENDING = "待核实"

# 端点特征版本数据对外提供的实际特征值。
# 键为特征身份，二级键为绑定版本（无版本时用空串）。
FEATURE_SERVING_VALUES: dict[str, dict[str, int]] = {
    "order_amount_30d": {"v2": 10, "v3": 12},
    "user_age_7d": {"": 7, "v1": 7, "v2": 8},
    "user_age_bucket_7d": {"": 3},
    "v3": {"": 3},
}


def parse_legacy_list(raw: str) -> list[str]:
    """拆开逗号分隔的旧特征清单，去空、去重并保持原有顺序。"""
    seen: set[str] = set()
    tokens: list[str] = []
    for piece in str(raw or "").split(","):
        token = piece.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def parse_label_sources(raw: str) -> dict[str, str]:
    """解析旧标签来源字段，如 ``version_mgr:v2;手动备注:v1``。"""
    sources: dict[str, str] = {}
    for piece in str(raw or "").split(";"):
        item = piece.strip()
        if not item or ":" not in item:
            continue
        source, label = item.split(":", 1)
        source, label = source.strip(), label.strip()
        if source and label:
            sources[label] = source
    return sources


def is_version_label(token: str) -> bool:
    return bool(VERSION_LABEL_PATTERN.match(token))


def serving_value(feature: str, version: str) -> int | None:
    """按端点特征版本数据取实际特征值；未知身份返回 None。"""
    versions = FEATURE_SERVING_VALUES.get(feature)
    if versions is None:
        return None
    if version in versions:
        return versions[version]
    return versions.get("")


@dataclass
class InventoryItem:
    """一条端点特征版本条目：身份与绑定版本分开保存。"""

    特征身份: str
    条目类型: str
    绑定版本: str = ""
    版本来源: str = ""
    核实状态: str = VERIFY_CONFIRMED
    原值: str = ""
    保留原因: str = ""
    可服务值: int | None = None
    补核方式: str = ""
    核实结论: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "特征身份": self.特征身份,
            "条目类型": self.条目类型,
            "绑定版本": self.绑定版本,
            "版本来源": self.版本来源,
            "核实状态": self.核实状态,
            "原值": self.原值,
            "保留原因": self.保留原因,
            "可服务值": self.可服务值,
            "补核方式": self.补核方式,
            "核实结论": self.核实结论,
        }


@dataclass
class Inventory:
    """一份端点清单：身份/绑定拆开后的完整条目集合。"""

    endpoint_no: str
    batch: str
    revision: int
    items: list[InventoryItem] = field(default_factory=list)

    @property
    def pending(self) -> list[InventoryItem]:
        return [item for item in self.items if item.核实状态 == VERIFY_PENDING]

    @property
    def labels(self) -> list[InventoryItem]:
        return [item for item in self.items if item.条目类型 == KIND_LABEL]

    @property
    def features(self) -> list[InventoryItem]:
        return [item for item in self.items if item.条目类型 == KIND_FEATURE]

    def binding_for(self, feature: str) -> InventoryItem | None:
        for item in self.items:
            if item.条目类型 == KIND_FEATURE and item.特征身份 == feature:
                return item
        return None


def classify_tokens(
    tokens: list[str],
    *,
    known_features: set[str],
    label_sources: dict[str, str],
) -> list[InventoryItem]:
    """把旧清单里的每个 token 归类，保证不删除任何一项。

    - 在特征注册里查得到的名字一律按真实特征保留（包括 v3）；
    - 形如 v1/v2 且未注册的才是旧版本标签，保留原值和来源；
    - 其余是待核实未知项，保留原值和原因。
    """
    items: list[InventoryItem] = []
    for token in tokens:
        if token in known_features:
            items.append(InventoryItem(
                特征身份=token,
                条目类型=KIND_FEATURE,
                原值=token,
                核实状态=VERIFY_CONFIRMED,
            ))
            continue
        if is_version_label(token):
            source = label_sources.get(token, "旧清单拼接")
            items.append(InventoryItem(
                特征身份="",
                条目类型=KIND_LABEL,
                绑定版本=token,
                版本来源=source,
                核实状态=VERIFY_LABEL,
                原值=token,
                保留原因=f"旧清单拼接的版本标签，来源：{source}；已从特征身份中剥离并保留原值",
            ))
            continue
        items.append(InventoryItem(
            特征身份=token,
            条目类型=KIND_UNKNOWN,
            核实状态=VERIFY_PENDING,
            原值=token,
            保留原因="与金额特征切换无关，且在特征注册/版本管理中均无法核实，保留原值待人工补核",
            补核方式="在端点清单中人工核实后对该条目执行「核实条目」，或登记对应特征/版本后再迁移",
        ))
    return items


def bind_verified_versions(
    items: list[InventoryItem],
    *,
    version_registry: dict[str, list[dict[str, Any]]],
) -> None:
    """为已核实的特征补上已确认的绑定版本和实际服务值。

    ``version_registry`` 以特征身份为键，值是版本管理记录列表，
    优先取“全量生效”的版本，其次取最后一条可确认版本。
    """
    for item in items:
        if item.条目类型 != KIND_FEATURE:
            continue
        versions = version_registry.get(item.特征身份, [])
        chosen = _choose_version(versions)
        if chosen is not None:
            item.绑定版本 = str(chosen.get("版本标签", ""))
            item.版本来源 = f"version_mgr/{chosen.get('版本编号', '')}"
        item.可服务值 = serving_value(item.特征身份, item.绑定版本)


def _choose_version(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not versions:
        return None
    for row in reversed(versions):
        if row.get("status") == "全量生效":
            return row
    return versions[-1]


def inventory_fingerprint(items: list[InventoryItem]) -> str:
    """给清单算一个可复查的指纹，用于确认“只有一份清单”。

    指纹覆盖条目类型、身份、绑定、原值与核实结论：补核会改变指纹，
    但确认动作本身不再改指纹，回执与清单能一一对应。
    """
    import hashlib

    material = "|".join(
        f"{item.条目类型}:{item.特征身份}:{item.绑定版本}:{item.原值}:"
        f"{item.核实状态}:{item.核实结论}"
        for item in items
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
