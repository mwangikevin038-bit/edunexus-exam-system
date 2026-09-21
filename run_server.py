"""
EduNexus Production Server (Windows).
Starts Redis, Celery workers, and Waitress together with health checks.

Usage:
    python run_server.py
"""
import os
import sys
import subprocess
import signal
import time
import socket

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ── Config ──────────────────────────────────────────────────────────────────
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
CELERY_APP = os.environ.get("CELERY_APP", "school")
CELERY_LOGLEVEL = os.environ.get("CELERY_LOGLEVEL", "info")
WAITRESS_HOST = os.environ.get('WAITRESS_HOST', '0.0.0.0')
WAITRESS_PORT = int(os.environ.get('WAITRESS_PORT', '8000'))
WAITRESS_THREADS = int(os.environ.get('WAITRESS_THREADS', str(min(32, (os.cpu_count() or 4) * 8))))

# ── Colors ──────────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"

def log(icon, msg, color=C.WHITE):
    print(f"  {color}{icon}{C.RESET} {C.DIM}{msg}{C.RESET}")

def ok(msg):   log("✓", msg, C.GREEN)
def warn(msg): log("⚠", msg, C.YELLOW)
def err(msg):  log("✗", msg, C.RED)
def info(msg): log("→", msg, C.CYAN)

