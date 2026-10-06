"""烧成时段安排：端到端业务规则验证（临时 SQLite，结束后删除）。"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "schedule.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)


def confirm_load(name, kiln_id, batches):
    """登记草案 -> 放入批次 -> 预览 -> 确认，返回窑次 JSON。"""
    d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
    for pos, b in enumerate(batches, start=1):
        r = c.put(f"/api/drafts/{d['id']}/items",
                  json={"batch_id": b["id"], "pos_no": pos})
        assert r.status_code == 200, r.data
    p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
    assert p["ready"], p
    snap = {"draft_version": p["draft_version"],
            "items": [{"batch_id": i["batch_id"],
                       "batch_version": i["batch_version"],
                       "pos_no": i["pos_no"]} for i in p["items"]]}
    r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
    assert r.status_code == 201, r.data
    return r.get_json()


def add_batch(code, pmin, pmax, hmin, hmax):
    return c.post("/api/batches", json={
        "code": code, "peak_min": pmin, "peak_max": pmax,
        "hold_min": hmin, "hold_max": hmax,
    }).get_json()


def schedule(load_id, start, end, peak, hold):
    return c.put(f"/api/schedules/loads/{load_id}", json={
        "start_at": start, "end_at": end,
        "peak_temp": peak, "hold_minutes": hold,
    })


kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

# K1 两个窑次（批次互不相同）；K2 一个窑次
l1 = confirm_load("L1", k1, [add_batch("S1", 1200, 1260, 20, 40),
                             add_batch("S2", 1230, 1280, 30, 50)])
# 共同峰温 1230~1260，共同保温 30~40
check("窑次1共同区间快照",
      (l1["peak_min"], l1["peak_max"], l1["hold_min"], l1["hold_max"])
      == (1230.0, 1260.0, 30, 40), l1)
l2 = confirm_load("L2", k1, [add_batch("S3", 1200, 1260, 20, 40),
                             add_batch("S4", 1210, 1250, 25, 45)])
l3 = confirm_load("L3", k2, [add_batch("S5", 1180, 1240, 15, 35)])

# ---------- 1. 正常安排（带时区，参数落在快照共同区间） ----------
r = schedule(l1["id"], "2026-10-10T10:00:00+08:00", "2026-10-10T12:00:00+08:00",
             1240, 35)
check("安排成功", r.status_code == 201 and r.get_json()["created"] is True, r.data)
s = r.get_json()
check("UTC 归一化", (s["start_utc"], s["end_utc"])
      == ("2026-10-10T02:00:00Z", "2026-10-10T04:00:00Z"), s)
check("回显允许区间（快照）",
      (s["allowed_peak_min"], s["allowed_peak_max"],
       s["allowed_hold_min"], s["allowed_hold_max"])
      == (1230.0, 1260.0, 30, 40), s)

lst = c.get("/api/schedules").get_json()
check("安排列表可查", len(lst) == 1 and lst[0]["load_id"] == l1["id"]
      and lst[0]["kiln_code"] == "K1")
loads_list = c.get("/api/loads").get_json()
check("窑次列表带 schedule_id",
      next(x for x in loads_list if x["id"] == l1["id"])["schedule_id"] is not None)

# ---------- 2. 同一窑次再安排 = 修改原行，不产生第二条 ----------
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1255, 40)
check("同窑次重复提交为修改(200)", r.status_code == 200
      and r.get_json()["created"] is False, r.data)
lst = c.get("/api/schedules").get_json()
check("同一窑次只有一条安排", len([x for x in lst if x["load_id"] == l1["id"]]) == 1
      and len(lst) == 1)

# ---------- 3. 参数越界（超出确认时共同区间） ----------
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1261, 35)
check("峰温超共同区间被拒", r.status_code == 400 and "峰温" in r.get_json()["error"])
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1229, 35)
check("峰温低于共同区间被拒", r.status_code == 400)
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1240, 41)
check("保温超共同区间被拒", r.status_code == 400 and "保温" in r.get_json()["error"])
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1240.5, 29)
check("保温低于共同区间被拒", r.status_code == 400)

# ---------- 4. 后来修改批次范围，仍只认确认时快照区间 ----------
# 把 S1 改到很窄的新范围（版本+1）
s1 = next(x for x in c.get("/api/batches").get_json() if x["code"] == "S1")
r = c.put(f"/api/batches/{s1['id']}", json={
    "peak_min": 1250, "peak_max": 1252, "hold_min": 38, "hold_max": 39,
    "version": s1["version"]})
check("批次后续可改工艺", r.status_code == 200)
# 1245 / 35 已不在“后来批次范围”，但仍在确认快照 1230~1260 / 30~40 内 -> 允许
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1245, 35)
check("取快照区间而非后来批次范围（允许）", r.status_code == 200, r.data)
# 快照之外（1261）仍拒绝
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T09:30:00+08:00",
             1261, 35)
check("不能越过快照上界（即便批次后来改过）", r.status_code == 400)
# 已确认装载快照本身不变
detail = c.get(f"/api/loads/{l1['id']}").get_json()
snap_s1 = next(i for i in detail["items"] if i["batch_code"] == "S1")
check("安排与批次修改不影响装载快照",
      (snap_s1["peak_min"], snap_s1["peak_max"], snap_s1["hold_min"], snap_s1["hold_max"])
      == (1200.0, 1260.0, 20, 40), snap_s1)

# ---------- 5. 时间合法性 ----------
r = schedule(l1["id"], "2026-10-11T09:30:00+08:00", "2026-10-11T08:00:00+08:00",
             1240, 35)
check("开始晚于结束被拒", r.status_code == 400)
r = schedule(l1["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T08:00:00+08:00",
             1240, 35)
check("起止相等被拒", r.status_code == 400)
r = schedule(l1["id"], "2026-10-11T08:00", "2026-10-11T09:30", 1240, 35)
check("不带时区被拒", r.status_code == 400 and "时区" in r.get_json()["error"])
r = schedule(l1["id"], "not-a-time", "2026-10-11T09:30:00+08:00", 1240, 35)
check("非法时间字符串被拒", r.status_code == 400)

# ---------- 6. 同窑时间段重叠：指出占用窑次，失败原安排不变 ----------
# l1 现为 10-11 08:00~09:30 +08（00:00~01:30Z）
r = schedule(l2["id"], "2026-10-11T09:00:00+08:00", "2026-10-11T10:00:00+08:00",
             1230, 30)
check("同窑重叠被拒", r.status_code == 409 and r.get_json()["conflict"] is True)
check("冲突指出占用窑次",
      f"#{l1['id']}" in r.get_json()["error"], r.get_json()["error"])
check("败方未产生安排",
      all(x["load_id"] != l2["id"] for x in c.get("/api/schedules").get_json()))
check("胜方原安排不变",
      next(x for x in c.get("/api/schedules").get_json() if x["load_id"] == l1["id"])
      ["end_utc"] == "2026-10-11T01:30:00Z")

# 跨时区表达同一段重叠：+07:00 的 08:30~09:00 = 01:30~02:00Z 与 l1 不重叠（首尾之外）
r = schedule(l2["id"], "2026-10-11T08:30:00+07:00", "2026-10-11T09:00:00+07:00",
             1230, 30)
check("跨时区但不重叠允许", r.status_code == 201, r.data)
# 但 +07:00 的 08:00~08:30 = 01:00~01:30Z，与 l1(00:00~01:30Z) 重叠
r = schedule(l2["id"], "2026-10-11T08:00:00+07:00", "2026-10-11T08:30:00+07:00",
             1230, 30)
check("跨时区重叠同样被拒", r.status_code == 409
      and f"#{l1['id']}" in r.get_json()["error"])

# ---------- 7. 首尾相接允许（半开区间） ----------
# 步骤 6 已为 L2 建立安排（01:30~02:00Z），此处是修改到与 L1 首尾相接
r = schedule(l2["id"], "2026-10-11T09:30:00+08:00", "2026-10-11T11:00:00+08:00",
             1230, 30)
check("首尾相接允许（end==start）", r.status_code == 200, r.data)

# ---------- 8. 不同窑同一时段允许 ----------
r = schedule(l3["id"], "2026-10-11T08:00:00+08:00", "2026-10-11T12:00:00+08:00",
             1200, 30)
check("不同窑同时烧成允许", r.status_code == 201, r.data)

# ---------- 9. 修改已有安排到冲突时段：失败保持原值 ----------
before = next(x for x in c.get("/api/schedules").get_json()
              if x["load_id"] == l2["id"])
# l1: 00:00~01:30Z；把 l2 改到该区间内 -> 拒
r = schedule(l2["id"], "2026-10-11T08:30:00+08:00", "2026-10-11T09:00:00+08:00",
             1230, 30)
check("修改到冲突时段被拒", r.status_code == 409)
after = next(x for x in c.get("/api/schedules").get_json()
             if x["load_id"] == l2["id"])
check("失败后原安排保持不变",
      (after["start_utc"], after["end_utc"], after["peak_temp"], after["hold_minutes"])
      == (before["start_utc"], before["end_utc"],
          before["peak_temp"], before["hold_minutes"]))
# 改成无冲突的新时段 -> 成功
r = schedule(l2["id"], "2026-10-12T08:00:00Z", "2026-10-12T10:00:00Z",
             1250, 40)
check("修改为无冲突时段成功（Z 时区）", r.status_code == 200, r.data)

# ---------- 10. 只能给已确认窑次安排 ----------
r = schedule(99999, "2026-10-12T08:00:00Z", "2026-10-12T10:00:00Z", 1200, 30)
check("不存在窑次被拒", r.status_code == 404)

# ---------- 11. 按窑与时间排序展示 ----------
lst = c.get("/api/schedules").get_json()
check("列表按窑、开始时间排序",
      [(x["kiln_code"], x["load_id"]) for x in lst]
      == sorted((x["kiln_code"], x["load_id"]) for x in lst)
      and [x["kiln_id"] for x in lst]
      == sorted(x["kiln_id"] for x in lst))

# ---------- 12. 重启后安排保留，且装载快照仍不变 ----------
app2 = create_app()
c2 = app2.test_client()
lst2 = c2.get("/api/schedules").get_json()
check("重启后安排保留", len(lst2) == 3, lst2)
detail2 = c2.get(f"/api/loads/{l1['id']}").get_json()
check("重启后装载快照共同区间不变",
      (detail2["peak_min"], detail2["peak_max"],
       detail2["hold_min"], detail2["hold_max"]) == (1230.0, 1260.0, 30, 40))

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
