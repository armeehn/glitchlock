import http.client, json, os
H = "glitchlock.hq.ripostelabs.xyz"
def conn(): return http.client.HTTPSConnection(H, timeout=900)
c = conn()
def post(path, body, hdrs):
    c.request("POST", path, body=body, headers=hdrs); r = c.getresponse(); d = r.read()
    try: return r.status, json.loads(d)
    except Exception: return r.status, {"raw": d[:160].decode(errors="replace")}
J = {"Content-Type": "application/json"}
def upload(fn, name):
    return post("/api/upload", open(fn, "rb").read(), {"Content-Type": "application/octet-stream", "X-Filename": name})

for label, fn in [("pure random bytes", "fixtures/random.bin"), ("crashing mpeg2 B-frame", "fixtures/crash_bf2.mpg"), ("noise carrier (good)", "fixtures/noise_ok.mpg")]:
    s, up = upload(fn, os.path.basename(fn))
    si, ins = post("/api/inspect", json.dumps({"id": up["id"], "name": up["name"]}), J)
    sl, lk = post("/api/lock", json.dumps({"id": up["id"], "name": up["name"], "key": {"type": "passphrase", "passphrase": "pw"}}), J)
    line = f"{label:24} upload={s} inspect={si} lock={sl}"
    if sl == 200:
        su, un = post("/api/unlock", json.dumps({"id": lk["id"], "name": "locked.mpg", "manifest": lk["manifest"], "key": {"type": "passphrase", "passphrase": "pw"}}), J)
        line += f" slots={lk['slots']} unlock={su} exact={un.get('exact')}"
    else:
        line += f" err={str(lk.get('error'))[:80]!r}"
    print(line)
# connection must still be usable after the failures
s, k = post("/api/keygen", "{}", J)
print("keep-alive still healthy after errors:", s == 200 and "public" in k)
