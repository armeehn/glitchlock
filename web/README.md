# glitchlock web service

The web front end. Until 2026-09-09 these files existed only on the host that
runs them and were in no repository, so a rebuild of that host would have lost
them.

## Layout

| Path | What it is |
|---|---|
| `server.py` | The whole service. Standard-library HTTP server, no framework. |
| `static/index.html` | The UI. Vanilla JS, no build step. |
| `static/bench.html` | The self-contained proof bench served at `/bench`. |
| `static/riposte-brand.css` | Brand CSS, vendored from [riposte-brand](https://github.com/armeehn/riposte-brand). |
| `static/fonts/` | JetBrains Mono subsets, vendored from the same place, under `OFL-1.1.txt`. |
| `glitchlock.service` | The systemd unit as deployed. |

Nothing here is installed by `pip`. The package in `src/` is installed into the
venv; these files are copied to `/opt/glitchlock/` separately. That split is
deliberate and easy to forget: **`pip install` does not update the server.**

The CSS and fonts are vendored because the service is meant to run without
reaching any CDN. They are a second copy of the brand repo, so they can drift.

## Deploying

```
tar --exclude=.git --exclude=.venv -czf /tmp/glitchlock.tgz .
# unpack over /opt/src/glitchlock on the service box, then:
/opt/glitchlock-venv/bin/pip install "/opt/src/glitchlock[recipients]"
install -m644 web/server.py /opt/glitchlock/server.py
cp -a web/static/. /opt/glitchlock/static/
systemctl restart glitchlock
```

The version string stays `1.0.0` across releases, so it proves nothing about
what is deployed. Confirm the code is live by grepping for a marker you just
changed, not by reading the version.

## Two failure modes worth knowing

**Never pipe a command whose exit status matters.** `ffedit -version | head`
reports *head's* status, so a dead binary passes the check. This has already
caused a CI job to report success for months while every test skipped.

**Uncaught exception classes become 500s.** Every field of a pasted manifest is
untrusted in *shape*, not only in value. Three bugs of exactly this kind have
been fixed here: a refused no-op lock raised `NoOpLock`, a layer that is not an
object raised `TypeError`, and a `kdf` that is not an object raised
`AttributeError`. All three escaped the `ValueError` arm of `do_POST` and
reached the browser as "internal error". Add new handlers to that arm.
