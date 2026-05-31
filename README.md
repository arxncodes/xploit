# Xploit - Distributed Node Orchestration Framework

A modular Python framework for centralized command-and-control orchestration of remote tasking agents. Built for **DevOps automation** and **security research**.

---

## Architecture

Xploit uses a modern, single-file V8 architecture (`main.py`) which acts as both the C2 server and the payload generator. 

The framework leverages an asynchronous FastAPI backend to manage agent sessions, track state via `X-Session-ID` headers, and provide a seamless interactive command shell. All agent communication is HTTP/Base64 encoded to bypass basic network filters.

```text
┌─────────────────────────┐         ┌─────────────────────────┐
│     Attacker Node       │         │       Target Node       │
│                         │         │                         │
│  1. Run main.py         │────────►│  4. Run generated       │
│  2. Input IP/Port       │         │     PowerShell payload  │
│  3. Copy Payload        │◄────────│  5. Call home (HTTP)    │
│                         │         │                         │
└────────────┬────────────┘         └────────────┬────────────┘
             │                                   │
             │           HTTP (Base64)           │
             └───────────────────────────────────┘
```

---

## Quick Start / Usage Roadmap

### 1. Start the Controller & Generate Payload

Run the primary server on your host machine (Linux/Windows/macOS). `main.py` requires Python 3.10+ and uses only standard libraries plus `fastapi`/`uvicorn` for the async HTTP server.

```bash
python main.py
```

Upon launching, the tool will:
- Detect your Local and Public IPs.
- Prompt you for a `CALLBACK HOST` (e.g., your public IP, local IP, or an `ngrok` URL).
- Prompt for a `LPORT` (default `8080`).

After configuration, the console will instantly output a `powershell.exe -Enc ...` payload string.

### 2. Deploy the Agent

Copy the generated payload string and execute it directly in a PowerShell terminal on your target node. The agent is memory-resident, loops continuously to poll for tasks, and avoids writing any persistent scripts to disk (unless directed).

### 3. Orchestrating Sessions

Once the agent calls back, you will see a check-in notification. Use the interactive registry shell to manage targets:

- `list-sessions` - View all connected targets, their IPs, IDs, and active status.
- `connect <ID>` - Drop into an interactive shell for a specific agent (e.g., `connect SES-001`).
- `session-stop <ID>` - Terminate the agent process on the remote machine and disconnect it.
- `exit` - Shut down the server gracefully.

While interacting with a session (`PS C:\> ` prompt), you can issue normal PowerShell commands, or use built-in macros like:
- `screenshot` - Captures and uploads a silent screenshot from the target.
- `harvest-browsers` - Exfiltrates credentials, history, and wi-fi passwords.
- `dump-os` / `dump-wifi` - Dumps intelligence via Discord webhook.
- `session-exit` - Background this agent and return to the main `xploit>` registry prompt.

---

## Requirements
- **Python 3.10+** (uses `match` type hints syntax)
- `pip install uvicorn fastapi` 

## Troubleshooting / Frequently Asked Questions

**Q: I get errors about "v6 vs v8 discrepancy" or missing `X-Session-ID`?**
**A:** In prior versions, the payload generation (v6) was separated into `generator/generate.py` and the server was a TCP monolithic socket wrapper in `server/controller.py`. **These legacy files have been deprecated.** Always use `main.py` which provides robust HTTP polling and `X-Session-ID` multiplexing. If you must generate a payload without starting the server, use `generator/generate.py` which has been updated to generate the V8 compatible payload.

**Q: The agent doesn't connect. What gives?**
**A:** Ensure your target can reach the CALLBACK URL you specified. If you are attacking over the internet, you must use something like `ngrok http 8080` and provide the ngrok URL (e.g., `https://random.ngrok-free.app`) as the CALLBACK HOST during generator startup.

---

## Disclaimer

> **This framework is intended for authorized DevOps automation, security research, and educational purposes only.** Unauthorized access to computer systems is illegal. Always obtain proper written authorization before deploying agents on systems you do not own. The authors assume no liability for misuse.
