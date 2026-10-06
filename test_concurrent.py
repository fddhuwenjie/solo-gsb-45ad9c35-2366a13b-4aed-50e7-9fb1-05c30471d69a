"""并发争用：两个草案同时确认含同一批次，只有一个成功。"""
import os
import tempfile
from threading import Thread

_tmp = tempfile.mkdtemp()
os.environ["KILN_DB_PATH"] = os.path.join(_tmp, "concurrent.db")

from app import create_app

app = create_app()


def setup():
    c = app.test_client()
    kilns = c.get("/api/kilns").get_json()
    shared = c.post("/api/batches", json={
        "code": "SHARED", "peak_min": 1200, "peak_max": 1260,
        "hold_min": 20, "hold_max": 40}).get_json()
    other_a = c.post("/api/batches", json={
        "code": "A1", "peak_min": 1210, "peak_max": 1250,
        "hold_min": 25, "hold_max": 35}).get_json()
    other_b = c.post("/api/batches", json={
        "code": "B7", "peak_min": 1210, "peak_max": 1250,
        "hold_min": 25, "hold_max": 35}).get_json()

    def make_draft(name, kiln_id, second):
        d = c.post("/api/drafts", json={"name": name, "kiln_id": kiln_id}).get_json()
        c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": shared["id"], "pos_no": 1})
        c.put(f"/api/drafts/{d['id']}/items", json={"batch_id": second["id"], "pos_no": 2})
        p = c.get(f"/api/drafts/{d['id']}/preview").get_json()
        assert p["ready"], p
        return d["id"], {"draft_version": p["draft_version"],
                         "items": [{"batch_id": i["batch_id"],
                                    "batch_version": i["batch_version"],
                                    "pos_no": i["pos_no"]} for i in p["items"]]}

    da, sa = make_draft("DA", kilns[0]["id"], other_a)
    db, sb = make_draft("DB", kilns[1]["id"], other_b)
    return da, sa, db, sb


da, sa, db, sb = setup()
results = {}
barrier = []

def confirm(key, draft_id, payload):
    client = app.test_client()
    barrier.append(1)              # 尽量让两线程同时进入
    while len(barrier) < 2:
        pass
    r = client.post(f"/api/drafts/{draft_id}/confirm", json=payload)
    results[key] = (r.status_code, r.get_json())

t1 = Thread(target=confirm, args=("DA", da, sa))
t2 = Thread(target=confirm, args=("DB", db, sb))
t1.start(); t2.start()
t1.join(); t2.join()

codes = sorted(results[k][0] for k in results)
print("DA:", results["DA"][0], results["DA"][1].get("error", f"load #{results['DA'][1].get('id')}"))
print("DB:", results["DB"][0], results["DB"][1].get("error", f"load #{results['DB'][1].get('id')}"))
assert codes == [201, 409], codes
assert 409 and any(
    "批次" in (results[k][1].get("error") or "") for k in results if results[k][0] == 409
)

# 最终只有一个窑次，SHARED 只出现一次
c = app.test_client()
loads = c.get("/api/loads").get_json()
assert len(loads) == 1, loads
detail = c.get(f"/api/loads/{loads[0]['id']}").get_json()
assert sum(1 for i in detail["items"] if i["batch_code"] == "SHARED") == 1
# 败方草案仍是 open，未产生任何半成品
loser = db if results["DB"][0] == 409 else da
d = c.get(f"/api/drafts/{loser}").get_json()
assert d["status"] == "open" and len(d["items"]) == 2
print("\n✅ 并发争用：恰好一个确认成功，另一个 409 并指出批次冲突；无部分占用")
