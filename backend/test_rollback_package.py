"""Schema 回滚包集成测试：导出、导入、试读、提交与拦截链路。

验收场景：
    1. 为改过列名且有同值依据的主表生成回滚包文件，字段对应明确快照；
    2. Schema 注册从回滚包读取目标字段和受影响的数据表；
    3. 用原从表读取请求试跑，审批记录数据版本和返回结果；
    4. 依赖未变更时提交确认；
    5. 无同值证据的文件提交须拦截；
    6. 试读后改变从表依赖，旧方案提交须拦截。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.store import store
from app.services import service_for

DATA_ROOT = os.path.join(os.path.dirname(__file__), "app", "data")
PACKAGE_DIR = os.path.join(DATA_ROOT, "rollback_packages")
SLAVE_FILE = os.path.join(DATA_ROOT, "slave_tables", "dws_order_daily.json")

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
    else:
        print(f"  {RED}FAIL{RESET} {desc} {detail}")
        failed += 1


def test_build_package_with_evidence():
    """改过列名且有同值依据的主表 → 生成可下载的回滚包文件。"""
    print("\n=== 场景1: 生成 Schema 回滚包（有同值依据） ===")
    svc = service_for("version_snapshot")
    package, msg = svc.build_rollback_package("DWD_ORDER_MASTER")
    check("回滚包生成成功", package is not None, msg)

    package_file = os.path.join(PACKAGE_DIR, "schema_rollback_DWD_ORDER_MASTER.json")
    check("回滚包文件已写入", os.path.exists(package_file), package_file)

    fields = {f["field"]: f for f in package["fields"]}
    check("包含依赖字段关系", set(fields) == {"order_amount", "buyer_no"})
    check("字段对应明确快照",
          all(f["snapshot_id"] == "VERS-0007" and f["snapshot_verified"] for f in fields.values()))
    check("有同值依据的字段可执行", all(f["executable"] for f in fields.values()))
    check("受影响从表已识别",
          fields["order_amount"]["affected_tables"] == ["dws_order_daily"]
          and fields["buyer_no"]["affected_tables"] == ["ads_order_report", "dws_order_daily"])
    check("快照片段已打包", any(s["snapshot_id"] == "VERS-0007" for s in package["snapshots"]))


def test_import_trial_and_confirm():
    """导入回滚包 → 原从表读取请求试跑 → 提交审批确认。"""
    print("\n=== 场景2: 导入 → 试读 → 提交确认 ===")
    snap_svc = service_for("version_snapshot")
    reg_svc = service_for("schema_registry")
    package, _ = snap_svc.build_rollback_package("DWD_ORDER_MASTER")

    entry, msg = reg_svc.import_rollback_package(package)
    check("回滚审批已生成", entry is not None, msg)
    check("目标字段来自回滚包", entry["目标字段"] == "order_amount、buyer_no", entry["目标字段"])
    check("受影响表来自回滚包",
          entry["受影响表"] == "ads_order_report、dws_order_daily", entry["受影响表"])

    entry, msg, done = reg_svc.trial_read_rollback(entry["id"])
    check("从表试读完成", done, msg)
    check("审批记录试读数据版本", entry["数据版本"] == "2026.11.20-v2.3", entry["数据版本"])

    results = {r["table"]: r for r in entry["_trial_results"]}
    daily = results["dws_order_daily"]
    check("从表保留当前列名", daily["retained_current_names"] and daily["requested_columns"] == ["order_amount", "buyer_no"])
    check("试读返回实际值",
          daily["returned_rows"][0] == {"order_amount": 128.5, "buyer_no": "B1001"},
          str(daily["returned_rows"][:1]))

    entry, msg, confirmed = reg_svc.submit_rollback_approval(entry["id"])
    check("依赖未变更时审批确认", confirmed and entry["status"] == "已确认", msg)


def test_block_package_without_evidence():
    """无同值证据的文件 → 提交须拦住。"""
    print("\n=== 场景3: 无同值依据的回滚包被拦截 ===")
    snap_svc = service_for("version_snapshot")
    reg_svc = service_for("schema_registry")
    package, _ = snap_svc.build_rollback_package("DWD_REFUND_MASTER")
    check("无依据字段标记为不可执行",
          all(not f["executable"] for f in package["fields"]))

    entry, _ = reg_svc.import_rollback_package(package)
    check("无依据字段不作为可执行目标", entry["目标字段"] == "（无可执行字段）", entry["目标字段"])

    entry, msg, done = reg_svc.trial_read_rollback(entry["id"])
    check("试读可执行并记录版本", done and entry["数据版本"] == "2026.11.20-v1.8", msg)

    entry, msg, confirmed = reg_svc.submit_rollback_approval(entry["id"])
    check("提交被拦截", not confirmed and entry["status"] == "已拦截", msg)
    check("拦截原因指向同值依据", "同值依据" in entry["拦截原因"], entry["拦截原因"])


def test_block_after_dependency_change():
    """试读后改变从表依赖 → 旧方案提交须拦住。"""
    print("\n=== 场景4: 试读后从表依赖变更被拦截 ===")
    snap_svc = service_for("version_snapshot")
    reg_svc = service_for("schema_registry")
    package, _ = snap_svc.build_rollback_package("DWD_ORDER_MASTER")
    entry, _ = reg_svc.import_rollback_package(package)
    entry, msg, done = reg_svc.trial_read_rollback(entry["id"])
    check("试读完成待确认", done and entry["status"] == "待确认", msg)

    with open(SLAVE_FILE, "r", encoding="utf-8") as f:
        original = f.read()
    try:
        slave = json.loads(original)
        slave["depends_on"] = sorted(set(slave["depends_on"]) | {"order_id"})
        with open(SLAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(slave, f, ensure_ascii=False, indent=2)

        entry, msg, confirmed = reg_svc.submit_rollback_approval(entry["id"])
        check("依赖变更后提交被拦截", not confirmed and entry["status"] == "已拦截", msg)
        check("拦截原因识别已变更依赖",
              "依赖已变更" in entry["拦截原因"] and "dws_order_daily" in entry["拦截原因"],
              entry["拦截原因"])
    finally:
        with open(SLAVE_FILE, "w", encoding="utf-8") as f:
            f.write(original)

    # 被拦截的旧方案不得复活；依赖恢复后需重新导入新方案才能确认
    blocked_id = entry["id"]
    retry, msg, done = reg_svc.trial_read_rollback(blocked_id)
    check("已拦截的旧方案不能再试读", not done and retry is None, msg)
    retry, msg, confirmed = reg_svc.submit_rollback_approval(blocked_id)
    check("已拦截的旧方案提交仍被拦住", not confirmed and retry["status"] == "已拦截", msg)

    package, _ = snap_svc.build_rollback_package("DWD_ORDER_MASTER")
    new_entry, _ = reg_svc.import_rollback_package(package)
    new_entry, msg, done = reg_svc.trial_read_rollback(new_entry["id"])
    check("依赖恢复后新方案可试读", done, msg)
    new_entry, msg, confirmed = reg_svc.submit_rollback_approval(new_entry["id"])
    check("依赖恢复后新方案确认", confirmed and new_entry["status"] == "已确认", msg)


def test_audit_trail():
    """回滚链路全程留痕。"""
    print("\n=== 场景5: 审计留痕 ===")
    audit_rows = store.rows("audit_log")
    actions = {r.get("操作类型") for r in audit_rows}
    for expected in ("导出回滚包", "导入回滚包", "试读从表", "确认回滚", "拦截回滚"):
        check(f"审计含「{expected}」", expected in actions)


def cleanup_generated_files():
    """清理测试生成的回滚包/审批文件，保持数据目录干净。"""
    if not os.path.isdir(PACKAGE_DIR):
        return
    for fname in os.listdir(PACKAGE_DIR):
        if fname.startswith(("schema_rollback_", "approval_")) and fname.endswith(".json"):
            os.remove(os.path.join(PACKAGE_DIR, fname))


if __name__ == "__main__":
    print("=" * 60)
    print("Schema 回滚包集成测试")
    print("=" * 60)

    cleanup_generated_files()
    try:
        test_build_package_with_evidence()
        test_import_trial_and_confirm()
        test_block_package_without_evidence()
        test_block_after_dependency_change()
        test_audit_trail()
    finally:
        cleanup_generated_files()

    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
