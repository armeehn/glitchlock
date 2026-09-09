import http.client, json, hashlib
c = http.client.HTTPSConnection("glitchlock.hq.ripostelabs.xyz", timeout=600)
def post(path, body, headers):
    c.request("POST", path, body=body, headers=headers); r = c.getresponse(); d = r.read()
    try: return r.status, json.loads(d)
    except Exception: return r.status, {"raw": d[:200].decode(errors="replace")}
raw = open("fixtures/bf.mpg","rb").read()
s, up = post("/api/upload", raw, {"Content-Type": "application/octet-stream", "X-Filename": "bf.mpg"}); print("upload", s, up.get("bytes"), up.get("sha256") == hashlib.sha256(raw).hexdigest())
J = lambda b: (json.dumps(b), {"Content-Type": "application/json"})
s, ins = post("/api/inspect", *J({"id": up["id"], "name": up["name"]})); print("inspect", s, {k: v for k, v in ins.items() if k != "id"} if s == 200 else ins)
s, kg = post("/api/keygen", *J({})); pub, sec = kg["public"], kg["secret"]
s, lk = post("/api/lock", *J({"id": up["id"], "name": up["name"], "key": {"type": "recipient", "recipients": [pub]}})); print("lock recipient", s, "slots", lk.get("slots"), "selftest", lk.get("selftest"), "repairs", lk.get("repairs"))
s, ok = post("/api/unlock", *J({"id": lk["id"], "name": "locked.mpg", "manifest": lk["manifest"], "key": {"type": "identity", "identity": sec}})); print("unlock recipient", s, ok.get("exact", ok))
s, kg2 = post("/api/keygen", *J({}))
s, bad = post("/api/unlock", *J({"id": lk["id"], "name": "locked.mpg", "manifest": lk["manifest"], "key": {"type": "identity", "identity": kg2["secret"]}})); print("unlock wrong identity", s, bad.get("error", bad))
print("RESULT", "PASS" if ok.get("exact") and s == 400 else "FAIL")
