# SonicBit Python SDK Issues

Summary of issues encountered while building the [home-assistant-sonicbit](https://github.com/) integration, intended to guide a PR to the [sonicbit-python-sdk](https://github.com/viperadnan-git/sonicbit-python-sdk).

---

## Issue 1: `Auth.login()` Uses a Detached Request — Session Cookie Never Set

**File:** `sonicbit/modules/auth.py`

`Auth.login()` is a `@staticmethod` that calls `requests.request()` (a one-off function-level call), **not** `self.session.post()`. This means the `Set-Cookie` headers from the `/web/login` response are never stored in the shared `requests.Session` cookie jar.

The Bearer token returned by login works for most API endpoints (torrents, storage, etc.), but the `/api/file-manager` endpoint requires a **web session cookie** (`sonicbit_session`). Without it, the server returns an HTML redirect or empty body instead of JSON.

**Impact:** `list_files()` and `delete_file()` fail with `InvalidResponseError: Server returned invalid JSON data:` because the session cookie was never set.

**Suggested Fix:** Perform the login via `self.session.post()` instead of `requests.request()`, or add a dedicated method that authenticates the web session and stores cookies in the shared `Session` object.

---

## Issue 2: `list_files()` Intermittently Returns Empty Response Body

**File:** `sonicbit/modules/file.py`, `sonicbit/types/file_list.py`

Even after manually authenticating the web session (by calling `session.post("/web/login")` ourselves), `list_files()` intermittently receives an empty response body, causing `response.json()` to raise `JSONDecodeError` → `InvalidResponseError`.

**Observed behavior:**
- A raw `session.get("/file-manager", params=...)` call made immediately before `list_files()` returns **HTTP 200 with valid JSON** (e.g., 877 bytes).
- The SDK's `list_files()` call, using the **exact same session and parameters**, then gets an **empty response body**.

**Suspected cause:** The `urllib3.Retry` adapter configured in `SonicBitBase.__init__()` (`Retry(connect=3, backoff_factor=1.5, backoff_max=120)`) may be interfering with response body consumption on retried connections. Some `urllib3` retry configurations can cause the response body to be read/discarded during retry evaluation, leaving subsequent reads empty.

**Suggested Fix Options:**
1. In `FileList.from_response()`, capture `response.text` first, then parse with `json.loads(response.text)` instead of calling `response.json()`. This avoids potential double-read issues.
2. Investigate whether the `Retry` adapter configuration needs `respect_retry_after_header` or other settings adjusted.
3. Add response status code and content-type to the `InvalidResponseError` message for better diagnostics.

---

## Issue 3: `InvalidResponseError` Lacks Diagnostic Context

**File:** `sonicbit/types/file_list.py`

The current error message is:
```python
raise InvalidResponseError(f"Server returned invalid JSON data: {response.text}")
```

When the response body is empty, this produces: `Server returned invalid JSON data: ` (with nothing after the colon), which is unhelpful for debugging.

**Suggested Fix:** Include the HTTP status code, content-type, and content-length:
```python
raise InvalidResponseError(
    f"Server returned invalid JSON (HTTP {response.status_code}, "
    f"Content-Type={response.headers.get('Content-Type', 'unknown')}, "
    f"Content-Length={len(response.text)}): {response.text[:200]}"
) from None
```

---

## Issue 4: `delete_file()` Has No Error Handling for Invalid JSON

**File:** `sonicbit/modules/file.py`

```python
response = self.session.post(self.url("/file-manager"), data=data)
json_data = response.json()  # Can raise JSONDecodeError
return json_data.get("success", False)
```

If the session cookie is expired, `response.json()` will raise an unhandled `JSONDecodeError`. This should be wrapped in the same `InvalidResponseError` pattern used by `FileList.from_response()`.

---

## Issue 5: Package Structure — `sonicbit.types` vs `sonicbit.models`

The SDK has both `sonicbit/types/` and `sonicbit/models/` directories. Some installations expose `sonicbit.models.path_info.PathInfo` but **not** `sonicbit.types.path_info.PathInfo` (or vice versa). This caused `ModuleNotFoundError: No module named 'sonicbit.types'` on our Home Assistant server even though the local dev environment had it.

**Suggested Fix:** Consolidate to a single namespace, or ensure both are always available. At minimum, document which import paths are part of the public API.

---

## Workaround Applied in home-assistant-sonicbit

In `coordinator.py`, the `_delete_drive_entry()` method bypasses the SDK entirely for file-manager operations:

1. **Session management:** A `_refresh_web_session()` method manually calls `session.post("/web/login")`, clears stale cookies first, and validates the response (HTTP status + cookie presence).
2. **Listing files:** Makes a raw `session.get("/file-manager", params=...)` call and parses `resp.json()` directly instead of using `client.list_files()`.
3. **Deleting files:** Makes a raw `session.post("/file-manager", data=...)` call instead of using `client.delete_file()`.
4. **Path data:** Uses the raw `data_drive_path` list from the API response directly instead of constructing a `PathInfo` object.
5. **Retry logic:** On failure, refreshes the web session and retries once.

---

## Sections to Restore to SDK Calls After Fix

Once the SDK issues above are resolved, the following sections in `coordinator.py` can be simplified back to SDK calls:

### 1. `_refresh_web_session()` (lines ~133–198)
**Currently:** Manual `session.post()` to `/web/login` with cookie clearing, status validation, and cookie-presence checks.
**Restore to:** Remove entirely if `Auth.login()` properly populates the session cookie jar. The SDK's constructor should handle this.

### 2. `_delete_drive_entry()` — File Listing (lines ~692–726)
**Currently:** Raw `session.get("/file-manager", params=...)` with manual JSON parsing and retry loop.
**Restore to:** `client.list_files(list_path)` — the single SDK call that this replaces.

### 3. `_delete_drive_entry()` — File Deletion (lines ~728–764)
**Currently:** Raw `session.post("/file-manager", data=...)` with manual `FileCommand.REMOVE` construction and JSON response parsing.
**Restore to:** `client.delete_file(item, is_directory=item.is_directory)` using the `File` object from `list_files()`.

### 4. `_get_client()` — Session Login Call (line ~130)
**Currently:** Calls `self._refresh_web_session()` after client creation to populate session cookies.
**Restore to:** Remove the `_refresh_web_session()` call if `SonicBit.__init__()` / `Auth.__init__()` properly populates cookies via the shared session.

### 5. Imports to Remove
Once restored, these imports inside `_delete_drive_entry()` are no longer needed:
- `import json as _json`
- `from sonicbit.enums import FileCommand`

The method would only need `from sonicbit.models.path_info import PathInfo` (for `list_path`).
