from __future__ import annotations
"""Celery distributed worker artifact generator for Crucible.

The feature creates runnable Celery worker files, Docker Compose services, and
setup documentation in the workspace around a run directory. Existing
``celery_app.py`` is preserved to avoid overwriting local configuration.
"""

import json
import os
import time
from typing import Any, Dict

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


CELERY_APP_SCRIPT = r'''from __future__ import annotations
"""Celery application factory for Crucible distributed pipeline runs."""

import os

try:
    from celery import Celery  # type: ignore
except ImportError as exc:
    raise RuntimeError("Celery is required: pip install celery redis") from exc


broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

app = Celery("quantsaas", broker=broker_url, backend=result_backend, include=["celery_task"])
app.conf.update(
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
'''


CELERY_TASK_SCRIPT = r'''from __future__ import annotations
"""Celery tasks for launching Crucible pipeline subprocesses."""

import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from celery_app import app


@app.task(bind=True, name="quantsaas.run_pipeline")
def run_pipeline(self: Any, project_path: str, extra_args: Optional[List[str]] = None) -> Dict[str, Any]:
    if not project_path:
        raise ValueError("project_path is required")
    workspace = os.path.dirname(os.path.abspath(__file__))
    runner = os.path.join(workspace, "run_crucible_enhanced.py")
    if not os.path.isfile(runner):
        raise FileNotFoundError(f"runner not found: {runner}")
    command = [sys.executable, runner, "run", "--project-dir", project_path]
    if extra_args:
        command.extend(str(item) for item in extra_args)
    _task_timeout = max(60, int(os.environ.get("CELERY_TASK_TIMEOUT_SECONDS", "3600")))
    process = subprocess.run(command, cwd=workspace, env=dict(os.environ), text=True, capture_output=True, timeout=_task_timeout)
    return {"task_id": self.request.id, "returncode": process.returncode, "stdout": process.stdout[-4000:], "stderr": process.stderr[-4000:], "command": command}
'''


def _docker_compose(concurrency: str) -> str:
    return f'''services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    ports:
      - "6379:6379"
    volumes:
      - celery-redis-data:/data
  celery-worker:
    image: python:3.11-slim
    working_dir: /workspace
    command: >
      sh -c "pip install --no-cache-dir -r requirements.txt celery redis &&
             celery -A celery_app worker --loglevel=INFO --concurrency={concurrency}"
    environment:
      CELERY_BROKER_URL: redis://redis:6379/0
      CELERY_RESULT_BACKEND: redis://redis:6379/1
    volumes:
      - .:/workspace
    depends_on:
      - redis
  flower:
    image: python:3.11-slim
    working_dir: /workspace
    command: >
      sh -c "pip install --no-cache-dir celery redis flower &&
             celery -A celery_app flower --port=5555 --broker=redis://redis:6379/0"
    environment:
      CELERY_BROKER_URL: redis://redis:6379/0
      CELERY_RESULT_BACKEND: redis://redis:6379/1
    ports:
      - "5555:5555"
    volumes:
      - .:/workspace
    depends_on:
      - redis
volumes:
  celery-redis-data:
'''


def _setup_md() -> str:
    return """# Crucible Celery Worker Setup

Start the local worker stack:

```bash
docker compose -f docker-compose.celery.yml up --build
```

Submit a task from Python:

```python
from celery_task import run_pipeline
task = run_pipeline.delay("/absolute/path/to/project", [])
print(task.id)
```

Flower is exposed at http://localhost:5555 when the compose stack is running.
"""


def _write_text(path: str, content: str, *, overwrite: bool = True) -> bool:
    try:
        if not overwrite and os.path.exists(path):
            return False
        _tmp = path + ".tmp"
        try:
            with open(_tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(_tmp, path)
        except OSError:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


@register("celery_worker")
class CeleryWorkerFeature(BaseFeature):
    name = "celery_worker"
    label = "Celery Distributed Worker"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("CELERY_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        try:
            workspace_root = os.path.dirname(os.path.abspath(run_dir))
            concurrency = os.environ.get("CELERY_WORKER_CONCURRENCY", "4").strip() or "4"
            celery_app_path = os.path.join(workspace_root, "celery_app.py")
            celery_task_path = os.path.join(workspace_root, "celery_task.py")
            compose_path = os.path.join(workspace_root, "docker-compose.celery.yml")
            setup_path = os.path.join(run_dir, "celery_setup.md")
            config_path = os.path.join(run_dir, "worker_config.json")
            app_written = _write_text(celery_app_path, CELERY_APP_SCRIPT, overwrite=False)
            _write_text(celery_task_path, CELERY_TASK_SCRIPT)
            _write_text(compose_path, _docker_compose(concurrency))
            _write_text(setup_path, _setup_md())
            payload = {
                "broker_url": os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
                "result_backend": os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
                "worker_concurrency": concurrency,
                "workspace_root": workspace_root,
                "celery_app_written": app_written,
                "celery_app_path": celery_app_path,
                "celery_task_path": celery_task_path,
                "docker_compose_path": compose_path,
            }
            _write_json(config_path, payload)
            return FeatureResult(feature=self.name, success=True, summary="Celery worker artifacts generated", details=payload, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Celery worker generation failed", error=str(exc), duration_seconds=time.monotonic() - start)
