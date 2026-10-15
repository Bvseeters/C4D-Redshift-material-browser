"""HTTP helpers for the BasPBR Browser — plain Python, no Cinema 4D imports.

Everything goes through one cookie-keeping opener (FreePBR's download form
needs the cookie from the product page). If Python's SSL can't verify a
certificate — Cinema 4D's embedded Python on macOS sometimes has no CA bundle —
we fall back to the system's curl, which uses the OS certificate store, rather
than switching verification off.
"""
import http.cookiejar
import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "BasPBRBrowser/1.0 (Cinema 4D plugin; +https://ambientcg.com)"
TIMEOUT = 30


class Cancelled(Exception):
    """Raised inside a download when the caller's cancel flag is set."""


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi  # present in some C4D builds; harmless when missing
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_jar),
    urllib.request.HTTPSHandler(context=_ssl_context()))


def _request(url, data=None, headers=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    return urllib.request.Request(url, data=body, headers=h)


def _is_ssl_error(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _curl(url, out_path=None, data=None, headers=None):
    """System curl fallback (Windows 10+ and macOS ship it)."""
    exe = shutil.which("curl")
    if not exe:
        raise RuntimeError("SSL verification failed and curl isn't available")
    cmd = [exe, "-sSL", "--fail", "-A", USER_AGENT, "--max-time", "600"]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    if data is not None:
        cmd += ["--data", urllib.parse.urlencode(data)]
    if out_path:
        cmd += ["-o", out_path]
    cmd.append(url)
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
    r = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError("curl failed (%d): %s" % (r.returncode, r.stderr.decode("utf-8", "ignore").strip()))
    return r.stdout


def get_bytes(url, data=None, headers=None, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            with _opener.open(_request(url, data, headers), timeout=TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            if _is_ssl_error(exc):
                return _curl(url, data=data, headers=headers)
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise last


def get_text(url, data=None, headers=None):
    return get_bytes(url, data, headers).decode("utf-8", "replace")


def get_json(url):
    return json.loads(get_text(url))


def download(url, dest, progress=None, cancel=None, data=None, headers=None):
    """Stream url to dest (via dest + '.part', renamed when complete).

    progress(done_bytes, total_bytes_or_0) is called as data arrives; cancel is
    an object with is_set() (e.g. threading.Event). Returns the response's
    Content-Type.
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    try:
        try:
            resp = _opener.open(_request(url, data, headers), timeout=TIMEOUT)
        except Exception as exc:
            if _is_ssl_error(exc):
                _curl(url, out_path=part, data=data, headers=headers)
                os.replace(part, dest)
                return ""
            raise
        with resp:
            total = int(resp.headers.get("Content-Length") or 0)
            ctype = resp.headers.get("Content-Type", "")
            done = 0
            with open(part, "wb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        os.replace(part, dest)
        return ctype
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
