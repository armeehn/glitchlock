"""A malformed manifest is user input: it must come back 400, never 500."""
import http.client, json, os, sys, copy
# Aim at the live service by default; override to test a scratch instance.
HOST = os.environ.get("GLITCHLOCK_HOST", "glitchlock.hq.ripostelabs.xyz")
c = (http.client.HTTPConnection(HOST, timeout=900) if ":" in HOST
     else http.client.HTTPSConnection(HOST, timeout=900))
J = {"Content-Type": "application/json"}
def post(p, b, h=J):
    c.request("POST", p, body=b, headers=h); r = c.getresponse(); d = r.read()
    try: return r.status, json.loads(d)
    except Exception: return r.status, {"raw": d[:120].decode(errors="replace")}
s, up = post("/api/upload", open("fixtures/noise_ok.mpg","rb").read(), {"Content-Type":"application/octet-stream","X-Filename":"n.mpg"})
s, lk = post("/api/lock", json.dumps({"id":up["id"],"name":up["name"],"key":{"type":"passphrase","passphrase":"pw"}}))
assert s == 200, lk
good = lk["manifest"]
cases = {}
m = copy.deepcopy(good); m["layers"][0] = "not-a-mapping";           cases["layer is a string"] = m
m = copy.deepcopy(good); m["layers"] = "not-a-list";                 cases["layers is a string"] = m
m = copy.deepcopy(good); m["kdf"] = "not-a-dict";                    cases["kdf is a string"] = m
m = copy.deepcopy(good); m["layers"][0]["frames"] = "abc";           cases["frames is a string"] = m
m = copy.deepcopy(good); m["kdf"] = {"algo":"scrypt","salt":"zz","n":1,"r":1,"p":1,"dklen":32}; cases["salt is not hex"] = m
m = copy.deepcopy(good); m["kem"] = "not-a-dict";                     cases["kem is a string"] = m
m = copy.deepcopy(good); m["layers"][0]["buckets"] = "not-a-list";    cases["buckets is a string"] = m
bad = 0
for label, man in cases.items():
    st, body = post("/api/unlock", json.dumps({"id":lk["id"],"name":"locked.mpg","manifest":man,"key":{"type":"passphrase","passphrase":"pw"}}))
    ok = st == 400
    bad += 0 if ok else 1
    print(f"{label:22} -> {st} {'OK' if ok else 'BAD (should be 400)'}  {str(body.get('error'))[:60]!r}")
st, k = post("/api/keygen", "{}")
print("connection healthy after:", st == 200)
print("RESULT", "PASS" if bad == 0 else f"FAIL ({bad} returned 500)")
sys.exit(0 if bad == 0 else 1)
