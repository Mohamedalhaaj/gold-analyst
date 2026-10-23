# Vendored files (frozen, listed in analyst/FROZEN_ANALYST.sha256)

- `eproc.py`: byte-identical copy of gold-forward-watch `analysis/system/lab/eproc.py` (lab L2 §9.1 e-process),
  sha256 `ee8d456ae3b5ddd1112bad92d12fffa95d2b12e0559335233c6a9b3a68067973`, the same at tag lab-v1-freeze 6cd6d57.
  Used only by the workflows (needs numpy); never by the routine session.
- `tzdata/`: the `tzdata` 2026.3 wheel (IANA 2026c), Apache-2.0 (`tzdata.LICENSE`); `tzdata.RECORD` holds the wheel's
  RECORD lines for these files. `ga/__init__.py` calls `zoneinfo.reset_tzpath(to=())`, so zoneinfo reads only this
  package (register R-AN-8).
