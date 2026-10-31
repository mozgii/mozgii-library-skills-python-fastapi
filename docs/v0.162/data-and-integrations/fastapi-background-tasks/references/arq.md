---
title: arq in an existing FastAPI app
author: MOZGII, Inc.
lang: en
---

# arq in an existing FastAPI app

This reference keeps the arq code for apps that already run it. arq 0.28 is in maintenance-only mode, as its README
says (issue #510 in its repository), so start new projects on Taskiq instead.

## Contents

This reference on arq in an existing FastAPI app has these sections:

- Enqueue a job from an endpoint
- The arq worker

## Enqueue a job from an endpoint

An endpoint enqueues with arq through one Redis pool opened in the lifespan. A job id that repeats is refused, so the
same report cannot be queued twice.

```python
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[dict[str, ArqRedis]]:
    redis = await create_pool(RedisSettings(host="redis"))
    yield {"arq": redis}
    await redis.aclose()


@router.post("/reports", status_code=status.HTTP_202_ACCEPTED)
async def create_report(body: ReportRequest, request: Request) -> JobAccepted:
    job = await request.state.arq.enqueue_job(
        "build_report", body.model_dump(mode="json"), _job_id=f"report:{body.month}"   # same id → not queued twice
    )
    if job is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Report already queued")
    return JobAccepted(job_id=job.job_id, status_url=f"/reports/jobs/{job.job_id}")
```

## The arq worker

The worker is a separate process that imports its functions from a settings class; retries and the job timeout live
there, and `cron_jobs` on the same class runs schedules.

```python
# worker.py, started with: uv run arq worker.WorkerSettings
async def build_report(ctx: dict[str, object], params: dict[str, object]) -> str:
    ...
    return "s3://reports/2026-10.pdf"


class WorkerSettings:
    functions = [build_report]
    redis_settings = RedisSettings(host="redis")
    max_tries = 5
    job_timeout = 600
```
