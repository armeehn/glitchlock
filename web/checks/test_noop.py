"""A no-op lock must come back as a clean 400 carrying the core's explanation, not a 500."""
import http.client, json, os, sys
HOST = os.environ.get("GLITCHLOCK_HOST", "glitchlock.hq.ripostelabs.xyz")
c = (http.client.HTTPConnection(HOST, timeout=600) if ":" in HOST
     else http.client.HTTPSConnection(HOST, timeout=600))
def post(path, body, headers):
    c.request("POST", path, body=body, headers=headers); r = c.getresponse(); return r.status, json.loads(r.read())
s, up = post("/api/upload", open("fixtures/noop.mpg", "rb").read(), {"Content-Type": "application/octet-stream", "X-Filename": "fixtures/noop.mpg"})
s, lk = post("/api/lock", json.dumps({"id": up["id"], "name": up["name"], "key": {"type": "passphrase", "passphrase": "x"}}), {"Content-Type": "application/json"})
ok = s == 400 and "nothing was actually scrambled" in lk.get("error", "")
print(f"no-op lock -> {s} {lk.get('error','')[:70]!r}  {'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
