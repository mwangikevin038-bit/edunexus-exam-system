"""
Waitress production server for EduNexus Exam System (Windows).
Starts Redis, Celery worker, and the Waitress web server together.

Usage:
    python run_server.py
"""
import os
import sys
import subprocess
import signal
import time

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ── Find executables ────────────────────────────────────────────────────────
REDIS_SERVER = None
for candidate in [
    r"C:\Users\1030 G3\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\Redis-8.8.0-Windows-x64-msys2\redis-server.exe",
    r"C:\Users\1030 G3\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\redis-server.exe",
    "redis-server",
]:
    if os.path.isfile(candidate):
        REDIS_SERVER = candidate
        break

CELERY_EXE = None
for candidate in [
    os.path.join(sys.prefix, "Scripts", "celery.exe"),
    os.path.join(sys.prefix, "bin", "celery"),
    "celery",
]:
    if os.path.isfile(candidate):
        CELERY_EXE = candidate
        break

REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
CELERY_APP = os.environ.get("CELERY_APP", "school")
CELERY_LOGLEVEL = os.environ.get("CELERY_LOGLEVEL", "info")

# ── Track child processes ───────────────────────────────────────────────────
_child_procs = []


def _start_redis():
    """Start Redis server if not already running."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(("127.0.0.1", REDIS_PORT))
        sock.close()
        print(f"  [redis]  Already running on port {REDIS_PORT}")
        return None
    except (ConnectionRefusedError, OSError, TimeoutError):
        pass

    if not REDIS_SERVER:
        print("  [redis]  WARNING: redis-server not found, skipping")
        return None

    redis_dir = os.path.join(PROJECT_ROOT, "redis_data")
    os.makedirs(redis_dir, exist_ok=True)

    proc = subprocess.Popen(
        [REDIS_SERVER, "--port", str(REDIS_PORT),
         "--dir", redis_dir, "--loglevel", "warning"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  [redis]  Started (PID {proc.pid}) on port {REDIS_PORT}")
    time.sleep(1)
    return proc


def _start_celery():
    """Start Celery worker."""
    if not CELERY_EXE:
        print("  [celery] WARNING: celery not found, skipping")
        return None

    proc = subprocess.Popen(
        [CELERY_EXE, "-A", CELERY_APP, "worker",
         "-l", CELERY_LOGLEVEL,
         "-P", "solo",
         "--concurrency=2",
         "--max-tasks-per-child=200",
         "-Q", "default,csv_upload"],
        cwd=PROJECT_ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  [celery] Started (PID {proc.pid})")
    return proc


def _shutdown_all(procs):
    """Gracefully shut down child processes."""
    for p in procs:
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


if __name__ == '__main__':
    # ── Activate Django ──────────────────────────────────────────────────────
    import django
    django.setup()

    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    application = get_wsgi_application()

    host = os.environ.get('WAITRESS_HOST', '0.0.0.0')
    port = int(os.environ.get('WAITRESS_PORT', '8000'))
    threads = int(os.environ.get('WAITRESS_THREADS', str(min(16, (os.cpu_count() or 4) * 4))))

    print(f"=" * 60)
    print(f"  EDUNEXUS Production Server")
    print(f"  Listening on: http://{host}:{port}")
    print(f"  Threads: {threads}")
    print(f"=" * 60)

    # ── Start Redis & Celery ─────────────────────────────────────────────────
    print(f"  Starting services...")
    redis_proc = _start_redis()
    celery_proc = _start_celery()
    _child_procs = [redis_proc, celery_proc]

    print(f"=" * 60)
    print(f"  All services running. Press CTRL-BREAK to stop.")
    print(f"=" * 60)

    # ── Handle shutdown ──────────────────────────────────────────────────────
    def _signal_handler(sig, frame):
        print("\n  Shutting down...")
        _shutdown_all(_child_procs)
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        serve(
            application,
            host=host,
            port=port,
            threads=threads,
            channel_timeout=120,
            cleanup_interval=30,
            max_request_body_size=10 * 1024 * 1024,
            recv_bytes=65536,
        )
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_all(_child_procs)
