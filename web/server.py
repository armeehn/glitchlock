#!/usr/bin/env python3
"""Web front end for the glitchlock CLI.

Deliberately stdlib-only, in the same spirit as the tool it wraps. Uploads
arrive as raw request bodies rather than multipart forms, so there is no form
parser to get wrong and no dependency to install.

Sits behind an authenticating reverse proxy, so it carries no auth of its own.
It must never be bound anywhere the proxy is not in front of it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from glitchlock import ffg, pubkey
from glitchlock.core import LockError, lock, select_features, unlock, lockable_features
from glitchlock.crypto import (
    derive_key_from_password, random_nonce, random_salt, sha256_file,
)
from glitchlock.manifest import Layer, Manifest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
STORE = "/var/lib/glitchlock/jobs"

PORT = int(os.environ.get("GLITCHLOCK_PORT", "8087"))
MAX_UPLOAD = int(os.environ.get("GLITCHLOCK_MAX_UPLOAD", str(64 * 1024 * 1024)))
RETENTION_S = int(os.environ.get("GLITCHLOCK_RETENTION", str(60 * 60)))
STORE_CAP = int(os.environ.get("GLITCHLOCK_STORE_CAP", str(4 * 1024 * 1024 * 1024)))

ID_RE = re.compile(r"^[0-9a-f]{32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

_sweep_lock = threading.Lock()
_last_sweep = 0.0


class _AlreadyAnswered(Exception):
    """Raised once a response has been written, to unwind without a second one."""


# ------------------------------------------------------------------- storage


def sweep() -> None:
    """Drop expired jobs, and oldest-first if the store is over its cap.

    Called opportunistically rather than on a timer: this is a tool people use
    in bursts, so there is no point running a thread to watch an idle disk.
    """
    global _last_sweep
    with _sweep_lock:
        if time.time() - _last_sweep < 30:
            return
        _last_sweep = time.time()
    now = time.time()
    entries = []
    for name in os.listdir(STORE):
        path = os.path.join(STORE, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        size = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        entries.append((st.st_mtime, size, path))

    for mtime, _size, path in entries:
        if now - mtime > RETENTION_S:
            shutil.rmtree(path, ignore_errors=True)

    alive = [e for e in entries if now - e[0] <= RETENTION_S]
    total = sum(e[1] for e in alive)
    for mtime, size, path in sorted(alive):
        if total <= STORE_CAP:
            break
        shutil.rmtree(path, ignore_errors=True)
        total -= size


def new_job() -> str:
    jid = uuid.uuid4().hex
    os.makedirs(os.path.join(STORE, jid), exist_ok=True)
    return jid


def job_dir(jid: str) -> str:
    if not ID_RE.match(jid or ""):
        raise ValueError("bad id")
    path = os.path.join(STORE, jid)
    if not os.path.isdir(path):
        raise FileNotFoundError("that file has expired - uploads are kept for one hour")
    return path


def job_file(jid: str, name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("bad name")
    path = os.path.join(job_dir(jid), name)
    # belt and braces against traversal via a crafted but regex-passing name
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(job_dir(jid)):
        raise ValueError("bad name")
    return path


def touch(jid: str) -> None:
    try:
        os.utime(os.path.join(STORE, jid), None)
    except OSError:
        pass


# --------------------------------------------------------------------- keys


def key_from_spec(spec: dict, manifest: Manifest = None):
    """Turn the UI's key description into ``(key_bytes, salt_or_None)``.

    Key material is never written to the response, the log, or disk.
    """
    kind = (spec or {}).get("type", "passphrase")

    if kind == "passphrase":
        phrase = (spec or {}).get("passphrase") or ""
        if not phrase:
            raise ValueError("a passphrase is required")
        if manifest is not None:
            kdf = manifest.kdf or {}
            if kdf.get("algo") != "scrypt":
                raise ValueError(
                    "this manifest was not locked with a passphrase "
                    f"(it used {kdf.get('algo', 'an unknown method')})")
            salt = bytes.fromhex(kdf["salt"])
        else:
            salt = random_salt()
        return derive_key_from_password(phrase, salt), salt

    if kind == "identity":
        secret = ((spec or {}).get("identity") or "").strip()
        if not secret:
            raise ValueError("a private identity is required")
        if manifest is None or not manifest.kem:
            raise ValueError("this file was not locked to a public key")
        return pubkey.unseal(manifest.kem, secret), None

    raise ValueError(f"unknown key type {kind!r}")


# ------------------------------------------------------------------ preview


def make_preview(src: str, dst: str, height: int = 288) -> bool:
    """Re-encode to H.264 so a browser can play it. Illustration, not proof."""
    if os.path.exists(dst):
        return True
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", src,
         "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-crf", "32",
         "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", dst],
        capture_output=True, text=True, timeout=300)
    return proc.returncode == 0 and os.path.exists(dst)


# ------------------------------------------------------------------ handlers


class Handler(BaseHTTPRequestHandler):
    server_version = "glitchlock-hq"
    protocol_version = "HTTP/1.1"
    #: bytes of the current request body the handler has read
    _consumed = 0

    # keep the journal readable; Caddy already logs requests
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -------------------------------------------------------------- plumbing

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _fail(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    #: How much of an over-sized upload to swallow so the client can actually
    #: read our 413. Rejecting on Content-Length alone and closing mid-stream
    #: gives the browser a connection reset and a useless "network error"
    #: instead of the message explaining the limit.
    DRAIN_CAP = 512 * 1024 * 1024

    def _too_large(self, length, limit):
        drained = 0
        while drained < min(length, self.DRAIN_CAP):
            chunk = self.rfile.read(min(1 << 20, length - drained))
            if not chunk:
                break
            drained += len(chunk)
        self._send(
            413,
            json.dumps({"error":
                        f"that file is {length // 1048576} MB and the limit here is "
                        f"{limit // 1048576} MB. Use the command line for bigger jobs."}),
            "application/json; charset=utf-8",
            {"Connection": "close"},
        )
        self.close_connection = True

    def _body(self, limit=MAX_UPLOAD):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("empty request body")
        if length > limit:
            self._too_large(length, limit)
            raise _AlreadyAnswered()
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(min(1 << 20, length - len(data)))
            if not chunk:
                break
            data += chunk
        self._consumed += len(data)
        return data

    def _drain_request(self):
        """Swallow any part of the request body the handler did not read.

        On a keep-alive connection an unread body is not harmless: it stays in
        the socket and the *next* request gets parsed starting at those leftover
        bytes. A handler that ignores its body (``/api/sample``, ``/api/keygen``)
        would otherwise corrupt whatever the browser sent next, which shows up as
        a nonsense 501 or 414 on an unrelated endpoint.
        """
        if self.close_connection:
            return
        left = (int(self.headers.get("Content-Length") or 0)) - self._consumed
        while left > 0:
            chunk = self.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            left -= len(chunk)

    def _json_body(self):
        return json.loads(self._body(4 * 1024 * 1024).decode("utf-8"))

    def _file(self, path, ctype, download_as=None):
        if not os.path.isfile(path):
            return self._fail("not found", 404)
        extra = {"Cache-Control": "no-store"}
        if download_as:
            extra["Content-Disposition"] = f'attachment; filename="{download_as}"'
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "none")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1 << 20)

    # ------------------------------------------------------------------ GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._consumed = 0
        try:
            path = self.path.split("?", 1)[0]

            if path == "/favicon.ico":
                return self._send(204)
            if path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"),
                                  "text/html; charset=utf-8")
            if path in ("/bench", "/bench.html"):
                return self._file(os.path.join(STATIC, "bench.html"),
                                  "text/html; charset=utf-8")
            if path == "/health":
                return self._json({"ok": True, "ffglitch": ffg.version()})
            if path == "/riposte-brand.css":
                return self._file(os.path.join(STATIC, "riposte-brand.css"),
                                  "text/css; charset=utf-8")
            if path.startswith("/fonts/"):
                name = path[len("/fonts/"):]
                if not NAME_RE.match(name):
                    return self._fail("bad name", 400)
                return self._file(os.path.join(STATIC, "fonts", name), "font/woff2")

            m = re.match(r"^/d/([0-9a-f]{32})/([A-Za-z0-9._-]{1,80})$", path)
            if m:
                jid, name = m.groups()
                touch(jid)
                return self._file(job_file(jid, name),
                                  "application/octet-stream", download_as=name)

            m = re.match(r"^/preview/([0-9a-f]{32})/([A-Za-z0-9._-]{1,80})$", path)
            if m:
                jid, name = m.groups()
                touch(jid)
                src = job_file(jid, name)
                dst = src + ".preview.mp4"
                if not make_preview(src, dst):
                    return self._fail("could not build a preview for this file", 500)
                return self._file(dst, "video/mp4")

            return self._fail("not found", 404)
        except FileNotFoundError as exc:
            self._fail(exc, 410)
        except ValueError as exc:
            self._fail(exc, 400)
        except Exception:
            traceback.print_exc()
            self._fail("internal error", 500)
        finally:
            self._drain_request()

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        self._consumed = 0
        try:
            sweep()
            path = self.path.split("?", 1)[0]
            route = {
                "/api/upload": self.api_upload,
                "/api/sample": self.api_sample,
                "/api/inspect": self.api_inspect,
                "/api/prepare": self.api_prepare,
                "/api/lock": self.api_lock,
                "/api/unlock": self.api_unlock,
                "/api/keygen": self.api_keygen,
            }.get(path)
            if not route:
                return self._fail("not found", 404)
            return route()
        except _AlreadyAnswered:
            return
        except FileNotFoundError as exc:
            self._fail(exc, 410)
        except (ValueError, pubkey.PubKeyError) as exc:
            self._fail(exc, 400)
        except ffg.FFglitchError as exc:
            self._fail(f"FFglitch could not process this file: {exc}", 400)
        except LockError as exc:
            # NoOpLock, GeometryError: the core refused, and says why.
            self._fail(exc, 400)
        except subprocess.TimeoutExpired:
            self._fail("that took too long and was stopped", 504)
        except Exception:
            traceback.print_exc()
            self._fail("internal error", 500)
        finally:
            self._drain_request()

    # ------------------------------------------------------------------ api

    def api_upload(self):
        data = self._body()
        name = self.headers.get("X-Filename") or "upload.bin"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name))[:60] or "upload.bin"
        jid = new_job()
        dest = os.path.join(STORE, jid, "source_" + name)
        with open(dest, "wb") as fh:
            fh.write(data)
        return self._json({
            "id": jid, "name": os.path.basename(dest), "bytes": len(data),
            "sha256": sha256_file(dest),
        })

    def api_sample(self):
        """Build a small carrier locally, so the tool can be tried with no upload."""
        jid = new_job()
        src = os.path.join(STORE, jid, "sample_src.mp4")
        subprocess.run(
            [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=320x240:rate=25:duration=4",
             "-vf", "rotate=0.4*sin(2*PI*t/3):c=black",
             "-pix_fmt", "yuv420p", src], check=True, capture_output=True, timeout=180)
        return self._json({
            "id": jid, "name": "sample_src.mp4",
            "bytes": os.path.getsize(src), "sha256": sha256_file(src),
        })

    def api_inspect(self):
        req = self._json_body()
        path = job_file(req["id"], req["name"])
        touch(req["id"])
        return self._json({
            "codec": ffg.codec_name(path),
            "features": ffg.supported_features(path),
            "lockable": lockable_features(path),
            "sha256": sha256_file(path),
            "bytes": os.path.getsize(path),
        })

    def api_prepare(self):
        req = self._json_body()
        src = job_file(req["id"], req["name"])
        touch(req["id"])
        out = os.path.join(job_dir(req["id"]), "carrier.mpg")
        ffg.transcode(
            src, out,
            codec=req.get("codec", "mpeg2video"),
            qscale=max(2, min(20, int(req.get("qscale", 6)))),
            gop=max(2, min(300, int(req.get("gop", 25)))),
            closed_gop=bool(req.get("closed_gop", False)),
        )
        return self._json({
            "id": req["id"], "name": "carrier.mpg",
            "bytes": os.path.getsize(out), "sha256": sha256_file(out),
            "lockable": lockable_features(out), "codec": ffg.codec_name(out),
        })

    def api_lock(self):
        req = self._json_body()
        jid = req["id"]
        src = job_file(jid, req["name"])
        touch(jid)

        features = select_features(src, req.get("features") or None)
        mode = req.get("mode", "full")
        intensity = float(req.get("intensity", 1.0))
        segment = int(req.get("segment", 0))

        spec = req.get("key") or {}
        kem = None
        salt = None
        if spec.get("type") == "recipient":
            recipients = [r.strip() for r in (spec.get("recipients") or []) if r.strip()]
            if not recipients:
                raise ValueError("at least one recipient public key is required")
            key = pubkey.new_content_key()
            kem = pubkey.seal(key, recipients)
            kdf = {"algo": "x25519-kem"}
        else:
            key, salt = key_from_spec(spec)
            kdf = {"algo": "scrypt", "n": 1 << 15, "r": 8, "p": 1,
                   "salt": salt.hex(), "dklen": 32}

        out = os.path.join(job_dir(jid), "locked.mpg")
        result = lock(src, out, key=key, nonce=random_nonce(), features=features,
                      mode=mode, intensity=intensity, segment=segment,
                      selftest=bool(req.get("selftest", True)))
        manifest = result.manifest
        manifest.kdf = kdf
        manifest.kem = kem
        manifest.sign(key)
        mpath = os.path.join(job_dir(jid), "manifest.json")
        manifest.save(mpath)

        return self._json({
            "id": jid,
            "locked": {"name": "locked.mpg", "bytes": os.path.getsize(out),
                       "sha256": manifest.locked_sha256},
            "carrier": {"name": req["name"], "bytes": manifest.carrier_bytes,
                        "sha256": manifest.carrier_sha256},
            "manifest": manifest.to_dict(),
            "manifest_name": "manifest.json",
            "selftest": manifest.selftest,
            "features": features,
            "growth": round(os.path.getsize(out) / max(1, manifest.carrier_bytes), 3),
            "slots": sum(l.slots_touched for l in manifest.layers),
            "repairs": sum(len(l.repairs) for l in manifest.layers),
        })

    def api_unlock(self):
        req = self._json_body()
        jid = req["id"]
        src = job_file(jid, req["name"])
        touch(jid)

        raw = req.get("manifest")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError("a manifest is required")
        if raw.get("format") != "glitchlock-manifest":
            raise ValueError("that JSON is not a glitchlock manifest")
        raw = dict(raw)

        # A pasted manifest is user input, so its *shape* is untrusted too, not
        # just its contents. Without these two checks a layer that is a string
        # reaches Layer(**"abc") as a TypeError and a kdf that is a string
        # reaches kdf.get() as an AttributeError - both escape the ValueError
        # arm below and surface as a 500 "internal error", which tells the
        # person holding a hand-edited manifest nothing at all.
        raw_layers = raw.get("layers", [])
        if not isinstance(raw_layers, list) or not all(isinstance(l, dict) for l in raw_layers):
            raise ValueError("the manifest is malformed: layers must be a list of objects")

        if raw.get("kdf") is not None and not isinstance(raw["kdf"], dict):
            raise ValueError("the manifest is malformed: kdf must be an object")

        layer_fields = set(Layer.__dataclass_fields__)
        manifest_fields = set(Manifest.__dataclass_fields__) - {"layers"}
        layers = [Layer(**{k: v for k, v in l.items() if k in layer_fields})
                  for l in raw.pop("layers", [])]
        manifest = Manifest(
            layers=layers,
            **{k: v for k, v in raw.items() if k in manifest_fields})

        key, _salt = key_from_spec(req.get("key") or {}, manifest)

        if not manifest.verify(key):
            raise ValueError(
                "wrong key, or the manifest has been altered - the manifest's "
                "authentication tag does not match")

        out = os.path.join(job_dir(jid), "restored.mpg")
        unlock(src, out, key=key, manifest=manifest, verify_input=False)
        digest = sha256_file(out)
        return self._json({
            "id": jid, "name": "restored.mpg",
            "bytes": os.path.getsize(out), "sha256": digest,
            "expected": manifest.carrier_sha256,
            "exact": bool(manifest.carrier_sha256) and digest == manifest.carrier_sha256,
        })

    def api_keygen(self):
        secret, public = pubkey.generate_identity()
        return self._json({
            "secret": secret, "public": public,
            "fingerprint": pubkey.fingerprint(public),
        })


def main():
    os.makedirs(STORE, exist_ok=True)
    ffg.ffedit_path()  # fail fast and loudly if FFglitch is missing
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"glitchlock web on :{PORT}, store {STORE}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
