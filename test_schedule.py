"""烧成时段安排：业务规则与并发争用验证（临时 SQLite，结束后删除）。"""
import os
import tempfile
from threading import Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "schedule.db")

from app import create_app

app = create_app()
c = app.test_client()

passed = []
def check(name, cond, detail=""):
    assert cond, f"FAIL: {name} {detail}"
    passed.append(name)

# ---------- 准备：批次与已确认窑次 ----------
def add_batch(code, pmin, pmax, hmin, hmax):
    r = c.post("/api/batches", json={
        "code": code, "peak_min": pmin, "peak_max": pmax,
        "hold_min": hmin, "hold_max": hmax,
    })
    assert r.status_code == 201, r.data
    return r.get_json()

def confirm_load(name, kiln_id, batch_pos):
    d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
    for b, pos in batch_pos:
        c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": b["id"], "pos_no": pos})
    p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
    assert p["ready"], p
    snap = {"draft_version": p["draft_version"],
            "items": [{"batch_id": i["batch_id"], "batch_version": i["batch_version"],
                       "pos_no": i["pos_no"]} for i in p["items"]]}
    r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
    assert r.status_code == 201, r.data
    return r.get_json()

kilns = c.get("/api/kilns").get_json()
k1, k2 = kilns[0]["id"], kilns[1]["id"]

ba = add_batch("BA", 1200, 1250, 30, 45)
bb = add_batch("BB", 1220, 1260, 30, 40)
load1 = confirm_load("L1", k1, [(ba, 1), (bb, 2)])     # 共同区间：峰 1220~1250，保温 30~40
bc = add_batch("BC", 1210, 1240, 20, 50)
bd = add_batch("BD", 1230, 1270, 25, 35)
load2 = confirm_load("L2", k1, [(bc, 1), (bd, 2)])     # 共同区间：峰 1230~1240，保温 25~35
be = add_batch("BE", 1200, 1300, 10, 60)
load3 = confirm_load("L3", k2, [(be, 1)])              # K2
load1_snapshot = c.get(f"/api/loads/{load1['id']}").get_json()

# ---------- 1. 时间合法性 ----------
def post_sched(load_id, start, end, peak, hold):
    return c.post("/api/schedules", json={
        "load_id": load_id, "start_at": start, "end_at": end,
        "peak_temp": peak, "hold_minutes": hold,
    })

r = post_sched(load1["id"], "2026-10-08T08:00:00", "2026-10-08T12:00:00+08:00", 1240, 35)
check("开始时间缺时区被拒", r.status_code == 400 and "时区" in r.get_json()["error"])
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00", 1240, 35)
check("结束时间缺时区被拒", r.status_code == 400 and "时区" in r.get_json()["error"])
r = post_sched(load1["id"], "not-a-time", "2026-10-08T12:00:00+08:00", 1240, 35)
check("非法时间被拒", r.status_code == 400)
r = post_sched(load1["id"], "2026-10-08T12:00:00+08:00", "2026-10-08T08:00:00+08:00", 1240, 35)
check("结束早于开始被拒（时间须先后有序）", r.status_code == 400)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T08:00:00+08:00", 1240, 35)
check("开始等于结束被拒", r.status_code == 400)
r = post_sched(99999, "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1240, 35)
check("窑次不存在", r.status_code == 404)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1240, 30.5)
check("保温分钟非整数被拒", r.status_code == 400)

# ---------- 2. 工艺参数必须落在确认时快照的共同区间 ----------
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1251, 35)
check("峰温高于快照上限被拒", r.status_code == 400 and "1220" in r.get_json()["error"])
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1219, 35)
check("峰温低于快照下限被拒", r.status_code == 400)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1240, 41)
check("保温高于快照上限被拒", r.status_code == 400)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1240, 29)
check("保温低于快照下限被拒", r.status_code == 400)

# 确认后批次工艺被改：校验仍以确认快照为准，不取后来修改的范围
ba_now = next(x for x in c.get("/api/batches").get_json() if x["code"] == "BA")
r = c.put(f"/api/batches/{ba_now['id']}", json={"peak_min": 1000, "peak_max": 1010,
                                                "hold_min": 5, "hold_max": 9,
                                                "version": ba_now["version"]})
