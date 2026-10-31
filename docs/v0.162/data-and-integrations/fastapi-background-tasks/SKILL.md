---
name: fastapi-background-tasks
description: Runs work outside the request in FastAPI - BackgroundTasks for small fire-and-forget jobs after the response, a task queue (Taskiq, Celery, a PostgreSQL queue, or arq in apps already on it) for work that must not be lost or needs retries, the 202 Accepted plus job status pattern, periodic jobs started from lifespan, and offloading blocking or CPU-heavy code to threads and processes. Use when an endpoint is slow because of email, file processing, external APIs or reports, when choosing a job queue, or when scheduling recurring work.
metadata:
  tags:
  - fastapi
  - background-tasks
  - task-queue
  - arq
  - taskiq
  - celery
  - scheduling
  - threads
---

# FastAPI background tasks and job queues

This skill moves slow work out of the request so endpoints answer fast, and picks the right tool by one question:
what happens if the work is lost?

## Step 0: installed versions for background work

Read what is installed before choosing a queue. A missing line means the package is not installed.

```bash
uv pip list | grep -iE '^(fastapi|taskiq|taskiq-redis|celery|arq|redis) '
```

`taskiq-redis` 1.2 needs the `redis` client 8 and `taskiq` 0.13. An app already on arq can stay on it; a new one starts on Taskiq.

## Choose the tool

The choice depends on how long the work takes and whether losing it is acceptable.

