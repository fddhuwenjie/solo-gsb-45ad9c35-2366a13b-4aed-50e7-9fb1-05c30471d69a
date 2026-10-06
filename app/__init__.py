"""陶瓷合窑装载确认台 — Flask 应用。

核心规则：
- 草案预览只做只读计算：共同峰温/保温闭区间、占位情况、冲突提示；
- 确认必须携带预览时的草案版本与各批次版本，过期则拒绝并提示刷新；
- 确认在单个 SQLite 事务内重查全部条件（窑位不重复、两区间均有交集、
  批次未被其他已确认窑次占用），任一失败整体不改动；
- confirmed_* 表只增不改，批次被某窑次占用由 UNIQUE(batch_id) 保证，
  并发争用同一批次时只有一个事务能提交；
- 烧成时段安排：仅面向已确认窑次，同一窑次只能安排一次；拟用峰温/保温
  必须落在该窑次确认时快照的共同区间内（不取批次后续修改的范围）；
  保存时在 BEGIN IMMEDIATE 事务内按数据库最新安排重查——同一台窑的
  时间段不得重叠（首尾相接允许），冲突指出占用窑次，失败则原安排不变，
  同窑并发争用同一时段只有一个事务能提交。
"""
import numbers
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

from .db import close_db, get_db, init_db


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def err(status, message, **extra):
        payload = {"error": message}
        payload.update(extra)
        return jsonify(payload), status

    def json_body():
        return request.get_json(silent=True) or {}

    def parse_range(data, low_key, high_key, low_name, integer=False):
        """解析并校验闭区间 [low, high]，返回 (low, high) 或错误字符串。"""
        low = data.get(low_key)
        high = data.get(high_key)
        if isinstance(low, bool) or isinstance(high, bool):
            return None, f"{low_name}上下限必须是数字"
        if integer:
            if not isinstance(low, int) or not isinstance(high, int):
                return None, f"{low_name}上下限必须是整数分钟"
        else:
            if not isinstance(low, numbers.Real) or not isinstance(high, numbers.Real):
                return None, f"{low_name}上下限必须是数字"
        if low < 0 or high < 0:
            return None, f"{low_name}不能为负数"
        if low > high:
            return None, f"{low_name}区间下限不能大于上限"
        return (low, high), None

    def touch_draft(db, draft_id):
        """草案内容变动，版本 +1。"""
        db.execute(
            "UPDATE drafts SET version = version + 1 WHERE id = ?", (draft_id,)
        )

    def draft_json(db, draft_id):
        draft = db.execute(
            """SELECT d.id, d.name, d.kiln_id, k.code AS kiln_code, d.version, d.status
               FROM drafts d JOIN kilns k ON k.id = d.kiln_id
               WHERE d.id = ?""",
            (draft_id,),
        ).fetchone()
        if draft is None:
            return None
        items = db.execute(
            """SELECT di.pos_no, b.id AS batch_id, b.code AS batch_code,
                      b.peak_min, b.peak_max, b.hold_min, b.hold_max, b.version AS batch_version
               FROM draft_items di JOIN batches b ON b.id = di.batch_id
               WHERE di.draft_id = ? ORDER BY di.pos_no""",
            (draft_id,),
        ).fetchall()
        return {
            "id": draft["id"],
            "name": draft["name"],
            "kiln_id": draft["kiln_id"],
            "kiln_code": draft["kiln_code"],
            "version": draft["version"],
            "status": draft["status"],
            "items": [dict(r) for r in items],
        }

    def build_preview(db, draft_id, *, stale=False):
        """只读计算预览。"""
        draft = draft_json(db, draft_id)
        if draft is None:
            return err(404, "草案不存在")
        if draft["status"] != "open":
            return err(409, "该草案已确认，不能再预览或修改", stale=stale)

        positions = [
            r["pos_no"]
            for r in db.execute(
                "SELECT pos_no FROM kiln_positions WHERE kiln_id = ? ORDER BY pos_no",
                (draft["kiln_id"],),
            )
        ]

        items = draft["items"]
        warnings = []
        if stale:
            warnings.append("草案或批次已被他人修改，当前预览已过期，请刷新后重新预览再确认")

        if not items:
            warnings.append("草案为空：请先放入批次")
            return jsonify(
                {
                    "draft_id": draft_id,
                    "draft_version": draft["version"],
                    "kiln_code": draft["kiln_code"],
                    "status": draft["status"],
                    "items": [],
                    "positions": positions,
                    "occupied": {},
                    "common_peak": None,
                    "common_hold": None,
                    "interval_ok": False,
                    "conflicts": [],
                    "warnings": warnings,
                    "ready": False,
                }
            )

        # 窑位重复检查
        seen = {}
        dup_positions = set()
        for it in items:
            p = it["pos_no"]
            if p in seen:
                dup_positions.add(p)
            seen[p] = it["batch_code"]
        conflicts = [
            {"type": "dup_position", "pos_no": p} for p in sorted(dup_positions)
        ]

        # 共同峰温 / 保温闭区间 = 各允许区间的交集
        peak_lo = max(i["peak_min"] for i in items)
        peak_hi = min(i["peak_max"] for i in items)
        hold_lo = max(i["hold_min"] for i in items)
        hold_hi = min(i["hold_max"] for i in items)
        peak_ok = peak_lo <= peak_hi
        hold_ok = hold_lo <= hold_hi

        if not peak_ok:
            blockers = [
                i["batch_code"]
                for i in items
                if i["peak_max"] < peak_lo or i["peak_min"] > peak_hi
            ]
            warnings.append(
                "峰温区间无交集：" + "、".join(blockers) + " 的峰温范围无法共同满足"
            )
        if not hold_ok:
            blockers = [
                i["batch_code"]
                for i in items
                if i["hold_max"] < hold_lo or i["hold_min"] > hold_hi
            ]
            warnings.append(
                "保温分钟区间无交集：" + "、".join(blockers) + " 的保温范围无法共同满足"
            )

        # 批次是否已被其他已确认窑次使用
        used = db.execute(
            """SELECT cli.batch_id, cli.batch_code, cli.pos_no, cl.id AS load_id,
                      k.code AS kiln_code
               FROM confirmed_load_items cli
               JOIN confirmed_loads cl ON cl.id = cli.load_id
               JOIN kilns k ON k.id = cl.kiln_id
               WHERE cli.batch_id IN (%s)""" % ",".join("?" * len(items)),
            [i["batch_id"] for i in items],
        ).fetchall()
        used_rows = [dict(r) for r in used]
        for r in used_rows:
            conflicts.append(
                {
                    "type": "batch_used",
                    "batch_id": r["batch_id"],
                    "batch_code": r["batch_code"],
                    "load_id": r["load_id"],
                    "kiln_code": r["kiln_code"],
                    "pos_no": r["pos_no"],
                }
            )
            warnings.append(
                f"批次 {r['batch_code']} 已被已确认窑次 #{r['load_id']}"
                f"（{r['kiln_code']} {r['pos_no']} 号位）使用"
            )

        occupied = {i["pos_no"]: i["batch_code"] for i in items}
        interval_ok = peak_ok and hold_ok
        ready = interval_ok and not conflicts

        return jsonify(
            {
                "draft_id": draft_id,
                "draft_version": draft["version"],
                "kiln_code": draft["kiln_code"],
                "status": draft["status"],
                "items": items,
                "positions": positions,
                "occupied": occupied,
                "common_peak": (
                    {"min": peak_lo, "max": peak_hi} if peak_ok else None
                ),
                "common_hold": (
                    {"min": hold_lo, "max": hold_hi} if hold_ok else None
                ),
                "interval_ok": interval_ok,
                "conflicts": conflicts,
                "warnings": warnings,
                "ready": ready,
            }
        )

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ------------------------------------------------------------------
    # 批次登记
    # ------------------------------------------------------------------

    @app.route("/api/kilns", methods=["GET"])
    def list_kilns():
        db = get_db()
        kilns = db.execute("SELECT id, code, name FROM kilns ORDER BY id").fetchall()
        result = []
        for k in kilns:
            positions = [
                r["pos_no"]
                for r in db.execute(
                    "SELECT pos_no FROM kiln_positions WHERE kiln_id = ? ORDER BY pos_no",
                    (k["id"],),
                )
            ]
            result.append({**dict(k), "positions": positions})
        return jsonify(result)

    @app.route("/api/batches", methods=["GET"])
    def list_batches():
        db = get_db()
        rows = db.execute(
            """SELECT b.*, cli.load_id AS used_by_load
               FROM batches b
               LEFT JOIN confirmed_load_items cli ON cli.batch_id = b.id
               ORDER BY b.id"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/batches", methods=["POST"])
    def create_batch():
        data = json_body()
        code = (data.get("code") or "").strip()
        if not code:
            return err(400, "批次编号不能为空")
        peak, e1 = parse_range(data, "peak_min", "peak_max", "峰温")
        if e1:
            return err(400, e1)
        hold, e2 = parse_range(data, "hold_min", "hold_max", "保温分钟", integer=True)
        if e2:
            return err(400, e2)
        db = get_db()
        try:
            cur = db.execute(
                """INSERT INTO batches(code, peak_min, peak_max, hold_min, hold_max)
                   VALUES (?, ?, ?, ?, ?)""",
                (code, peak[0], peak[1], hold[0], hold[1]),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return err(409, f"批次编号 {code} 已存在")
        row = db.execute("SELECT * FROM batches WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(dict(row)), 201

    @app.route("/api/batches/<int:batch_id>", methods=["PUT"])
    def update_batch(batch_id):
        data = json_body()
        peak, e1 = parse_range(data, "peak_min", "peak_max", "峰温")
        if e1:
            return err(400, e1)
        hold, e2 = parse_range(data, "hold_min", "hold_max", "保温分钟", integer=True)
        if e2:
            return err(400, e2)
        expect_version = data.get("version")
        if not isinstance(expect_version, int):
            return err(400, "缺少批次版本号 version")
        db = get_db()
        cur = db.execute(
            """UPDATE batches
                  SET peak_min = ?, peak_max = ?, hold_min = ?, hold_max = ?,
                      version = version + 1,
                      updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ? AND version = ?""",
            (peak[0], peak[1], hold[0], hold[1], batch_id, expect_version),
        )
        if cur.rowcount == 0:
            row = db.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if row is None:
                return err(404, "批次不存在")
            return err(
                409,
                "批次已被他人修改，版本已过期，请刷新后重试",
                stale=True,
            )
        db.commit()
        row = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        return jsonify(dict(row))

    @app.route("/api/batches/<int:batch_id>", methods=["DELETE"])
    def delete_batch(batch_id):
        db = get_db()
        used = db.execute(
            "SELECT 1 FROM confirmed_load_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if used:
            return err(409, "批次已被已确认窑次使用，不可删除（确认记录不可变）")
        in_draft = db.execute(
            "SELECT d.name FROM draft_items di JOIN drafts d ON d.id = di.draft_id WHERE di.batch_id = ?",
            (batch_id,),
        ).fetchall()
        if in_draft:
            return err(409, "批次仍在草案（" + "、".join(r["name"] for r in in_draft) + "）中，请先移出")
        cur = db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
        if cur.rowcount == 0:
            return err(404, "批次不存在")
        db.commit()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # 草案
    # ------------------------------------------------------------------

    @app.route("/api/drafts", methods=["GET"])
    def list_drafts():
        db = get_db()
        rows = db.execute(
            """SELECT d.id, d.name, k.code AS kiln_code, d.version, d.status,
                      COUNT(di.id) AS item_count
                 FROM drafts d
                 JOIN kilns k ON k.id = d.kiln_id
                 LEFT JOIN draft_items di ON di.draft_id = d.id
                GROUP BY d.id ORDER BY d.id"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/drafts", methods=["POST"])
    def create_draft():
        data = json_body()
        name = (data.get("name") or "").strip()
        kiln_id = data.get("kiln_id")
        if not name:
            return err(400, "草案名称不能为空")
        if not isinstance(kiln_id, int):
            return err(400, "请选择窑炉")
        db = get_db()
        kiln = db.execute("SELECT id FROM kilns WHERE id = ?", (kiln_id,)).fetchone()
        if kiln is None:
            return err(400, "窑炉不存在")
        cur = db.execute(
            "INSERT INTO drafts(name, kiln_id) VALUES (?, ?)", (name, kiln_id)
        )
        db.commit()
        return jsonify(draft_json(db, cur.lastrowid)), 201

    @app.route("/api/drafts/<int:draft_id>", methods=["GET"])
    def get_draft(draft_id):
        db = get_db()
        d = draft_json(db, draft_id)
        if d is None:
            return err(404, "草案不存在")
        return jsonify(d)

    @app.route("/api/drafts/<int:draft_id>", methods=["DELETE"])
    def delete_draft(draft_id):
        db = get_db()
        row = db.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return err(404, "草案不存在")
        if row["status"] != "open":
            return err(409, "草案已确认，不可删除（确认记录不可变）")
        db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.route("/api/drafts/<int:draft_id>/items", methods=["PUT"])
    def put_item(draft_id):
        """把一批放入某窑位（该位已被占则替换）。一批占一位。"""
        data = json_body()
        batch_id = data.get("batch_id")
        pos_no = data.get("pos_no")
        if not isinstance(batch_id, int) or not isinstance(pos_no, int):
            return err(400, "需要 batch_id 与 pos_no（整数）")
        db = get_db()
        draft = db.execute(
            "SELECT id, kiln_id, status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            return err(404, "草案不存在")
        if draft["status"] != "open":
            return err(409, "草案已确认，不可修改")
        if not db.execute(
            "SELECT 1 FROM kiln_positions WHERE kiln_id = ? AND pos_no = ?",
            (draft["kiln_id"], pos_no),
        ).fetchone():
            return err(400, f"{pos_no} 号位不是该窑的固定窑位")
        if not db.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
            return err(404, "批次不存在")
        # 一批占一位：先移除该批在本草案中的旧占位；再移除目标位上的旧批次。
        db.execute(
            "DELETE FROM draft_items WHERE draft_id = ? AND batch_id = ?",
            (draft_id, batch_id),
        )
        db.execute(
            "DELETE FROM draft_items WHERE draft_id = ? AND pos_no = ?",
            (draft_id, pos_no),
        )
        db.execute(
            "INSERT INTO draft_items(draft_id, batch_id, pos_no) VALUES (?, ?, ?)",
            (draft_id, batch_id, pos_no),
        )
        touch_draft(db, draft_id)
        db.commit()
        return jsonify(draft_json(db, draft_id))

    @app.route("/api/drafts/<int:draft_id>/items/<int:batch_id>", methods=["DELETE"])
    def remove_item(draft_id, batch_id):
        db = get_db()
        draft = db.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if draft is None:
            return err(404, "草案不存在")
        if draft["status"] != "open":
            return err(409, "草案已确认，不可修改")
        db.execute(
            "DELETE FROM draft_items WHERE draft_id = ? AND batch_id = ?",
            (draft_id, batch_id),
        )
        touch_draft(db, draft_id)
        db.commit()
        return jsonify(draft_json(db, draft_id))

    @app.route("/api/drafts/<int:draft_id>/preview", methods=["GET"])
    def preview(draft_id):
        return build_preview(get_db(), draft_id)

    # ------------------------------------------------------------------
    # 确认（乐观版本 + 事务内重查）
    # ------------------------------------------------------------------

    @app.route("/api/drafts/<int:draft_id>/confirm", methods=["POST"])
    def confirm(draft_id):
        data = json_body()
        expect_draft_version = data.get("draft_version")
        client_items = data.get("items")
        if not isinstance(expect_draft_version, int) or not isinstance(client_items, list):
            return err(400, "确认须携带预览时的 draft_version 与 items 快照")
        # 预览快照中的每项：{batch_id, batch_version, pos_no}
        snapshot = {}
        for it in client_items:
            if (
                not isinstance(it, dict)
                or not isinstance(it.get("batch_id"), int)
                or not isinstance(it.get("batch_version"), int)
                or not isinstance(it.get("pos_no"), int)
            ):
                return err(400, "items 每项须含 batch_id、batch_version、pos_no")
            if it["batch_id"] in snapshot:
                return err(400, f"批次在快照中出现多次：batch_id={it['batch_id']}")
            snapshot[it["batch_id"]] = (it["batch_version"], it["pos_no"])

        db = get_db()
        try:
            # BEGIN IMMEDIATE：并发确认时在此排队，保证最终条件重查与写入原子
            db.execute("BEGIN IMMEDIATE")

            draft = db.execute(
                "SELECT id, kiln_id, name, version, status FROM drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                db.rollback()
                return err(404, "草案不存在")
            if draft["status"] != "open":
                db.rollback()
                load = db.execute(
                    "SELECT id FROM confirmed_loads WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                return err(
                    409,
                    f"该草案已确认（窑次 #{load['id'] if load else '?'}），不可重复确认",
                    stale=True,
                )
            if draft["version"] != expect_draft_version:
                db.rollback()
                return err(
                    409,
                    "草案自预览后已被他人改动（位置调整），请刷新并重新预览后再确认",
                    stale=True,
                )

            rows = db.execute(
                """SELECT di.batch_id, di.pos_no, b.code, b.version,
                          b.peak_min, b.peak_max, b.hold_min, b.hold_max
                     FROM draft_items di JOIN batches b ON b.id = di.batch_id
                    WHERE di.draft_id = ?""",
                (draft_id,),
            ).fetchall()
            if not rows:
                db.rollback()
                return err(409, "草案为空，无法确认", stale=True)

            # 快照比对：位置或工艺范围变了都算过期
            if {r["batch_id"] for r in rows} != set(snapshot):
                db.rollback()
                return err(
                    409, "草案批次组成已变化，请刷新并重新预览后再确认", stale=True
                )
            for r in rows:
                v, p = snapshot[r["batch_id"]]
                if r["version"] != v:
                    db.rollback()
                    return err(
                        409,
                        f"批次 {r['code']} 的工艺范围自预览后已被修改，请刷新并重新预览后再确认",
                        stale=True,
                        conflict_batch=r["code"],
                    )
                if r["pos_no"] != p:
                    db.rollback()
                    return err(
                        409,
                        f"批次 {r['code']} 的窑位自预览后已被调整（{p} → {r['pos_no']} 号位），请刷新并重新预览后再确认",
                        stale=True,
                        conflict_batch=r["code"],
                    )

            # 窑位不重复
            pos_seen = {}
            for r in rows:
                if r["pos_no"] in pos_seen:
                    db.rollback()
                    return err(
                        409,
                        f"窑位冲突：{r['pos_no']} 号位同时放了批次 "
                        f"{pos_seen[r['pos_no']]} 和 {r['code']}",
                        conflict_pos=r["pos_no"],
                        conflict_batches=[pos_seen[r["pos_no"]], r["code"]],
                    )
                pos_seen[r["pos_no"]] = r["code"]
                valid = db.execute(
                    "SELECT 1 FROM kiln_positions WHERE kiln_id = ? AND pos_no = ?",
                    (draft["kiln_id"], r["pos_no"]),
                ).fetchone()
                if not valid:
                    db.rollback()
                    return err(409, f"{r['pos_no']} 号位不是该窑的固定窑位")

            # 两项区间交集
            peak_lo = max(r["peak_min"] for r in rows)
            peak_hi = min(r["peak_max"] for r in rows)
            if peak_lo > peak_hi:
                blockers = [
                    r["code"]
                    for r in rows
                    if r["peak_max"] < peak_lo or r["peak_min"] > peak_hi
                ]
                db.rollback()
                return err(
                    409,
                    "峰温区间无交集，无法合窑，冲突批次：" + "、".join(blockers),
                    conflict_batches=blockers,
                )
            hold_lo = max(r["hold_min"] for r in rows)
            hold_hi = min(r["hold_max"] for r in rows)
            if hold_lo > hold_hi:
                blockers = [
                    r["code"]
                    for r in rows
                    if r["hold_max"] < hold_lo or r["hold_min"] > hold_hi
                ]
                db.rollback()
                return err(
                    409,
                    "保温分钟区间无交集，无法合窑，冲突批次：" + "、".join(blockers),
                    conflict_batches=blockers,
                )

            # 批次未被其他已确认窑次使用
            used = db.execute(
                """SELECT cli.batch_id, cli.batch_code, cl.id AS load_id, k.code AS kiln_code
                     FROM confirmed_load_items cli
                     JOIN confirmed_loads cl ON cl.id = cli.load_id
                     JOIN kilns k ON k.id = cl.kiln_id
                    WHERE cli.batch_id IN (%s)""" % ",".join("?" * len(rows)),
                [r["batch_id"] for r in rows],
            ).fetchall()
            if used:
                parts = [
                    f"{u['batch_code']} 已在窑次 #{u['load_id']}（{u['kiln_code']}）"
                    for u in used
                ]
                db.rollback()
                return err(
                    409,
                    "批次已被其他已确认窑次使用：" + "；".join(parts),
                    conflict_batches=[u["batch_code"] for u in used],
                )

            # —— 全部通过：一次写入不可变装载清单 ——
            cur = db.execute(
                """INSERT INTO confirmed_loads
                       (draft_id, kiln_id, name, peak_min, peak_max, hold_min, hold_max)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft_id,
                    draft["kiln_id"],
                    draft["name"],
                    peak_lo,
                    peak_hi,
                    hold_lo,
                    hold_hi,
                ),
            )
            load_id = cur.lastrowid
            for r in rows:
                db.execute(
                    """INSERT INTO confirmed_load_items
                           (load_id, batch_id, batch_code, pos_no,
                            peak_min, peak_max, hold_min, hold_max)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        load_id,
                        r["batch_id"],
                        r["code"],
                        r["pos_no"],
                        r["peak_min"],
                        r["peak_max"],
                        r["hold_min"],
                        r["hold_max"],
                    ),
                )
            db.execute(
                "UPDATE drafts SET status = 'confirmed', version = version + 1 WHERE id = ?",
                (draft_id,),
            )
            db.commit()
        except sqlite3.IntegrityError:
            # UNIQUE(batch_id) / UNIQUE(draft_id)：并发争用的落败方
            db.rollback()
            return err(
                409,
                "并发冲突：相关批次或草案已被另一窑次抢先确认，请刷新后查看",
                conflict=True,
            )

        return load_detail(db, load_id, created=True)

    # ------------------------------------------------------------------
    # 已确认清单（按确认时快照查询）
    # ------------------------------------------------------------------

    def load_detail(db, load_id, created=False):
        load = db.execute(
            """SELECT cl.*, k.code AS kiln_code
                 FROM confirmed_loads cl JOIN kilns k ON k.id = cl.kiln_id
                WHERE cl.id = ?""",
            (load_id,),
        ).fetchone()
        if load is None:
            return err(404, "窑次不存在")
        items = db.execute(
            """SELECT batch_id, batch_code, pos_no, peak_min, peak_max,
                      hold_min, hold_max
                 FROM confirmed_load_items WHERE load_id = ? ORDER BY pos_no""",
            (load_id,),
        ).fetchall()
        body = {**dict(load), "items": [dict(r) for r in items]}
        return jsonify(body), (201 if created else 200)

    @app.route("/api/loads", methods=["GET"])
    def list_loads():
        db = get_db()
        rows = db.execute(
            """SELECT cl.id, cl.name, k.code AS kiln_code,
                      cl.peak_min, cl.peak_max, cl.hold_min, cl.hold_max,
                      cl.confirmed_at, COUNT(cli.id) AS item_count
                 FROM confirmed_loads cl
                 JOIN kilns k ON k.id = cl.kiln_id
                 LEFT JOIN confirmed_load_items cli ON cli.load_id = cl.id
                GROUP BY cl.id ORDER BY cl.id"""
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/loads/<int:load_id>", methods=["GET"])
    def get_load(load_id):
        return load_detail(get_db(), load_id)

    # ------------------------------------------------------------------
    # 烧成时段安排（对已确认窑次；保存时事务内按最新数据重查）
    # ------------------------------------------------------------------

    SCHEDULE_SELECT = """
        SELECT fs.id, fs.load_id, cl.name AS load_name, fs.kiln_id, k.code AS kiln_code,
               fs.start_at, fs.end_at, fs.peak_temp, fs.hold_minutes,
               fs.created_at, fs.updated_at
          FROM firing_schedules fs
          JOIN confirmed_loads cl ON cl.id = fs.load_id
          JOIN kilns k ON k.id = fs.kiln_id
    """

    def parse_iso_tz(value, name):
        """解析带时区的 ISO 8601 时间并转为 UTC。返回 (datetime, None) 或 (None, 错误)。"""
        if not isinstance(value, str) or not value.strip():
            return None, f"{name}不能为空"
        s = value.strip()
        if s[-1:] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None, f"{name}不是合法的 ISO 8601 日期时间"
        if dt.tzinfo is None:
            return None, f"{name}必须带时区（如 2026-10-08T08:00:00+08:00）"
        return dt.astimezone(timezone.utc), None

    def fmt_utc(dt):
        """UTC 时间 -> 定长 ISO 字符串（字典序即时间序），如 2026-10-08T00:00:00.000000Z。"""
        return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def parse_schedule_fields(data):
        """解析时段与拟用工艺参数。返回 ((start, end, peak, hold), None) 或 (None, 错误)。"""
        start, e = parse_iso_tz(data.get("start_at"), "开始时间")
        if e:
            return None, e
        end, e = parse_iso_tz(data.get("end_at"), "结束时间")
        if e:
            return None, e
        if not start < end:
            return None, "时间须先后有序：开始时间必须早于结束时间"
        peak = data.get("peak_temp")
        hold = data.get("hold_minutes")
        if isinstance(peak, bool) or not isinstance(peak, numbers.Real):
            return None, "拟用峰温必须是数字"
        if isinstance(hold, bool) or not isinstance(hold, int):
            return None, "保温分钟必须是整数"
        if peak < 0 or hold < 0:
            return None, "拟用峰温与保温分钟不能为负数"
        return (start, end, peak, hold), None

    def params_range_error(load, peak, hold):
        """拟用参数必须落在该窑次确认时快照的共同区间内（不取批次后续修改的范围）。"""
        if not load["peak_min"] <= peak <= load["peak_max"]:
            return (
                f"拟用峰温 {peak}°C 不在窑次 #{load['id']} 确认时的共同峰温区间 "
                f"[{load['peak_min']}, {load['peak_max']}] 内（以确认快照为准，不取批次后续修改）"
            )
        if not load["hold_min"] <= hold <= load["hold_max"]:
            return (
                f"保温 {hold} 分钟不在窑次 #{load['id']} 确认时的共同保温区间 "
                f"[{load['hold_min']}, {load['hold_max']}] 内（以确认快照为准，不取批次后续修改）"
            )
        return None

    def find_overlaps(db, kiln_id, start, end, exclude_id=None):
        """同一台窑的重叠安排（首尾相接不算重叠）。须在事务内调用以读到最新数据。"""
        q = (
            "SELECT fs.id, fs.load_id, fs.start_at, fs.end_at, cl.name AS load_name"
            "  FROM firing_schedules fs"
            "  JOIN confirmed_loads cl ON cl.id = fs.load_id"
            " WHERE fs.kiln_id = ?"
        )
        args = [kiln_id]
        if exclude_id is not None:
            q += " AND fs.id != ?"
            args.append(exclude_id)
        overlaps = []
        for r in db.execute(q, args):
            s, _ = parse_iso_tz(r["start_at"], "开始时间")
            e, _ = parse_iso_tz(r["end_at"], "结束时间")
            if s < end and start < e:
                overlaps.append(
                    {
                        "schedule_id": r["id"],
                        "load_id": r["load_id"],
                        "load_name": r["load_name"],
                        "start_at": r["start_at"],
                        "end_at": r["end_at"],
                    }
                )
        return overlaps

    def overlap_error(overlaps):
        parts = [
            f"窑次 #{o['load_id']}（{o['load_name']}）{o['start_at']} ~ {o['end_at']}"
            for o in overlaps
        ]
        return err(
            409,
            "同一台窑的烧成时段重叠，占用窑次：" + "；".join(parts),
            conflicts=overlaps,
        )

    def schedule_detail(db, schedule_id, created=False):
        row = db.execute(
            SCHEDULE_SELECT + " WHERE fs.id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            return err(404, "烧成安排不存在")
        return jsonify(dict(row)), (201 if created else 200)

    @app.route("/api/schedules", methods=["GET"])
    def list_schedules():
        """全部安排：按窑和开始时间排序。"""
        db = get_db()
        rows = db.execute(
            SCHEDULE_SELECT + " ORDER BY fs.kiln_id, fs.start_at, fs.id"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/schedules", methods=["POST"])
    def create_schedule():
        data = json_body()
        load_id = data.get("load_id")
        if isinstance(load_id, bool) or not isinstance(load_id, int):
            return err(400, "需要 load_id（整数）：从已确认窑次清单中选择")
        parsed, e = parse_schedule_fields(data)
        if e:
            return err(400, e)
        start, end, peak, hold = parsed

        db = get_db()
        try:
            # BEGIN IMMEDIATE：并发安排在此排队，重查与写入原子完成
            db.execute("BEGIN IMMEDIATE")
            load = db.execute(
                "SELECT * FROM confirmed_loads WHERE id = ?", (load_id,)
            ).fetchone()
            if load is None:
                db.rollback()
                return err(404, "窑次不存在（仅已确认窑次可安排烧成）")
            dup = db.execute(
                "SELECT id FROM firing_schedules WHERE load_id = ?", (load_id,)
            ).fetchone()
            if dup:
                db.rollback()
                return err(
                    409,
                    f"窑次 #{load_id} 已安排过烧成时段（安排 #{dup['id']}），"
                    "同一窑次只能安排一次；如需调整请修改原安排",
                    conflict_schedule=dup["id"],
                )
            e = params_range_error(load, peak, hold)
            if e:
                db.rollback()
                return err(400, e)
            overlaps = find_overlaps(db, load["kiln_id"], start, end)
            if overlaps:
                db.rollback()
                return overlap_error(overlaps)
            cur = db.execute(
                """INSERT INTO firing_schedules
                       (load_id, kiln_id, start_at, end_at, peak_temp, hold_minutes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (load_id, load["kiln_id"], fmt_utc(start), fmt_utc(end), peak, hold),
            )
            db.commit()
        except sqlite3.IntegrityError:
            # UNIQUE(load_id)：并发争用同一窑次时的落败方
            db.rollback()
            return err(
                409,
                "并发冲突：该窑次已被另一请求抢先安排，请刷新后查看",
                conflict=True,
            )
        return schedule_detail(db, cur.lastrowid, created=True)

    @app.route("/api/schedules/<int:schedule_id>", methods=["PUT"])
    def update_schedule(schedule_id):
        """修改已有安排的时段与参数；任一检查失败则回滚，原安排保持不变。"""
        data = json_body()
        parsed, e = parse_schedule_fields(data)
        if e:
            return err(400, e)
        start, end, peak, hold = parsed

        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            sched = db.execute(
                "SELECT * FROM firing_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
            if sched is None:
                db.rollback()
                return err(404, "烧成安排不存在")
            load = db.execute(
                "SELECT * FROM confirmed_loads WHERE id = ?", (sched["load_id"],)
            ).fetchone()
            e = params_range_error(load, peak, hold)
            if e:
                db.rollback()
                return err(400, e)
            overlaps = find_overlaps(
                db, sched["kiln_id"], start, end, exclude_id=schedule_id
            )
            if overlaps:
                db.rollback()
                return overlap_error(overlaps)
            db.execute(
                """UPDATE firing_schedules
                      SET start_at = ?, end_at = ?, peak_temp = ?, hold_minutes = ?,
                          updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?""",
                (fmt_utc(start), fmt_utc(end), peak, hold, schedule_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return err(409, "保存失败：时间须先后有序，原安排保持不变", conflict=True)
        return schedule_detail(db, schedule_id)

    return app
