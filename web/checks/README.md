# Proof battery

These checks run against the **live service** and the real FFglitch binaries.
They are estate-specific: they use `pct exec` to reach the container that has
FFglitch installed, which is the only host that has it.

```
bash run-all.sh            # ten checks, exits non-zero if any fail
bash negative-control.sh   # the same checks against pre-fix code, must FAIL
```

`run-all.sh` passing on its own means very little. Run `negative-control.sh`
too: it starts the previous version of `server.py` from the rollback directory
on a scratch port and runs the same checks against it. They must fail there. A
check that passes against both versions is not testing what it claims to.

The randomised sweeps draw fresh values every run, so counts differ each time.
The part that must hold is `FAIL=0`. A high `NO-OP` count is expected on noise:
noise defeats motion estimation, so the bitstream carries no motion vectors to
scramble and the core refuses rather than hand back the plaintext.
