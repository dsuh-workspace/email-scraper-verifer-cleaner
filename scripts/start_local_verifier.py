"""
Start Local Email Verifier Service (Port 8080).

Launches the native Python verifier server (or Docker container if available)
and confirms it is healthy and responding.
"""

import os
import subprocess
import sys
import time
import requests

PORT = 8080
HEALTH_URL = f"http://127.0.0.1:{PORT}/version"


def is_running() -> bool:
    try:
        r = requests.get(HEALTH_URL, timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def start_verifier():
    if is_running():
        print(f"Local Email Verifier is ALREADY RUNNING at http://127.0.0.1:{PORT}")
        return

    print(f"Starting Local Email Verifier on http://127.0.0.1:{PORT}...")
    server_script = os.path.join(os.path.dirname(__file__), "local_verifier_server.py")
    
    # Launch in background using pythonw.exe to prevent terminal exit termination
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(
            [pythonw, server_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True
        )
    else:
        proc = subprocess.Popen(
            [pythonw, server_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp
        )

    # Poll for health
    for i in range(20):
        time.sleep(0.5)
        if is_running():
            print(f"[SUCCESS] Local Email Verifier is now ONLINE at http://127.0.0.1:{PORT} (PID: {proc.pid})")
            return

    print("[ERROR] Timed out waiting for Local Email Verifier to start.")


if __name__ == "__main__":
    start_verifier()