check("确认后批次工艺仍可修改", r.status_code == 200)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1005, 35)
check("落在批次新范围但超出快照被拒", r.status_code == 400)
r = post_sched(load1["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1240, 35)
check("落在快照内即可安排（不取批次新范围）", r.status_code == 201, r.data)
sched1 = r.get_json()
check("时间统一归一为 UTC",
      sched1["start_at"] == "2026-10-08T00:00:00.000000Z"
      and sched1["end_at"] == "2026-10-08T04:00:00.000000Z", sched1)
check("安排记录窑与窑次", sched1["kiln_id"] == k1 and sched1["load_id"] == load1["id"])

# ---------- 3. 同一窑次只能安排一次 ----------
r = post_sched(load1["id"], "2026-10-09T08:00:00+08:00", "2026-10-09T12:00:00+08:00", 1240, 35)
check("同一窑次重复安排被拒", r.status_code == 409 and "只能安排一次" in r.get_json()["error"])

# ---------- 4. 同一台窑时间段不得重叠（跨时区表达也算） ----------
# load1 占用 00:00Z~04:00Z（即 +08:00 的 08:00~12:00）
r = post_sched(load2["id"], "2026-10-08T02:00:00Z", "2026-10-08T06:00:00Z", 1235, 30)
check("同窑重叠被拒", r.status_code == 409)
body = r.get_json()
check("冲突指出占用窑次",
      body["conflicts"][0]["load_id"] == load1["id"]
      and f"#{load1['id']}" in body["error"], body)

# ---------- 5. 首尾相接允许（用另一时区表达同一时刻） ----------
# +02:00 的 06:00 即 04:00Z，恰好接上 load1 的结束时刻
r = post_sched(load2["id"], "2026-10-08T06:00:00+02:00", "2026-10-08T08:00:00+02:00", 1235, 30)
check("首尾相接允许", r.status_code == 201, r.data)
sched2 = r.get_json()
check("相接安排的起点等于前一段终点",
      sched2["start_at"] == sched1["end_at"], sched2)

# ---------- 6. 不同窑同时烧成允许 ----------
r = post_sched(load3["id"], "2026-10-08T08:00:00+08:00", "2026-10-08T12:00:00+08:00", 1250, 40)
check("不同窑同时烧成允许", r.status_code == 201, r.data)
sched3 = r.get_json()

# ---------- 7. 修改已有安排 ----------
r = c.put(f"/api/schedules/{sched2['id']}", json={
    "start_at": "2026-10-08T10:00:00+02:00", "end_at": "2026-10-08T12:00:00+02:00",
    "peak_temp": 1235, "hold_minutes": 30})   # 即 08:00Z~10:00Z
check("修改时段与参数成功", r.status_code == 200, r.data)
check("修改后时间为新值", r.get_json()["start_at"] == "2026-10-08T08:00:00.000000Z")

r = c.put(f"/api/schedules/{sched2['id']}", json={
    "start_at": "2026-10-08T10:00:00+02:00", "end_at": "2026-10-08T12:00:00+02:00",
    "peak_temp": 1300, "hold_minutes": 30})
check("修改参数超出快照区间被拒", r.status_code == 400)

r = c.put(f"/api/schedules/{sched2['id']}", json={
    "start_at": "2026-10-08T01:00:00Z", "end_at": "2026-10-08T05:00:00Z",
    "peak_temp": 1235, "hold_minutes": 30})
check("修改为重叠时段被拒并指出占用窑次",
      r.status_code == 409 and r.get_json()["conflicts"][0]["load_id"] == load1["id"])

cur = next(s for s in c.get("/api/schedules").get_json() if s["id"] == sched2["id"])
check("失败修改后原安排保持不变",
      cur["start_at"] == "2026-10-08T08:00:00.000000Z"
      and cur["end_at"] == "2026-10-08T10:00:00.000000Z"
      and cur["peak_temp"] == 1235 and cur["hold_minutes"] == 30, cur)

r = c.put(f"/api/schedules/{sched2['id']}", json={
    "start_at": "2026-10-08T08:00:00Z", "end_at": "2026-10-08T10:00:00Z",
    "peak_temp": 1240, "hold_minutes": 35})
check("原样再保存不与自身重叠", r.status_code == 200, r.data)

# ---------- 8. 列表按窑和时间展示 ----------
schedules = c.get("/api/schedules").get_json()
check("安排按窑和开始时间排序",
      [(s["kiln_id"], s["load_id"]) for s in schedules]
      == [(k1, load1["id"]), (k1, load2["id"]), (k2, load3["id"])], schedules)

# ---------- 9. 持久保存且不改动已确认装载快照 ----------
app2 = create_app()
c2 = app2.test_client()
check("重启后安排保留", len(c2.get("/api/schedules").get_json()) == 3)
load1_after = c2.get(f"/api/loads/{load1['id']}").get_json()
check("已确认装载快照未被安排改动",
      load1_after["items"] == load1_snapshot["items"]
      and (load1_after["peak_min"], load1_after["peak_max"],
           load1_after["hold_min"], load1_after["hold_max"])
      == (load1_snapshot["peak_min"], load1_snapshot["peak_max"],
          load1_snapshot["hold_min"], load1_snapshot["hold_max"]))

# ---------- 10. 并发争用：同窑同一时段只能成功一个 ----------
bf = add_batch("BF", 1180, 1290, 15, 55)
bg = add_batch("BG", 1180, 1290, 15, 55)
load4 = confirm_load("L4", k1, [(bf, 1)])
load5 = confirm_load("L5", k1, [(bg, 1)])

results = {}
barrier = []
def race(key, load_id):
    client = app.test_client()
    barrier.append(1)
    while len(barrier) < 2:
        pass
    r = client.post("/api/schedules", json={
        "load_id": load_id,
        "start_at": "2026-10-09T08:00:00+08:00", "end_at": "2026-10-09T12:00:00+08:00",
        "peak_temp": 1250, "hold_minutes": 40})
    results[key] = (r.status_code, r.get_json())

t1 = Thread(target=race, args=("L4", load4["id"]))
t2 = Thread(target=race, args=("L5", load5["id"]))
t1.start(); t2.start()
t1.join(); t2.join()

codes = sorted(results[k][0] for k in results)
print("L4:", results["L4"][0], results["L4"][1].get("error", "已安排"))
print("L5:", results["L5"][0], results["L5"][1].get("error", "已安排"))
check("同窑同时段并发只有一个成功", codes == [201, 409], results)
loser = load5 if results["L5"][0] == 409 else load4
check("败方错误指出占用窑次", "窑次 #" in (results["L5" if results["L5"][0] == 409 else "L4"][1].get("error") or ""))
slot = [s for s in c.get("/api/schedules").get_json()
        if s["start_at"] == "2026-10-09T00:00:00.000000Z"]
check("争用时段最终只有一条安排", len(slot) == 1, slot)
r = post_sched(loser["id"], "2026-10-10T08:00:00+08:00", "2026-10-10T12:00:00+08:00", 1250, 40)
check("败方改其他时段可安排", r.status_code == 201, r.data)

print(f"\n✅ 全部 {len(passed)} 项检查通过")
for n in passed:
    print("  -", n)
