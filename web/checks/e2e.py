import http.client, json, sys
c = http.client.HTTPSConnection("glitchlock.hq.ripostelabs.xyz", timeout=300)
def post(path, body):
    c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    r = c.getresponse(); data = r.read()
    try: j = json.loads(data)
    except Exception: j = {"raw": data[:200].decode(errors="replace")}
    return r.status, j
s, sample = post("/api/sample", {}); print("sample", s, sample.get("bytes"))
s, prep = post("/api/prepare", {"id": sample["id"], "name": sample["name"], "codec": "mpeg2video"}); print("prepare", s, prep.get("name"), prep.get("bytes"))
key = {"type": "passphrase", "passphrase": "correct horse"}
s, lk = post("/api/lock", {"id": prep["id"], "name": prep["name"], "key": key}); print("lock", s, "slots", lk.get("slots"), "features", lk.get("features"), "selftest", lk.get("selftest"))
assert lk["locked"]["sha256"] != lk["carrier"]["sha256"], "lock changed nothing"
s, bad = post("/api/unlock", {"id": lk["id"], "name": "locked.mpg", "manifest": lk["manifest"], "key": {"type": "passphrase", "passphrase": "wrong"}}); print("unlock wrong key", s, bad.get("error", bad))
s, ok = post("/api/unlock", {"id": lk["id"], "name": "locked.mpg", "manifest": json.dumps(lk["manifest"]), "key": key}); print("unlock", s, "exact", ok.get("exact"))
s, ok2 = post("/api/unlock", {"id": lk["id"], "name": "locked.mpg", "manifest": lk["manifest"], "key": key}); print("unlock (dict manifest)", s, "exact", ok2.get("exact"))
print("RESULT", "PASS" if ok.get("exact") and ok2.get("exact") and bad.get("exact") is None else "FAIL")
