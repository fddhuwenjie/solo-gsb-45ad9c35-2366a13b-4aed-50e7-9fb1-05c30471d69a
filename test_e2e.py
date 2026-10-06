"""端到端业务规则验证（临时 SQLite，结束后删除）。"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "test.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)

# ---------- 1. 窑位种子 ----------
kilns = c.get("/api/kilns").get_json()
check("预置窑炉与固定窑位", len(kilns) == 2 and len(kilns[0]["positions"]) == 8)

# ---------- 2. 登记批次 ----------
def add_batch(code, pmin, pmax, hmin, hmax):
    r = c.post("/api/batches", json={
        "code": code, "peak_min": pmin, "peak_max": pmax,
        "hold_min": hmin, "hold_max": hmax,
    })
    check(f"登记批次 {code}", r.status_code == 201, r.data)
    return r.get_json()

b1 = add_batch("B1", 1200, 1250, 30, 45)
b2 = add_batch("B2", 1220, 1280, 20, 40)
b3 = add_batch("B3", 1300, 1320, 10, 15)   # 峰温与前两者无交集
b4 = add_batch("B4", 1210, 1240, 50, 60)   # 保温与 B1/B2 无交集
b5 = add_batch("B5", 1230, 1260, 25, 35)

r = c.post("/api/batches", json={"code": "B1", "peak_min": 1, "peak_max": 2,
                                 "hold_min": 1, "hold_max": 2})
check("批次编号重复被拒", r.status_code == 409)
r = c.post("/api/batches", json={"code": "BAD", "peak_min": 10, "peak_max": 5,
                                 "hold_min": 1, "hold_max": 2})
check("非法区间被拒", r.status_code == 400)

# ---------- 3. 草案与占位 ----------
k1 = kilns[0]["id"]
k2 = kilns[1]["id"]
r = c.post("/api/drafts", json={"name": "D1", "kiln_id": k1})
d1 = r.get_json()
check("创建草案", r.status_code == 201 and d1["version"] == 1)

r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b1["id"], "pos_no": 1})
check("放入 B1@1", r.status_code == 200 and r.get_json()["version"] == 2)
r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b2["id"], "pos_no": 2})
check("放入 B2@2", r.status_code == 200)
# B1 移到 3 号位：一批占一位
r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b1["id"], "pos_no": 3})
d = c.get(f"/api/drafts/{d1['id']}").get_json()
check("一批占一位（移动后旧位置释放）",
      {i["batch_code"]: i["pos_no"] for i in d["items"]} == {"B1": 3, "B2": 2})
# B5 放到 3 号位：替换 B1
r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b5["id"], "pos_no": 3})
d = c.get(f"/api/drafts/{d1['id']}").get_json()
check("窑位放置新批次替换原批次",
      {i["pos_no"]: i["batch_code"] for i in d["items"]} == {2: "B2", 3: "B5"})
# 把 B1 放回去
c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b1["id"], "pos_no": 1})

# 非法窑位
r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b1["id"], "pos_no": 99})
check("非固定窑位被拒", r.status_code == 400)

# ---------- 4. 预览：有交集 ----------
p = c.get(f"/api/drafts/{d1['id']}/preview").get_json()
check("预览共同峰温交集", p["common_peak"] == {"min": 1230.0, "max": 1250.0}, p["common_peak"])
check("预览共同保温交集", p["common_hold"] == {"min": 30, "max": 35}, p["common_hold"])
check("预览就绪", p["ready"] is True and p["conflicts"] == [])
check("占位图数据", p["occupied"] == {"1": "B1", "2": "B2", "3": "B5"} and len(p["positions"]) == 8)

# ---------- 5. 峰温无交集 ----------
r = c.post("/api/drafts", json={"name": "D-bad-peak", "kiln_id": k2})
dbp = r.get_json()
c.put(f"/api/drafts/{dbp['id']}/items", json={"batch_id": b1["id"], "pos_no": 1})
c.put(f"/api/drafts/{dbp['id']}/items", json={"batch_id": b3["id"], "pos_no": 2})
p = c.get(f"/api/drafts/{dbp['id']}/preview").get_json()
check("峰温无交集预览不可确认", p["ready"] is False and p["common_peak"] is None)
check("指出峰温冲突批次", any("B3" in w for w in p["warnings"]))
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
                  for i in p["items"]]}
r = c.post(f"/api/drafts/{dbp['id']}/confirm", json=snap)
check("峰温无交集确认拒绝并指出批次", r.status_code == 409 and "B3" in r.get_json()["error"])

# ---------- 6. 保温无交集 ----------
r = c.post("/api/drafts", json={"name": "D-bad-hold", "kiln_id": k2})
dbh = r.get_json()
c.put(f"/api/drafts/{dbh['id']}/items", json={"batch_id": b1["id"], "pos_no": 1})
c.put(f"/api/drafts/{dbh['id']}/items", json={"batch_id": b4["id"], "pos_no": 2})
p = c.get(f"/api/drafts/{dbh['id']}/preview").get_json()
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
                  for i in p["items"]]}
r = c.post(f"/api/drafts/{dbh['id']}/confirm", json=snap)
check("保温无交集确认拒绝并指出批次", r.status_code == 409 and "B4" in r.get_json()["error"])

# ---------- 7. 成功确认 D1 ----------
p = c.get(f"/api/drafts/{d1['id']}/preview").get_json()
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
                  for i in p["items"]]}
r = c.post(f"/api/drafts/{d1['id']}/confirm", json=snap)
check("确认成功", r.status_code == 201, r.data)
load = r.get_json()
check("清单记录共同区间", (load["peak_min"], load["peak_max"], load["hold_min"], load["hold_max"])
      == (1230.0, 1250.0, 30, 35))
check("清单明细数=3", len(load["items"]) == 3)
check("明细为批次快照", all(i["batch_code"] in ("B1", "B2", "B5") for i in load["items"]))

# 草案已确认：不可改、不可重复确认、不可删
r = c.put(f"/api/drafts/{d1['id']}/items", json={"batch_id": b4["id"], "pos_no": 4})
check("已确认草案不可改", r.status_code == 409)
r = c.post(f"/api/drafts/{d1['id']}/confirm", json=snap)
check("不可重复确认", r.status_code == 409)
r = c.delete(f"/api/drafts/{d1['id']}")
check("已确认草案不可删", r.status_code == 409)

# 失败后不占用部分批次：D-bad-* 的 B1/B3/B4 未被锁定，但 B1/B2/B5 已锁
r = c.delete(f"/api/batches/{b1['id']}")
check("已用批次不可删除", r.status_code == 409)
# 先把 B3 从失败草案移出，再删除：验证失败确认未占用任何批次
c.delete(f"/api/drafts/{dbp['id']}/items/{b3['id']}")
r = c.delete(f"/api/batches/{b3['id']}")
check("未使用批次可删除（失败确认未占用）", r.status_code == 200)

# ---------- 8. 批次已被其他窑次使用 ----------
r = c.post("/api/drafts", json={"name": "D2", "kiln_id": k2})
d2 = r.get_json()
c.put(f"/api/drafts/{d2['id']}/items", json={"batch_id": b1["id"], "pos_no": 1})  # B1 已在窑次1
c.put(f"/api/drafts/{d2['id']}/items", json={"batch_id": b4["id"], "pos_no": 2})
p = c.get(f"/api/drafts/{d2['id']}/preview").get_json()
check("预览提示批次被其他窑次使用",
      any(cf["type"] == "batch_used" and cf["batch_code"] == "B1" for cf in p["conflicts"]))
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
                  for i in p["items"]]}
r = c.post(f"/api/drafts/{d2['id']}/confirm", json=snap)
check("占用批次确认拒绝并指出窑次", r.status_code == 409 and "B1" in r.get_json()["error"])
# 移除 B1 后可确认，验证失败时没有写入任何 confirmed 记录（B4 没被部分占用）
c.delete(f"/api/drafts/{d2['id']}/items/{b1['id']}")
p = c.get(f"/api/drafts/{d2['id']}/preview").get_json()
snap = {"draft_version": p["draft_version"],
        "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
                  for i in p["items"]]}
r = c.post(f"/api/drafts/{d2['id']}/confirm", json=snap)
check("移除占用批次后确认成功（B4 此前未被部分占用）", r.status_code == 201, r.data)

# ---------- 9. 过期预览：批次工艺被改 ----------
r = c.post("/api/drafts", json={"name": "D3", "kiln_id": k2})
d3 = r.get_json()
b6 = add_batch("B6", 1230, 1260, 25, 40)
c.put(f"/api/drafts/{d3['id']}/items", json={"batch_id": b6["id"], "pos_no": 1})
p = c.get(f"/api/drafts/{d3['id']}/preview").get_json()
old_items = [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
             for i in p["items"]]
# 别人改了 B6 的工艺范围（版本+1）
r = c.put(f"/api/batches/{b6['id']}", json={"peak_min": 1240, "peak_max": 1270,
                                            "hold_min": 25, "hold_max": 40, "version": b6["version"]})
check("改工艺成功版本递增", r.status_code == 200 and r.get_json()["version"] == b6["version"] + 1)
r = c.post(f"/api/drafts/{d3['id']}/confirm", json={"draft_version": p["draft_version"], "items": old_items})
check("批次版本过期拒绝确认并要求刷新", r.status_code == 409 and r.get_json()["stale"] is True
      and "B6" in r.get_json()["error"])

# 旧版本号改批次也拒绝（乐观锁）
r = c.put(f"/api/batches/{b6['id']}", json={"peak_min": 1, "peak_max": 2,
                                            "hold_min": 1, "hold_max": 2, "version": b6["version"]})
check("旧版本修改批次被拒", r.status_code == 409 and r.get_json()["stale"])

# ---------- 10. 过期预览：位置被改 ----------
p = c.get(f"/api/drafts/{d3['id']}/preview").get_json()
old_items = [{"batch_id": i["batch_id"], "batch_version": i["batch_version"], "pos_no": i["pos_no"]}
             for i in p["items"]]
c.put(f"/api/drafts/{d3['id']}/items", json={"batch_id": b6["id"], "pos_no": 5})  # 位置 1->5
r = c.post(f"/api/drafts/{d3['id']}/confirm", json={"draft_version": p["draft_version"], "items": old_items})
check("位置变动后旧预览拒绝确认", r.status_code == 409 and r.get_json()["stale"])

# 空草案
r = c.post("/api/drafts", json={"name": "empty", "kiln_id": k2})
de = r.get_json()
r = c.post(f"/api/drafts/{de['id']}/confirm", json={"draft_version": 1, "items": []})
check("空草案不可确认", r.status_code == 409)

# ---------- 11. 重启后快照保留 ----------
app2 = create_app()
c2 = app2.test_client()
loads = c2.get("/api/loads").get_json()
check("重启后已确认窑次保留", len(loads) == 2)
detail = c2.get(f"/api/loads/{load['id']}").get_json()
check("重启后清单快照内容不变",
      [(i["batch_code"], i["pos_no"]) for i in detail["items"]]
      == sorted([(i["batch_code"], i["pos_no"]) for i in load["items"]]))
# 修改批次工艺不影响快照
cur_b1 = c2.get("/api/batches").get_json()
b1_now = next(x for x in cur_b1 if x["code"] == "B1")
r = c2.put(f"/api/batches/{b1_now['id']}", json={"peak_min": 1000, "peak_max": 1010,
                                                 "hold_min": 5, "hold_max": 9,
                                                 "version": b1_now["version"]})
check("已用批次工艺仍可编辑", r.status_code == 200)
detail2 = c2.get(f"/api/loads/{load['id']}").get_json()
snap_b1 = next(i for i in detail2["items"] if i["batch_code"] == "B1")
check("快照不随后续编辑改变", (snap_b1["peak_min"], snap_b1["peak_max"]) == (1200.0, 1250.0))
check("共同区间快照不变", (detail2["peak_min"], detail2["peak_max"]) == (1230.0, 1250.0))

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
