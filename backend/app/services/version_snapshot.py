"""version_snapshot 领域服务：版本快照管理，接收 asset_catalog 和 schema_registry 上游事件。

跨模块数据流：
    asset_catalog → version_snapshot: 资产归档触发快照创建
    asset_catalog → schema_registry → version_snapshot: Schema注册后创建版本快照
    version_snapshot → lineage_graph: 快照创建更新血缘信息
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "snapshots")
ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "registry_archive")
SLAVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "slave_tables")
PACKAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "rollback_packages")


class VersionSnapshotService(BaseModuleService):
    MODULE = "version_snapshot"
    ENTITY = "数据快照"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["快照编号", "关联资产", "快照时间", "数据量", "快照类型", "保留天数", "依赖快照", "快照状态"]
    REQUIRED_FIELDS = ["快照编号", "关联资产", "快照时间"]
    STATUS_ORDER = ["创建中", "已完成", "已过期", "已删除"]
    ACTION_RULES = {"确认完成": "已完成", "标记过期": "已过期", "安全删除": "已删除"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(PACKAGE_DIR, exist_ok=True)

    def build_rollback_package(self, asset_id: str) -> tuple[dict[str, Any] | None, str]:
        """导出 Schema 回滚包：快照片段 + 依赖字段关系，供发布人员核对字段依赖。

        回滚包中的字段必须对应明确快照；注册档案里拿不出同值依据的字段
        保留在包内供核对，但标记为不可执行目标。
        """
        archive = self._load_registry_archive(asset_id)
        if archive is None:
            return None, f"资产 {asset_id} 没有注册档案，无法生成Schema回滚包"

        slave_tables = self._load_slave_tables(asset_id)
        fields: list[dict[str, Any]] = []
        fragments: dict[str, dict[str, Any]] = {}
        for rename in archive.get("renames", []):
            snapshot_id = str(rename.get("snapshot_id", ""))
            fragment = self._snapshot_fragment(asset_id, snapshot_id)
            if fragment is not None:
                fragments[snapshot_id] = fragment
            evidence = rename.get("same_value_evidence")
            new_name = str(rename.get("new_name", ""))
            affected = sorted(
                slave["table"] for slave in slave_tables
                if new_name in slave.get("depends_on", [])
            )
            fields.append({
                "field": str(rename.get("field", "")),
                "old_name": str(rename.get("old_name", "")),
                "new_name": new_name,
                "snapshot_id": snapshot_id,
                "snapshot_verified": fragment is not None,
                "same_value_evidence": evidence,
                "executable": bool(evidence) and fragment is not None,
                "affected_tables": affected,
            })

        package = {
            "package_id": f"RBPK-{asset_id}-{int(datetime.now(timezone.utc).timestamp())}",
            "package_type": "schema_rollback",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "asset_id": asset_id,
            "table": str(archive.get("table", "")),
            "snapshots": [fragments[key] for key in sorted(fragments)],
            "fields": fields,
        }
        path = os.path.join(PACKAGE_DIR, f"schema_rollback_{asset_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(package, f, ensure_ascii=False, indent=2, default=str)

        self._write_audit_entry(
            entity="版本快照",
            action="导出回滚包",
            target_id=package["package_id"],
            detail=f"Schema回滚包: {asset_id}, 字段{len(fields)}个, "
                   f"可执行{sum(1 for item in fields if item['executable'])}个",
        )
        return package, f"Schema回滚包已生成: {asset_id}（{len(fields)}个依赖字段）"

    def _load_registry_archive(self, asset_id: str) -> dict[str, Any] | None:
        path = os.path.join(ARCHIVE_DIR, f"{asset_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_slave_tables(self, asset_id: str) -> list[dict[str, Any]]:
        if not os.path.isdir(SLAVE_DIR):
            return []
        tables = []
        for fname in sorted(os.listdir(SLAVE_DIR)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(SLAVE_DIR, fname), "r", encoding="utf-8") as f:
                slave = json.load(f)
            if str(slave.get("master_asset", "")) == asset_id:
                tables.append(slave)
        return tables

    def _snapshot_fragment(self, asset_id: str, snapshot_id: str) -> dict[str, Any] | None:
        """按快照编号取快照片段，确认回滚包字段对应明确快照。"""
        if not snapshot_id:
            return None
        for row in store.rows(self.MODULE):
            if str(row.get("快照编号", "")) == snapshot_id:
                return {
                    "snapshot_id": snapshot_id,
                    "asset_id": str(row.get("关联资产", "")),
                    "snapshot_time": str(row.get("快照时间", "")),
                    "snapshot_type": str(row.get("快照类型", "")),
                    "data_volume": str(row.get("数据量", "")),
                    "status": str(row.get("快照状态", "")) or str(row.get("status", "")),
                }
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith(f"_{snapshot_id}.json"):
                continue
            with open(os.path.join(DATA_DIR, fname), "r", encoding="utf-8") as f:
                record = json.load(f)
            return {
                "snapshot_id": snapshot_id,
                "asset_id": str(record.get("asset_id", "")),
                "snapshot_time": str(record.get("snapshot_time", "")),
                "snapshot_type": str(record.get("snapshot_type", "")),
                "data_volume": str(record.get("data_volume", "")),
                "status": str(record.get("status", "")),
            }
        return None

    def create_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["快照时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["快照状态"] = "已完成"

        # 写入快照文件
        self._write_snapshot(entry)

        # 更新血缘图谱
        self._update_lineage_snapshot(asset_id)

        # 记录审计
        self._write_audit_entry(
            entity="版本快照",
            action="创建",
            target_id=str(entry.get("快照编号", "")),
            detail=f"快照创建: {asset_id}",
        )

        return entry, f"{self.ENTITY} {entry_id} 已创建"

    def list_snapshots_for_asset(self, asset_id: str) -> list[dict[str, Any]]:
        """列出某资产的所有版本快照。"""
        pattern = f"snapshot_{asset_id}_.json"
        results = []
        for fname in os.listdir(DATA_DIR):
            if fname.startswith(f"snapshot_{asset_id}_"):
                path = os.path.join(DATA_DIR, fname)
                with open(path, "r", encoding="utf-8") as f:
                    results.append(json.load(f))
        return sorted(results, key=lambda x: x.get("snapshot_time", ""), reverse=True)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认完成":
            return self.create_snapshot(entry_id)
        return super().run_action(entry_id, action)

    def _write_snapshot(self, entry: dict[str, Any]) -> None:
        asset_id = str(entry.get("关联资产", ""))
        snapshot_id = str(entry.get("快照编号", ""))
        filename = f"snapshot_{asset_id}_{snapshot_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "snapshot_time": str(entry.get("快照时间", "")),
            "data_volume": str(entry.get("数据量", "")),
            "snapshot_type": str(entry.get("快照类型", "")),
            "retention_days": str(entry.get("保留天数", "")),
            "dependent_snapshot": str(entry.get("依赖快照", "")),
            "status": str(entry.get("快照状态", "")),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_lineage_snapshot(self, asset_id: str) -> None:
        lineage_rows = store.rows("lineage_graph")
        for row in lineage_rows:
            if str(row.get("下游资产")) == asset_id:
                row["变更SHA"] = f"snap-{int(datetime.now(timezone.utc).timestamp())}"
                break

    def _write_audit_entry(self, entity: str, action: str, target_id: str,
                           detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": entity,
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        }
        audit_rows.append(entry)