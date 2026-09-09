"""Pure random noise has no motion vectors, so a noise MPEG-4 carrier must be refused, not silently copied."""
import http.client, json, sys
c = http.client.HTTPSConnection("glitchlock.hq.ripostelabs.xyz", timeout=900)
J = {"Content-Type": "application/json"}
def post(p, b, h=J):
    c.request("POST", p, body=b, headers=h); r = c.getresponse(); d = r.read()
    try: return r.status, json.loads(d)
    except Exception: return r.status, {"raw": d[:200].decode(errors="replace")}
s, up = post("/api/upload", open("fixtures/noise_mpeg4.mpg", "rb").read(),
             {"Content-Type": "application/octet-stream", "X-Filename": "noise_mpeg4.mpg"})
si, ins = post("/api/inspect", json.dumps({"id": up["id"], "name": up["name"]}))
sl, lk = post("/api/lock", json.dumps({"id": up["id"], "name": up["name"],
                                       "key": {"type": "passphrase", "passphrase": "pw"}}))
print(f"inspect={si} lockable={ins.get('lockable')}  lock={sl}")
print("message:", str(lk.get("error"))[:120])
ok = sl == 400 and "nothing was actually scrambled" in str(lk.get("error"))
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
