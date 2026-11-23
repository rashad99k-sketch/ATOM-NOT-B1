# Render startup and runtime supervision

## Why this exists

The Flask dashboard can be imported by a WSGI server without executing
`ggpbot.py`'s `if __name__ == "__main__"` block. In that deployment shape the
HTTP dashboard may load while the trading supervisor never starts.

## Required Render web service commands

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn wsgi:app --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile -`
- Recommended health-check path: `/runtime-health`

The committed `Procfile` provides the same start command when Render uses the
repository default. If the service has an explicit Start Command in its Render
settings, update that setting to the command above; a Procfile does not override
an explicit dashboard setting.

## Runtime behavior

- `wsgi.py` imports the Flask app and starts `safe_main_loop` in one dedicated
  supervisor thread.
- The OS-backed `data/ggpbot.instance.lock` is retained for the lifetime of the
  supervisor. A second process cannot start a duplicate trading engine.
- The main loop publishes a heartbeat on every cycle.
- `/health` continues to report HTTP/dashboard health and includes
  `trading_runtime` details.
- `/runtime-health` reports STARTING/RUNNING/STALE/FAILED/LOCKED states. A
  missing or stale supervisor is not reported as runtime-ready.
- Gunicorn uses one worker because a trading engine must have one process owner;
  request concurrency is provided by its eight HTTP threads.
- Keep the Render service at exactly one instance. The OS file lock coordinates
  processes on the same host; it is not a distributed lock across separate
  Render instances with separate filesystems.

## Safety

The runtime bootstrap does not change PAPER/LIVE configuration. Existing
fail-safe mode resolution remains in force: LIVE still requires the project's
explicit LIVE switches and valid credentials. No exchange order is sent by
importing the WSGI module or by the health endpoint itself.