| Work | Tool |
|---|---|
| under a few seconds, losing it on a crash is acceptable (a welcome email, a cache warm-up, an audit ping) | `BackgroundTasks` |
| must run even after a crash or deploy, needs retries, takes minutes, or must be spread across machines | a task queue: Taskiq, Celery or a PostgreSQL-backed queue |
| runs on a schedule | a scheduler process (the queue's cron feature, a Kubernetes `CronJob`), not every web worker |
| blocking library call inside a request | `asyncio.to_thread` or a `def` endpoint |
| CPU-heavy work (image resize, PDF, ML inference) | a process pool for short jobs, a queue worker for long ones |

## BackgroundTasks for small jobs

`BackgroundTasks` runs functions after the response has been sent, in the same process. Add tasks from the endpoint
or from any dependency.

```python
from fastapi import BackgroundTasks


async def send_welcome_email(email: str) -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        await http.post("https://mail.example.com/send", json={"to": email, "template": "welcome"})


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: UserCreate, background: BackgroundTasks, session: SessionDep) -> UserRead:
    user = await users_service.create(session, body)
    background.add_task(send_welcome_email, user.email)
    return UserRead.model_validate(user)
```

Facts verified on FastAPI 0.162:

- Tasks run in the order added, once the client has the response and before request-scoped `yield` dependencies clean up.
- A `def` task runs in the thread pool; an `async def` task runs on the event loop and must not block.
- If a task raises, the client already has its 200, the remaining tasks of that request are skipped, and the
  exception goes only to the server log. Wrap each task body in `try/except` with logging.
- The worker holds the connection open until tasks finish; a long task delays graceful shutdown and is killed on
  deploy.
- Pass ids and plain values to tasks, and open a new database session inside the task, so its transaction is
  separate from the request's.

```python
async def recalculate_totals(order_id: int) -> None:
    try:
        async with SessionFactory() as session:
            await orders_service.recalculate(session, order_id)
    except Exception:
        logger.exception("recalculate_totals failed", extra={"order_id": order_id})
```

## Task queues for work that must not be lost

Welcome emails and other background tasks get lost whenever you deploy or restart, because `BackgroundTasks` runs
inside the web process that is being replaced; work that must not be lost belongs in a job queue. A task queue stores
jobs in a broker (Redis, RabbitMQ, PostgreSQL) and runs them in separate worker processes, with retries and
visibility. The FastAPI app only enqueues.

| Queue | Fits | Notes |
|---|---|---|
| Taskiq 0.13 | new async apps | async-native, FastAPI integration package, Redis/RabbitMQ/NATS brokers |
| Celery 5.6 | large or mixed sync codebases | mature, many features; tasks are sync functions |
| PostgreSQL queue (`SELECT ... FOR UPDATE SKIP LOCKED`, or a library such as Procrastinate) | apps that want no extra broker | the job is enqueued in the same transaction as the data change |
| arq 0.28 | apps that already run it | small and async-native, with retries and cron jobs built in, but its README marks it maintenance-only, so start new projects on Taskiq. Its enqueue and worker code is in [references/arq.md](references/arq.md) |

Queue a job with Taskiq on Redis (`uv add taskiq taskiq-redis`). The broker lives in its own module, so the web app
and the worker import the same object:

```python
# app/tasks.py, the worker starts with: uv run taskiq worker app.tasks:broker
from taskiq import SmartRetryMiddleware, TaskiqDepends
from taskiq.depends.progress_tracker import ProgressTracker, TaskState
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

REDIS_URL = "redis://redis:6379/0"

broker = (
    ListQueueBroker(REDIS_URL)
    .with_result_backend(RedisAsyncResultBackend(REDIS_URL, result_ex_time=86_400))
    .with_middlewares(SmartRetryMiddleware(default_retry_count=5, default_delay=10, use_jitter=True, use_delay_exponent=True))
)


@broker.task(task_name="build_report", retry_on_error=True, max_retries=5, timeout=600)
async def build_report(params: dict[str, object], progress: ProgressTracker[str] = TaskiqDepends()) -> str:
    await progress.set_progress(TaskState.STARTED)
    ...
    return "s3://reports/2026-10.pdf"
```

The web app opens the broker in the lifespan and kicks the task from the endpoint:

```python
from uuid import uuid4

from taskiq.depends.progress_tracker import TaskProgress

from app.tasks import broker, build_report


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    await broker.startup()
    yield
    await broker.shutdown()


@router.post("/reports", status_code=status.HTTP_202_ACCEPTED)
async def create_report(body: ReportRequest) -> JobAccepted:
    job_id = uuid4().hex
    await broker.result_backend.set_progress(job_id, TaskProgress(state="QUEUED", meta=None))
    await build_report.kicker().with_task_id(job_id).kiq(body.model_dump(mode="json"))
    return JobAccepted(job_id=job_id, status_url=f"/reports/jobs/{job_id}")
```

Taskiq does not refuse a repeated job, so guard a job that must run once with a unique row in PostgreSQL before the
`kiq()` call. Rules for queued jobs: make them idempotent (a retry must not charge twice), pass ids rather than objects, set a
timeout, and record the result where the API can read it.

## 202 Accepted and a job status endpoint

Long work returns `202 Accepted` with a job id at once; the client polls a status endpoint or listens on SSE or a
WebSocket for completion. With Taskiq, the status endpoint reads the result backend. The `QUEUED` mark written before
`kiq()` and the worker's `STARTED` over it let the endpoint tell `queued`, `running` and an id nobody issued (404).

```python
from typing import Literal, cast


class JobAccepted(BaseModel):
    job_id: str
    status_url: str


class JobStatus(BaseModel):
    job_id: str
    state: Literal["queued", "running", "done", "failed"]
    result_url: str | None = None
    error: str | None = None


@router.get("/reports/jobs/{job_id}")
async def report_status(job_id: str) -> JobStatus:
    backend = broker.result_backend
    if await backend.is_result_ready(job_id):
        result = await backend.get_result(job_id)
        if result.is_err:
            return JobStatus(job_id=job_id, state="failed", error="Report failed")
        return JobStatus(job_id=job_id, state="done", result_url=cast(str, result.return_value))
    progress = await backend.get_progress(job_id)
    if progress is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return JobStatus(job_id=job_id, state="running" if progress.state == TaskState.STARTED else "queued")
```

For progress updates without polling, stream them with Server-Sent Events (fastapi-websockets-streaming).

## Periodic jobs and lifespan task groups

A loop started in the lifespan runs once per worker process: with four workers it runs four times. Use it only for
per-process work (refreshing an in-memory cache); run global schedules in one scheduler process.

```python
import asyncio


async def refresh_rates(app_state: dict[str, object]) -> None:
    while True:
        try:
            app_state["rates"] = await fetch_rates()
        except Exception:
            logger.exception("rate refresh failed")
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[dict[str, object]]:
    state: dict[str, object] = {"rates": await fetch_rates()}
    task = asyncio.create_task(refresh_rates(state))
    yield state
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

Keep a reference to every task you create (`asyncio.create_task` holds only a weak one) and cancel it on shutdown.
For global schedules use Taskiq's scheduler, Celery beat, arq's `cron_jobs` or a Kubernetes `CronJob`, or take a
PostgreSQL advisory lock (`pg_try_advisory_lock`) so only one worker runs the job.

## Blocking and CPU-heavy code

Blocking I/O goes to a thread; CPU-heavy work goes to a process, because threads share the GIL on standard CPython
builds.

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

pool = ProcessPoolExecutor(max_workers=2)      # create in lifespan, shut down on exit


@router.post("/thumbnails")
async def thumbnail(file: UploadFile) -> ThumbnailRead:
    data = await file.read()
    resized = await asyncio.get_running_loop().run_in_executor(pool, make_thumbnail, data)   # CPU work
    url = await asyncio.to_thread(upload_to_storage, resized)                                  # blocking SDK
    return ThumbnailRead(url=url)
```

The function passed to a process pool must be importable at module level and its arguments picklable. For work over
a few seconds, use a queue worker instead so web workers stay free.

## Verify background work

Prove the three things that fail silently: a task error reaches the log, a job survives a worker restart, and the
status endpoint tells the truth.

```bash
uv run pytest -k "background or job"
uv run taskiq worker app.tasks:broker &                         # or the worker of the queue you chose
curl -s -X POST http://127.0.0.1:8000/reports -H 'Content-Type: application/json' -d '{"month":"2026-10"}'
# expected: 202 with job_id and status_url; poll status_url until state is "done"
```

Stop the worker before posting a job, post it, then start the worker: the job must still run. With `BackgroundTasks`
it would be lost. Also post a job that raises, and confirm the status says `failed` after the last retry.

```python
async def test_report_is_queued_and_answered(client: httpx.AsyncClient) -> None:
    response = await client.post("/reports", json={"month": "2026-10"})
    assert response.status_code == 202
    status = (await client.get(response.json()["status_url"])).json()
    assert status["state"] in {"queued", "running", "done"}
    assert (await client.get("/reports/jobs/never-issued")).status_code == 404
```

In tests, replace the broker with Taskiq's `InMemoryBroker(await_inplace=True)`, so no Redis is needed and each task
finishes before `kiq()` returns.

## Background job anti-patterns

Each of these loses work, or does it twice, with no error anyone sees.

| Do not | Do instead |
|---|---|
| `BackgroundTasks` for work that must not be lost | a task queue |
| pass ORM objects or a request session to a task | pass ids and open a new session |
| a schedule in the lifespan of every worker | one scheduler process, or an advisory lock |
| a job that charges on every retry | make it idempotent |
| `asyncio.create_task` with no stored reference | keep it and cancel it on shutdown |
| enqueue before `commit()` | enqueue after it, or use a PostgreSQL queue |
| a new project on arq | Taskiq, since arq is in maintenance-only mode |

## Background job checklist

Go through this background task and job queue checklist before merging a change.

```text
[ ] the tool matches the loss tolerance: BackgroundTasks only for work that may be lost
[ ] each background task catches and logs its own exceptions
[ ] tasks receive ids and open their own database session
[ ] queued jobs are idempotent, have timeouts and retry limits
[ ] long work answers 202 with a job id and a status endpoint
[ ] periodic jobs run once globally (scheduler, cron, advisory lock), not once per worker
[ ] every create_task is stored and cancelled on shutdown
[ ] blocking calls in threads, CPU work in processes or workers
```

## Common background task and job queue problems

These are the background task and job queue problems people hit most, each with its symptom, cause and fix.

1. **Emails vanish after deploys.** `BackgroundTasks` died with the old worker. Move must-deliver work to a queue.
2. **One failing task silently skips the next ones.** Tasks of a request run in sequence; an exception ends the
   sequence. Catch inside each task.
3. **A job runs four times every hour.** The scheduler loop lives in the lifespan of four workers. Run it in one
   process.
4. **`Task was destroyed but it is pending!`** A task created with `asyncio.create_task` and no stored reference was
   garbage-collected. Keep references.
5. **The worker cannot find the row it was given.** The job was enqueued before the request's transaction committed,
   and the worker ran first. Enqueue after `commit()`, or use a PostgreSQL queue that inserts the job in the same
   transaction as the data.
6. **`RuntimeWarning: coroutine '...' was never awaited` in a Celery worker, and the task reports success with no work
   done.** Celery tasks are sync, so calling one of the app's async services only creates the coroutine. Run it with
   `asyncio.run()` in the task, or use an async-native queue such as Taskiq.

## See also

- fastapi-external-apis: retries and webhooks that end in a queue.
- fastapi-websockets-streaming: progress over SSE.
- fastapi-sqlalchemy-async: sessions and `SKIP LOCKED`.
- fastapi-deployment: graceful shutdown.
- fastapi-performance: threads, processes and blocking calls.
