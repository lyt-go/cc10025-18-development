"""schema_registry 领域服务：Schema注册管理，接收 asset_catalog 上游事件。

跨模块数据流：
    asset_catalog → schema_registry: 资产发布触发Schema注册
    schema_registry → version_snapshot: Schema注册触发版本快照创建
    version_snapshot → schema_registry: Schema回滚包导入，生成回滚审批
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")
SLAVE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "slave_tables")
TABLE_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "table_data")
PACKAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "rollback_packages")
ROLLBACK_MODULE = "schema_rollback"
ROLLBACK_STATUS = ["待试读", "待确认", "已确认", "已拦截"]


class SchemaRegistryService(BaseModuleService):
    MODULE = "schema_registry"
    ENTITY = "Schema版本"
    OPERATION_TERM = "入库"
    LIST_FIELDS = ["版本编号", "关联资产", "字段清单", "前向兼容", "后向兼容", "Schema摘要", "注册时间", "版本状态"]
    REQUIRED_FIELDS = ["版本编号", "关联资产", "字段清单"]
    STATUS_ORDER = ["草稿", "待校验", "已注册", "已废止"]
    ACTION_RULES = {"提交校验": "待校验", "确认注册": "已注册", "标记废止": "已废止"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(PACKAGE_DIR, exist_ok=True)

    # ---------- Schema 回滚审批 ----------

    def import_rollback_package(self, package: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """从 Schema 回滚包读取目标字段和受影响的数据表，生成回滚审批。

        只有带同值依据且对应明确快照的字段才进入可执行目标；
        拿不出依据的字段留在包内供核对，不作为可执行目标。
        """
        if not isinstance(package, dict) or package.get("package_type") != "schema_rollback":
            return None, "文件不是有效的Schema回滚包"
        asset_id = str(package.get("asset_id", "")).strip()
        fields = package.get("fields")
        if not asset_id or not isinstance(fields, list) or not fields:
            return None, "Schema回滚包缺少目标字段或关联资产"

        targets = [str(f.get("field", "")) for f in fields if f.get("executable")]
        affected = sorted({
            table
            for f in fields
            for table in (f.get("affected_tables") or [])
        })
        snapshot_ids = sorted({str(f.get("snapshot_id", "")) for f in fields if f.get("snapshot_id")})

        rows = store.rows(ROLLBACK_MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": next_id,
            "status": "待试读",
            "pending": True,
            "abnormal": False,
            "审批编号": f"RBAK-{next_id:04d}",
            "关联资产": asset_id,
            "目标字段": "、".join(targets) if targets else "（无可执行字段）",
            "受影响表": "、".join(affected) if affected else "（无）",
            "目标快照": "、".join(snapshot_ids),
            "数据版本": "",
            "试读结果": "",
            "拦截原因": "",
            "package_id": str(package.get("package_id", "")),
            "_package": package,
            "_affected_tables": affected,
            "_trial_fingerprint": "",
        }
        rows.append(entry)
        self._write_audit_entry(
            action="导入回滚包",
            target_id=entry["审批编号"],
            detail=f"Schema回滚包导入: {asset_id}, 可执行字段{len(targets)}个, 受影响表{len(affected)}张",
        )
        return entry, f"回滚审批 {entry['审批编号']} 已生成，目标字段{len(targets)}个，受影响表{len(affected)}张"

    def list_rollback_approvals(self) -> list[dict[str, Any]]:
        return [self._public_approval(row) for row in store.rows(ROLLBACK_MODULE)]

    def trial_read_rollback(self, approval_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """用候选方案试读实际从表：回放原从表读取请求，记录数据版本和返回结果。"""
        entry = store.find(ROLLBACK_MODULE, approval_id)
        if entry is None:
            return None, f"回滚审批 {approval_id} 不存在", False
        if entry.get("status") not in ("待试读", "待确认"):
            return None, f"回滚审批 {approval_id} 当前状态为{entry.get('status')}，不能试读", False

        asset_id = str(entry.get("关联资产", ""))
        master = self._load_table_data(asset_id)
        if master is None:
            return None, f"主表 {asset_id} 没有可试读的数据版本", False
        current_columns = [str(c) for c in master.get("columns", [])]
        data_version = str(master.get("data_version", ""))

        results = []
        broken = []
        for table in entry.get("_affected_tables", []):
            slave = self._load_slave_table(table)
            if slave is None:
                broken.append(f"{table}（从表定义缺失）")
                continue
            requested = [str(c) for c in slave.get("read_request", {}).get("columns", [])]
            missing = [c for c in requested if c not in current_columns]
            retained = not missing
            if not retained:
                broken.append(f"{table}（列名已变更: {'、'.join(missing)}）")
            returned = [
                {col: row.get(col) for col in requested}
                for row in master.get("rows", [])[:5]
            ]
            results.append({
                "table": table,
                "requested_columns": requested,
                "retained_current_names": retained,
                "missing_columns": missing,
                "data_version": data_version,
                "row_count": len(master.get("rows", [])),
                "returned_rows": returned,
            })

        entry["数据版本"] = data_version
        entry["_trial_fingerprint"] = self._dependency_fingerprint(entry.get("_affected_tables", []))
        entry["_trial_results"] = results
        entry["_trial_at"] = datetime.now(timezone.utc).isoformat()
        entry["试读结果"] = (
            f"试读{len(results)}张从表@版本{data_version}，"
            f"保留当前列名{sum(1 for r in results if r['retained_current_names'])}张"
        )
        if broken:
            entry["status"] = "待试读"
            entry["abnormal"] = True
            return entry, f"试读未完成：{'；'.join(broken)}", False

        entry["status"] = "待确认"
        entry["abnormal"] = False
        self._write_audit_entry(
            action="试读从表",
            target_id=str(entry.get("审批编号", "")),
            detail=entry["试读结果"],
        )
        return entry, f"试读完成：{entry['试读结果']}，可提交审批", True

    def submit_rollback_approval(self, approval_id: int) -> tuple[dict[str, Any] | None, str, bool]:
        """提交回滚审批。确认之前识别已变更的依赖；无同值依据或依赖已变更的旧方案一律拦截。"""
        entry = store.find(ROLLBACK_MODULE, approval_id)
        if entry is None:
            return None, f"回滚审批 {approval_id} 不存在", False
        if entry.get("status") == "已确认":
            return entry, f"回滚审批 {entry.get('审批编号')} 已确认过，无需重复提交", False
        if entry.get("status") != "待确认":
            return entry, f"回滚审批 {entry.get('审批编号')} 尚未完成试读，不能提交", False

        package = entry.get("_package") or {}
        fields = package.get("fields") or []
        no_evidence = [str(f.get("field", "")) for f in fields if not f.get("executable")]
        targets = [str(f.get("field", "")) for f in fields if f.get("executable")]
        if not targets:
            reason = "回滚包内字段均无同值依据，不得作为可执行目标"
            if no_evidence:
                reason += f"：{'、'.join(no_evidence)}"
            return self._block_approval(entry, reason)

        changed = self._changed_dependencies(entry)
        if changed:
            reason = "从表依赖已变更，旧方案被拦截：" + "、".join(changed)
            return self._block_approval(entry, reason)

        entry["status"] = "已确认"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["拦截原因"] = ""
        self._persist_approval(entry)
        self._write_audit_entry(
            action="确认回滚",
            target_id=str(entry.get("审批编号", "")),
            detail=f"回滚审批确认: {entry.get('关联资产')}@数据版本{entry.get('数据版本')}",
        )
        return entry, f"回滚审批 {entry.get('审批编号')} 已确认，依赖未变更", True

    # ---------- 内部工具 ----------

    def _block_approval(self, entry: dict[str, Any], reason: str) -> tuple[dict[str, Any], str, bool]:
        entry["status"] = "已拦截"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["拦截原因"] = reason
        self._persist_approval(entry)
        self._write_audit_entry(
            action="拦截回滚",
            target_id=str(entry.get("审批编号", "")),
            detail=reason,
        )
        return entry, f"回滚审批 {entry.get('审批编号')} 已拦截：{reason}", False

    def _changed_dependencies(self, entry: dict[str, Any]) -> list[str]:
        """确认之前识别已变更的依赖：对比试读时与当前的从表依赖指纹。"""
        baseline = str(entry.get("_trial_fingerprint", ""))
        tables = entry.get("_affected_tables", [])
        if not baseline:
            return [f"{t}（缺少试读基线）" for t in tables] or ["（缺少试读基线）"]
        current = self._dependency_fingerprint(tables)
        if current == baseline:
            return []
        changed = []
        trial_deps = json.loads(baseline)
        current_deps = json.loads(current)
        for table in tables:
            before = trial_deps.get(table)
            after = current_deps.get(table)
            if before != after:
                changed.append(f"{table}（{'/'.join(before or [])}→{'/'.join(after or [])}）")
        return changed or ["（依赖指纹不一致）"]

    def _dependency_fingerprint(self, tables: list[str]) -> str:
        deps: dict[str, list[str]] = {}
        for table in tables:
            slave = self._load_slave_table(table)
            deps[table] = sorted(str(c) for c in (slave or {}).get("depends_on", []))
        return json.dumps(deps, ensure_ascii=False, sort_keys=True)

    def _load_slave_table(self, table: str) -> dict[str, Any] | None:
        path = os.path.join(SLAVE_DIR, f"{table}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_table_data(self, asset_id: str) -> dict[str, Any] | None:
        path = os.path.join(TABLE_DATA_DIR, f"{asset_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _persist_approval(self, entry: dict[str, Any]) -> None:
        record = self._public_approval(entry)
        record["trial_results"] = entry.get("_trial_results", [])
        path = os.path.join(PACKAGE_DIR, f"approval_{entry.get('审批编号')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _public_approval(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def _write_audit_entry(self, action: str, target_id: str, detail: str) -> None:
        audit_rows = store.rows("audit_log")
        next_id = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
        audit_rows.append({
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": "Schema注册",
            "操作类型": action,
            "操作对象": target_id,
            "操作时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        })

    # ---------- 原有注册链路 ----------

    def register_schema(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        asset_id = str(entry.get("关联资产", ""))
        fields = str(entry.get("字段清单", ""))
        forward_ok = str(entry.get("前向兼容", ""))
        backward_ok = str(entry.get("后向兼容", ""))

        entry["status"] = "已注册"
        entry["pending"] = False
        entry["注册时间"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry["版本状态"] = "已注册"

        # 写入Schema注册文件
        self._write_schema_record(asset_id, entry)

        # 创建版本快照
        self._create_version_snapshot(asset_id, entry)

        return entry, f"{self.ENTITY} {entry_id} 已注册: {fields}"

    def get_schema_for_asset(self, asset_id: str) -> dict[str, Any] | None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认注册":
            return self.register_schema(entry_id)
        return super().run_action(entry_id, action)

    def _write_schema_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        filename = f"schema_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "schema_id": str(entry.get("版本编号", "")),
            "asset_id": asset_id,
            "fields": str(entry.get("字段清单", "")),
            "forward_compatible": str(entry.get("前向兼容", "")),
            "backward_compatible": str(entry.get("后向兼容", "")),
            "summary": str(entry.get("Schema摘要", "")),
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "version_status": "已注册",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _create_version_snapshot(self, asset_id: str, schema_entry: dict[str, Any]) -> None:
        snap_rows = store.rows("version_snapshot")
        next_id = max((int(r.get("id", 0)) for r in snap_rows), default=0) + 1
        snap = {
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"VERS-{next_id:04d}",
            "关联资产": asset_id,
            "快照时间": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "数据量": "0MB",
            "快照类型": "Schema快照",
            "保留天数": "90",
            "依赖快照": "",
            "快照状态": "已完成",
        }
        snap_rows.append(snap)