# ── Find executables ────────────────────────────────────────────────────────
def _find_redis():
    for candidate in [
        r"C:\Users\1030 G3\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\Redis-8.8.0-Windows-x64-msys2\redis-server.exe",
        r"C:\Users\1030 G3\AppData\Local\Microsoft\WinGet\Packages\taizod1024.redis-windows-fork_Microsoft.Winget.Source_8wekyb3d8bbwe\redis-server.exe",
        "redis-server",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None

def _find_celery():
    for candidate in [
        os.path.join(sys.prefix, "Scripts", "celery.exe"),
        os.path.join(sys.prefix, "bin", "celery"),
        "celery",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return None

REDIS_SERVER = _find_redis()
CELERY_EXE = _find_celery()

# ── Process tracking ────────────────────────────────────────────────────────
_children = []

def _is_port_open(port, host="127.0.0.1"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect((host, port))
        s.close()
        return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False

def _wait_for_port(port, retries=10, delay=0.5):
    for _ in range(retries):
        if _is_port_open(port):
            return True
        time.sleep(delay)
    return False

# ── Service starters ────────────────────────────────────────────────────────
def start_redis():
    if _is_port_open(REDIS_PORT):
        ok(f"Redis already running on port {REDIS_PORT}")
        return None

    if not REDIS_SERVER:
        warn("redis-server not found — skipping (background tasks disabled)")
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

    if _wait_for_port(REDIS_PORT, retries=15, delay=0.3):
        ok(f"Redis started (PID {proc.pid}) on port {REDIS_PORT}")
        return proc
    else:
        err("Redis started but port not responding — check redis-server")
        return proc

def start_celery_workers():
    procs = []
    if not CELERY_EXE:
        warn("celery not found — skipping (background tasks disabled)")
        return procs

    queues = [
        ("pdf_worker",   "pdf_generation",         4, 20),
        ("default_worker", "default,csv_upload",   4, 200),
    ]

    for name, queues_str, concurrency, max_tasks in queues:
        proc = subprocess.Popen(
            [CELERY_EXE, "-A", CELERY_APP, "worker",
             "-l", CELERY_LOGLEVEL,
             "-P", "prefork",
             f"--concurrency={concurrency}",
             f"--max-tasks-per-child={max_tasks}",
             "-Q", queues_str,
             "-n", f"{name}@%%h"],
            cwd=PROJECT_ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        ok(f"Celery {name} started (PID {proc.pid}) — queues: {queues_str}")

    return procs

def _shutdown_all(procs):
    for p in procs:
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
            except Exception:
                pass

def _check_port_available(port):
    if _is_port_open(port):
        err(f"Port {port} is already in use!")
        info("Another server may be running. Stop it first or change WAITRESS_PORT.")
        return False
    return True

# ── Banner ──────────────────────────────────────────────────────────────────
def print_banner(host, port, threads):
    w = 60
    cpu_count = os.cpu_count() or 1
    print()
    print(f"  {C.GREEN}{'━' * w}{C.RESET}")
    print(f"  {C.GREEN}{C.BOLD}  EDUNEXUS Production Server{C.RESET}")
    print(f"  {C.GREEN}{'━' * w}{C.RESET}")
    print(f"  {C.WHITE}  URL:      {C.CYAN}http://{host}:{port}{C.RESET}")
    print(f"  {C.WHITE}  Threads:  {C.CYAN}{threads}{C.RESET}  {C.WHITE}CPUs: {C.CYAN}{cpu_count}{C.RESET}")
    print(f"  {C.WHITE}  Redis:    {C.CYAN}port {REDIS_PORT}{C.RESET}")
    print(f"  {C.WHITE}  Workers:  {C.CYAN}pdf_worker(4) + default_worker(4){C.RESET}")
    print(f"  {C.GREEN}{'━' * w}{C.RESET}")
    print()

# ── Main ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import django
    django.setup()

    from django.core.wsgi import get_wsgi_application
    from django.core.management import call_command
    from waitress import serve

    application = get_wsgi_application()

    # ── Startup checks ───────────────────────────────────────────────────
    info("Running startup checks...")

    # 1) Django system checks
    try:
        call_command('check', '--deploy', verbosity=0)
        ok("Django system checks passed")
    except SystemCheckError as e:
        warn(f"System check warnings (non-fatal): {e}")
    except Exception as e:
        warn(f"System check skipped: {e}")

    # 2) Pending migrations check
    try:
        from io import StringIO
        out = StringIO()
        call_command('showmigrations', '--plan', verbosity=1, stdout=out)
        plan_output = out.getvalue()
        if '[ ]' in plan_output:
            pending = [l.strip() for l in plan_output.splitlines() if '[ ]' in l]
            err(f"{len(pending)} unapplied migration(s) found!")
            info("Run: python manage.py migrate")
            resp = input("  Continue anyway? [y/N] ").strip().lower()
            if resp != 'y':
                sys.exit(1)
        else:
            ok("All migrations applied")
    except Exception as e:
        warn(f"Migration check skipped: {e}")

    # 3) Static files check
    static_root = os.path.join(PROJECT_ROOT, 'staticfiles')
    if not os.path.isdir(static_root) or not os.listdir(static_root):
        warn("staticfiles/ is empty or missing")
        info("Run: python manage.py collectstatic --noinput")
        try:
            call_command('collectstatic', '--noinput', verbosity=0)
            ok("Static files collected")
        except Exception as e:
            warn(f"collectstatic failed: {e}")

    # Check port availability first
    if not _check_port_available(WAITRESS_PORT):
        sys.exit(1)

    print_banner(WAITRESS_HOST, WAITRESS_PORT, WAITRESS_THREADS)

    # Start services
    info("Starting services...")
    redis_proc = start_redis()
    celery_procs = start_celery_workers()
    _children = [redis_proc] + celery_procs

    # Filter out None (skipped services)
    _children = [p for p in _children if p is not None]

    print()
    ok("All services running. Press CTRL+C to stop.")
    print()

    # Handle shutdown
    def _signal_handler(sig, frame):
        print()
        info("Shutting down...")
        _shutdown_all(_children)
        ok("Server stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        serve(
            application,
            host=WAITRESS_HOST,
            port=WAITRESS_PORT,
            threads=WAITRESS_THREADS,
            channel_timeout=1200,
            cleanup_interval=30,
            max_request_body_size=10 * 1024 * 1024,
            recv_bytes=131072,
        )
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_all(_children)
