# Wheel / Vendored-Dependency Integrity Verification

Prove a vendored Python package equals the official PyPI wheel — or detect tampering. Read-only except for temp downloads (%TEMP% or /tmp).

## Steps
1. **Get authoritative metadata from the PyPI JSON API** — do NOT guess `files.pythonhosted.org` URLs (guessing returns 0-byte downloads):
   `GET https://pypi.org/pypi/<pkg>/<version>/json` → pick `urls[]` entry where `filename` endswith `.whl`; read `digests.sha256` and `url`.
2. **Compare the official sha256 with the `--hash=sha256:...`** declared in requirements.txt / lockfiles. Exact string equality expected.
3. **Download + unzip the wheel**; byte-compare every wheel file against the vendored tree (normalize `/` ↔ os.sep). Every file must be byte-identical.
4. **Verify the installed RECORD** (the one in `vendor/<pkg>-<ver>.dist-info/RECORD`):
   - RECORD hashes are **urlsafe-base64 of sha256, NOT hex**. Recompute:
     `base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")` and compare strings.
   - Expected delta vs official wheel RECORD: exactly 1 differing file (RECORD itself, rewritten by the installer) plus 2 added lines:
     - uv: `INSTALLER` with content `uv` (sha256 b64url of `b"uv"` = `5hhM4Q4mYTT9z6QB6PGpUAW81PGNFrYrdXMj4oM_6ak`), and `REQUESTED` (empty file; hash of `b""` = `47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU`).
     - pip: `INSTALLER` content `pip`.
   - All other entries must match byte-for-byte. RECORD's own line carries no hash. Official wheels do NOT contain INSTALLER.
5. **Check runtime enforcement**: the loading code should enforce module origin (module `__file__` resolves under the vendor root) + exact version tuple + required attrs — e.g. the `_validate_pymysql_module` pattern (`tools.py:2280-2330` in datasage-query: origin under vendor_root, `VERSION == (1,2,0)`, `connect` callable, `cursors.SSDictCursor` present, else `QueryFailure("DEPENDENCY_UNTRUSTED")`).

## Pitfalls
- **Hex-vs-base64 confusion caused a false 18/24 "mismatch" alarm** in a real audit; the correct recompute (b64url string comparison) showed 24/24 matching. Always recompute the expected encoding, never eyeball.
- Empty-file digests appear in RECORD (REQUESTED) — expect `47DEQpj8...` for them.
- Padded/standard `base64.b64decode` comparisons are error-prone; prefer exact `urlsafe_b64encode(...).rstrip(b"=")` string comparison against the RECORD value.
- Extra files in the vendored tree that are NOT in RECORD (e.g. `__pycache__/*.pyc`) are pollution (finding for distribution hygiene), not tampering.

## Reusable snippet (per-file RECORD check)
```python
import hashlib, os, base64
def b64url(data): return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
for ln in open(record_path, encoding="utf-8").read().splitlines():
    parts = ln.split(",")
    if len(parts) < 2 or not parts[1].startswith("sha256="): continue
    path, h = parts[0], parts[1].split("=", 1)[1]
    full = os.path.join(vendor_root, path.replace("/", os.sep))
    data = open(full, "rb").read()
    if b64url(hashlib.sha256(data).digest()) != h: print("MISMATCH", path)
```
