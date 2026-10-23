"""Is the glitchtip-worker CONSUMING, or merely running?

`glitchtip-worker` runs `./bin/run-worker.sh`, which execs
`manage.py runworker --scheduler`: a *scheduler* half and a *consumer* half
inside one container. `django_vtasks`'s `runworker` command starts both as
bare asyncio tasks and then awaits a stop event it only ever receives from a
signal -- it never awaits either task -- so a consumer that raises or hangs
takes nothing down with it. The process stays up, the scheduler keeps
enqueuing, and no event ever becomes a GlitchTip issue again (#4208).

Nothing the container reports can see that state, which is the whole defect:

* Kubernetes said `1/1 Running` with 0 restarts through 400 enqueued-and-never
  -processed tasks in CI run 39679850907, because the worker Deployment
  declared no probe at all;
* the Compose twin's probe -- the mtime of `VTASKS_HEALTH_CHECK_FILE` -- was
  measured GREEN in a reproduction of exactly this state. `Worker.run()` has
  not returned, so `self.running` is still true and `_heartbeat_loop` keeps
  rewriting the file on schedule. A heartbeat proves the event loop turns; it
  cannot prove the consumer consumes.

So this probe asks the only process that knows -- the consumer itself -- for
evidence of work, via the Prometheus metrics `runworker` exposes on
`VTASKS_METRICS_PORT`. Two questions, because they fail at different times:

1. **Has the consumer loop ever run?** `Worker.consume_queue` sets the
   `vtasks_queue_depth` gauge within 15s of its first iteration and every 15s
   after. A served metrics page with no `vtasks_queue_depth` sample means no
   consumer iteration has ever happened -- the start-up wedge, caught on the
   first probe rather than after a timeout. Measured: a healthy worker
   publishes the gauge; the wedged one serves the page with the gauge absent.
2. **Is it still making progress?** `vtasks_tasks_processed_total` is
   incremented only by `process_task`/`process_batch`, i.e. only by the
   consumer. The scheduler enqueues `send-alert-notifications` every
   `ALERT_NOTIFICATION_INTERVAL` (60s by default) and, with uptime checks on,
   `uptime-dispatch-checks` every second -- so on this workload there is
   always work, and a total that has not moved in `--max-stall-seconds` means
   the consumer stopped, not that the stack is idle. The last total and when
   it last changed are kept in `--state-file`, which lives in the container's
   own filesystem and so resets with the container.

Exits 0 when the consumer is working, 1 with the reason on stderr when it is
not. Shared verbatim by both substrates -- the Kubernetes probes mount it from
a ConfigMap kustomize generates out of this file, and the Compose healthcheck
bind-mounts the same path -- so the two cannot drift into disagreeing about
what "the worker is healthy" means.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

# Incremented only by Worker.process_task / Worker.process_batch.
PROCESSED_METRIC = "vtasks_tasks_processed_total"
# Set only by Worker.consume_queue, on its first iteration and every 15s after.
QUEUE_DEPTH_METRIC = "vtasks_queue_depth"


def _sum_samples(metrics_page: str, metric: str) -> tuple[float, int]:
    """Total and sample count for ``metric`` in a Prometheus text exposition.

    Matches the metric name only where a label block or the value follows it,
    so ``vtasks_tasks_processed_total`` does not also pick up the
    ``vtasks_tasks_processed_created`` gauge prometheus_client emits beside
    every counter (whose value is a unix timestamp and would swamp the total).
    """
    total = 0.0
    samples = 0
    for line in metrics_page.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        rest = line[len(metric) :]
        if rest[:1] not in ("{", " "):
            continue
        value = rest.rsplit(" ", 1)[-1]
        try:
            total += float(value)
        except ValueError:
            continue
        samples += 1
    return total, samples


def _read_state(path: str) -> tuple[float, float] | None:
    """The last observed processed-total and when it last CHANGED."""
    try:
        with open(path) as handle:
            processed, changed_at = handle.read().split()
        return float(processed), float(changed_at)
    except (OSError, ValueError):
        return None


def _write_state(path: str, processed: float, changed_at: float) -> None:
    try:
        with open(path, "w") as handle:
            handle.write(f"{processed} {changed_at}")
    except OSError as exc:
        # A probe that cannot persist its own state must not report the worker
        # unhealthy for it -- say so and let the caller's verdict stand.
        print(f"warning: could not write {path}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("VTASKS_METRICS_PORT") or 0),
        help="port runworker serves its Prometheus metrics on " "(default: $VTASKS_METRICS_PORT)",
    )
    parser.add_argument(
        "--max-stall-seconds",
        type=float,
        default=180.0,
        help="fail once the processed-task total has not moved for this long. "
        "Must exceed the slowest thing the scheduler enqueues (60s by "
        "default) by a clear margin, or a healthy-but-quiet worker reads as "
        "stalled.",
    )
    parser.add_argument(
        "--state-file",
        default="/tmp/nyxgpt-glitchtip-worker-probe.state",  # noqa: S108
        help="where to remember the last processed-task total and when it " "last changed",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    if not args.metrics_port:
        # Refuse rather than pass: a probe with nothing to read is the blind
        # probe this file exists to replace, and silently exiting 0 would
        # reinstate it. The manifest and the Compose service both set
        # VTASKS_METRICS_PORT; if it is missing, that wiring broke.
        print(
            "VTASKS_METRICS_PORT is unset and --metrics-port was not given, so "
            "there is no way to ask the worker whether it is consuming",
            file=sys.stderr,
        )
        return 1

    url = f"http://127.0.0.1:{args.metrics_port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=args.timeout) as response:  # noqa: S310
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"the worker is not serving metrics on {url}: {exc}", file=sys.stderr)
        return 1

    _, depth_samples = _sum_samples(page, QUEUE_DEPTH_METRIC)
    if depth_samples == 0:
        print(
            f"the worker is serving metrics but has published no "
            f"{QUEUE_DEPTH_METRIC} sample, so its consumer loop has never run "
            f"an iteration -- the scheduler half may still be enqueuing work "
            f"nothing will ever process (#4208)",
            file=sys.stderr,
        )
        return 1

    processed, _ = _sum_samples(page, PROCESSED_METRIC)
    now = time.time()
    state = _read_state(args.state_file)
    if state is None or processed > state[0]:
        _write_state(args.state_file, processed, now)
        return 0

    stalled_for = now - state[1]
    if stalled_for < args.max_stall_seconds:
        return 0

    print(
        f"the worker's consumer has processed nothing for {stalled_for:.0f}s "
        f"({PROCESSED_METRIC} stuck at {processed:g}) while its scheduler keeps "
        f"enqueuing -- events are being accepted and never turned into issues "
        f"(#4208)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
