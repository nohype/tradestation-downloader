# Research: Python 3.13+ Migration for tradestation-downloader

## Part 1 — Full Scope of Python-Version Incompatibilities

### Methodology

Scanned every `.py` file in `tradestation/`, `tests/`, entry-point wrappers
(`tradestation_downloader.py`, `setup_auth.py`), and `pyproject.toml` for:
- `pathlib.Path` methods that changed signatures across versions
- `sys.version_info` checks or version-conditional code
- `typing` features that changed (`TypeAlias`, `type X = ...`, `Self`, `override`)
- Deprecated/removed stdlib modules (`asynchat`, `asyncore`, `cgi`, `imp`, `pkgutil.ImpImporter`, `distutils`)
- `unittest.mock` / `pytest` features that differ
- Any code assuming Python <= 3.11 behavior

### Findings

| # | File:Line | Code | Issue | Breaks on |
|---|-----------|------|-------|-----------|
| 1 | `tests/test_csv_export_up_down.py:286` | `output.read_text(encoding="utf-8", newline="")` | `Path.read_text()` gained the `newline` parameter in Python 3.12. On 3.11 the signature is `(encoding=None, errors=None)` — confirmed by `inspect.signature` on the 3.11.15 venv. | **3.11** (TypeError) |

**That is the ONLY incompatibility.** Every other `read_text` / `read_bytes` / `write_text` call in the codebase uses only parameters available on all supported versions:

| File:Line | Call | Status |
|-----------|------|--------|
| `tradestation/metadata.py:385` | `output_path.write_text(json_str, encoding="utf-8")` | OK (all versions) |
| `tests/test_csv_export_up_down.py:123` | `output.read_text(encoding="utf-8")` | OK |
| `tests/test_csv_export_up_down.py:149` | `output.read_bytes()` | OK |
| `tests/test_csv_export_up_down.py:167` | `output.read_text(encoding="utf-8")` | OK |
| `tests/test_csv_export_up_down.py:197` | `output.read_text(encoding="utf-8")` | OK |
| `tests/test_csv_export_up_down.py:242` | `output.read_text(encoding="utf-8")` | OK |
| `tests/test_csv_export.py:78` | `output.read_text(encoding="utf-8")` | OK |
| `tests/test_csv_export.py:264` | `(plain_data / "@ES.txt").read_bytes()` | OK |
| `tests/test_metadata.py:609` | `metadata_path.read_text(encoding="utf-8")` | OK |

### Other version-sensitive patterns checked (all clean)

- **`sys.version_info` / version-conditional code**: None found.
- **`from __future__` imports**: None found.
- **`typing.TypeAlias` / `type X = ...` (PEP 695)**: None used. The only `from typing import` is `from typing import Any` at `tradestation/downloader.py:12`.
- **`typing.Self`**: Not used.
- **`typing.override`**: Not used.
- **Removed stdlib modules** (`asynchat`, `asyncore`, `cgi`, `imp`, `distutils`, `pkgutil.ImpImporter`): None imported. The `import importlib` in `tests/test_csv_export_up_down.py:3` and `tests/test_csv_export.py:3` is the standard `importlib` module (not the removed `imp`).
- **`zip(..., strict=True)`**: Used at `tests/test_metadata.py:600,706,971`. Added in Python 3.10 (PEP 618) — fine for 3.10+.
- **`match`/`case` (structural pattern matching)**: None used.
- **Walrus operator `:=`**: None used.
- **`unittest.mock`**: Uses `Mock`, `patch`, `patch.object` — all stable since 3.3+.
- **`lineterminator` in `to_csv`**: `tradestation/csv_export.py:110` uses `lineterminator="\r\n"` — this is a pandas parameter (pandas ≥ 1.5), not a Python stdlib feature. Fine.
- **`os.path` usage**: None — the codebase uses `pathlib.Path` throughout.

### Verdict

**The csv test at `tests/test_csv_export_up_down.py:286` is the ONLY incompatibility.**
It breaks on Python 3.11 (the current venv) because `Path.read_text(newline="")` requires 3.12+.
Once the venv is upgraded to 3.13+, this call works as-is with no code change needed.

---

## Part 2 — Python 3.13+ Simplification Opportunities

Each candidate evaluated against the ponytail principle: does it GENUINELY reduce
complexity (fewer lines / clearer / less boilerplate), or is it just ceremony?

