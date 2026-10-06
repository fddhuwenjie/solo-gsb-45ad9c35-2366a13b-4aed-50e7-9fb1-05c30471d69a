"""并发争用：两台已确认窑次在同一台窑争用同一烧成时段，只能成功一个。"""
import os
import tempfile
from threading import Barrier, Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "concurrent_sched.db")

from app import create_app

app = create_app()
c = app.test_client()
kilns = c.get("/api/kilns").get_json()
k1 = kilns[0]["id"]
k2 = kilns[1]["id"]


def confirm_load(name, kiln_id, code):
    b = c.post("/api/batches", json={
        "code": code, "peak_min": 1200, "peak_max": 1260,
        "hold_min": 20, "hold_max": 40}).get_json()
    d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
    c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": b["id"], "pos_no": 1})
    p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
    assert p["ready"], p
    snap = {"draft_version": p["draft_version"],
            "items": [{"batch_id": i["batch_id"],
                       "batch_version": i["batch_version"],
                       "pos_no": i["pos_no"]} for i in p["items"]]}
    r = c.post(f"/api/drafts/{d['id']}/confirm", json=snap)
    assert r.status_code == 201, r.data
    return r.get_json()["id"]


# 同窑 K1 两个窑次争同一时段；K2 窑次同一时段（不同窑）应照样成功
la = confirm_load("LA", k1, "CA")
lb = confirm_load("LB", k1, "CB")
lc = confirm_load("LC", k2, "CC")

payload = {
    "start_at": "2026-11-01T08:00:00+08:00",
    "end_at": "2026-11-01T10:00:00+08:00",
    "peak_temp": 1230,
    "hold_minutes": 30,
}

results = {}
barrier = Barrier(3)


def save(key, load_id):
    client = app.test_client()
    barrier.wait()
    r = client.put(f"/api/schedules/loads/{load_id}", json=payload)
    results[key] = (r.status_code, r.get_json())


t1 = Thread(target=save, args=("LA", la))
t2 = Thread(target=save, args=("LB", lb))
t3 = Thread(target=save, args=("LC", lc))
t1.start(); t2.start(); t3.start()
t1.join(); t2.join(); t3.join()

for k in ("LA", "LB", "LC"):
    print(k, results[k][0], results[k][1].get("error", f"saved #{results[k][1].get('load_id')}"))

# K1 上恰好一个成功，另一个 409 并指出占用窑次
k1_codes = sorted(results[k][0] for k in ("LA", "LB"))
assert k1_codes == [201, 409], k1_codes
loser = "LA" if results["LA"][0] == 409 else "LB"
winner = "LB" if loser == "LA" else "LA"
err = results[loser][1].get("error") or ""
assert "占用" in err, err
assert f"#{results[winner][1]['load_id']}" in err, err
# 不同窑同时段成功
assert results["LC"][0] == 201, results["LC"]

# 数据库中 K1 只有一条该时段安排，败方没有半成品
scheds = c.get("/api/schedules").get_json()
assert len(scheds) == 2, scheds
k1_scheds = [s for s in scheds if s["kiln_code"] == "K1"]
assert len(k1_scheds) == 1
assert k1_scheds[0]["load_id"] == results[winner][1]["load_id"]

# 败方随后可在不冲突时段安排（失败后状态干净）
loser_id = {"LA": la, "LB": lb}[loser]
r = c.put(f"/api/schedules/loads/{loser_id}",
          json={**payload, "start_at": "2026-11-01T10:00:00+08:00",
                "end_at": "2026-11-01T12:00:00+08:00"})
assert r.status_code == 201, (r.status_code, r.get_json())

print("\n✅ 并发争用：同窑同时段恰好一个成功，另一个 409 指出占用窑次且无半成品；"
      "不同窑同时烧成互不影响")