| # | Feature | File:Line | Current code | Proposed 3.13 code | Verdict | Reason |
|---|---------|-----------|--------------|---------------------|---------|--------|
| 1 | `Path.read_text(newline="")` | `tests/test_csv_export_up_down.py:286` | `output.read_text(encoding="utf-8", newline="")` | *(no change)* | **SKIP (already correct)** | Already uses the 3.12+ API. Works as-is on 3.13+. No code change needed. |
| 2 | PEP 695 type aliases (`type X = ...`) | *(none)* | No type aliases exist in the codebase | N/A | **SKIP** | Nothing to convert. The only `from typing import` is `Any` in `downloader.py:12`. |
| 3 | `typing.override` decorator | `tradestation/storage.py:204,221,238,316,333,350` (6 overrides of `StorageBackend` methods) | `def get_last_timestamp(...):` | `@override\ndef get_last_timestamp(...):` | **SKIP** | Adds a line to each method with zero logic reduction. Pure ceremony — the ponytail says don't add it. |
| 4 | `typing.Self` | `tradestation/models.py:17,36` | `def from_string(cls, value: str) -> "StorageFormat":` | `def from_string(cls, value: str) -> Self:` | **SKIP** | 1:1 swap of a string literal for a type. Marginally cleaner but not a complexity reduction. Ceremony. |
| 5 | `pathlib` improvements | *(none)* | Code already uses `pathlib.Path` everywhere; no `os.path` usage | N/A | **SKIP** | Nothing to modernize. |
| 6 | `datetime` improvements | `tradestation/storage.py:176,245` | `datetime.combine(date, datetime.min.time())` | No 3.13-specific simplification exists for this pattern | **SKIP** | No 3.13 datetime feature helps here. The `ponytail:` comment at `metadata.py:94-95` about `Series.dt.dst()` is a pandas limitation, not a Python one. |
| 7 | `logging` improvements | *(none)* | Standard `logging.getLogger(__name__)` / `logging.basicConfig()` | N/A | **SKIP** | No 3.13 logging features are relevant to this codebase. |

### Verdict

**Zero simplifications qualify for adoption.** The codebase is already clean and
idiomatic. Every 3.13+ feature considered would add ceremony without reducing
complexity. The only change needed is the version bump itself.

---

## Part 3 — Python 3.13+ Availability on This Machine

### Installed Python versions (py launcher)

```
 -V:3.14[-64] *   Python 3.14.4
 -V:Astral\CPython3.11.15 CPython 3.11.15 (64-bit)
```

- **Python 3.14.4** — installed and is the default (`py` / `python` / `python3`)
- **Python 3.11.15** — installed (Astral/uv managed, used by current `.venv`)
- **Python 3.13** — NOT installed locally
- **Python 3.12** — NOT installed locally

### `uv python list` (known/available versions)

```
cpython-3.15.0b3-windows-x86_64-none                 <download available>
cpython-3.14.6-windows-x86_64-none                   <download available>
cpython-3.14.4-windows-x86_64-none                   C:\Users\jweiss\AppData\Local\Python\bin\python.exe  (INSTALLED)
cpython-3.13.14-windows-x86_64-none                  <download available>
cpython-3.12.13-windows-x86_64-none                  <download available>
cpython-3.11.15-windows-x86_64-none                  C:\Users\jweiss\AppData\Roaming\uv\python\...  (INSTALLED)
```

- `uv 0.11.25` is installed.
- `uv python find 3.13` → "No interpreter found" (not installed yet, but downloadable).
- `uv python find 3.14` → found at `C:\Users\jweiss\AppData\Local\Python\pythoncore-3.14-64\python.exe`.

### Exact commands to set up the venv

**Option A — use Python 3.14 (already installed, fastest):**
```powershell
uv venv --python 3.14
uv sync
```

**Option B — use Python 3.13 (uv will auto-download 3.13.14):**
```powershell
uv venv --python 3.13
uv sync
```

**Option C — install 3.13 explicitly first, then create venv:**
```powershell
uv python install 3.13
uv venv --python 3.13
uv sync
```

**To pin the project's Python version (writes `.python-version` file):**
```powershell
uv python pin 3.13   # or 3.14
```

> **Recommendation**: Use **Option A (3.14)** since it's already installed and
> satisfies the `>=3.13` requirement. If you want to test on the minimum supported
> version, use Option B/C for 3.13.

---

## Recommended Minimal Plan

The smallest set of changes to (a) require 3.13+, (b) fix the incompatibility,
and (c) adopt only simplifications that genuinely reduce complexity:

### 1. `pyproject.toml` — bump version requirement and ruff target

**`pyproject.toml:11`** — change requires-python:
```toml
# Before
requires-python = ">=3.10"
# After
requires-python = ">=3.13"
```

**`pyproject.toml:31-33`** — update classifiers (remove 3.10/3.11/3.12, add 3.13):
```toml
# Before
"Programming Language :: Python :: 3.10",
"Programming Language :: Python :: 3.11",
"Programming Language :: Python :: 3.12",
# After
"Programming Language :: Python :: 3.13",
```

**`pyproject.toml:68`** — update ruff target-version:
```toml
# Before
target-version = "py310"
# After
target-version = "py313"
```

### 2. Fix the incompatibility — NO CODE CHANGE NEEDED

`tests/test_csv_export_up_down.py:286` already uses `Path.read_text(newline="")`
which works on Python 3.12+. Once the venv is 3.13+, the test passes as-is.

### 3. Adopt 3.13+ simplifications — NONE QUALIFY

No code changes. Every candidate evaluated would add ceremony without reducing
complexity (see Part 2 table).

### 4. Recreate the venv

```powershell
uv venv --python 3.14      # or 3.13
uv sync
```

### Summary of all changes

| File | Line(s) | Change |
|------|---------|--------|
| `pyproject.toml` | 11 | `requires-python = ">=3.13"` |
| `pyproject.toml` | 31-33 | Replace 3.10/3.11/3.12 classifiers with 3.13 |
| `pyproject.toml` | 68 | `target-version = "py313"` |

**Total: 3 lines in 1 file. Zero source code changes. Zero test changes.**
