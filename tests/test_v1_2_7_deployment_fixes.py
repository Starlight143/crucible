"""
v1.2.7 — deployment_artifacts correctness fixes.

Two HIGH-severity bugs found by an end-to-end SaaS idea-mode run
(`20260703_173342_..._personal_saas_subscription_tracker`):

1. **Health-probe path mismatch.** Every generated probe hardcoded `/health`,
   but Crucible apps serve `/healthz` (codegen + runtime-smoke convention), so
   Docker HEALTHCHECK / k8s readiness+liveness / helm probes hit 404 → the
   container/pod is unhealthy forever. Fixed: `_detect_health_path` scans the
   generated code for the real health route (default `/healthz`) and every
   probe uses it.
2. **Build-context mismatch.** compose set `context: .` (the `deployment/` dir)
   while the Dockerfile did `COPY requirements.txt` / `COPY . .` / `CMD ... main:app`
   — but the app code and requirements live in `code/` + the run root, not in
   `deployment/`. `docker build` failed / the app wasn't importable. Fixed:
   compose builds from the run root (`context: ..`,
   `dockerfile: deployment/Dockerfile`) and the Dockerfile does `COPY code/ ./`
   (requirements.txt is read from the run-root mirror written by section_07).
"""
from __future__ import annotations

import os
import tempfile

from crucible.features.deployment_artifacts import (
    _detect_health_path,
    _generate_docker_compose,
    _generate_dockerfile,
    _generate_helm_values,
    _generate_k8s_deployment,
    generate_deployment_artifacts,
)

_FASTAPI_HEALTHZ = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
    "\n"
    "@app.get('/healthz')\n"
    "def healthz():\n"
    "    return {'status': 'ok'}\n"
)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── _detect_health_path ───────────────────────────────────────────────────────

def test_detect_health_path_reads_healthz_route() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write(os.path.join(td, "main.py"), _FASTAPI_HEALTHZ)
        assert _detect_health_path(td) == "/healthz"


def test_detect_health_path_reads_plain_health_route() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write(os.path.join(td, "app.py"), "import flask\napp=flask.Flask(__name__)\n@app.route('/health')\ndef h(): return 'ok'\n")
        assert _detect_health_path(td) == "/health"


def test_detect_health_path_prefers_healthz_over_health() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write(os.path.join(td, "main.py"), "P='/health'\nQ='/healthz'\n")
        assert _detect_health_path(td) == "/healthz"


def test_detect_health_path_defaults_to_healthz() -> None:
    with tempfile.TemporaryDirectory() as td:
        _write(os.path.join(td, "main.py"), "x = 1\n")
        assert _detect_health_path(td) == "/healthz"
    # missing dir never raises
    assert _detect_health_path(os.path.join(tempfile.gettempdir(), "nope_xyz")) == "/healthz"


# ── generators honour the detected path ───────────────────────────────────────

def test_dockerfile_healthcheck_uses_health_path() -> None:
    df = _generate_dockerfile("fastapi", "3.11", port="8000", health_path="/healthz")
    assert "http://localhost:8000/healthz" in df
    assert "/health 2>/dev/null" not in df  # old hardcode gone


def test_k8s_probes_use_health_path() -> None:
    y = _generate_k8s_deployment("app", "fastapi", "3.11", port="8000", health_path="/healthz")
    assert y.count("path: /healthz") == 2          # readiness + liveness
    assert "path: /health\n" not in y


def test_helm_values_use_health_path() -> None:
    y = _generate_helm_values("app", "fastapi", port="8000", health_path="/healthz")
    assert "path: /healthz" in y
    assert "path: /health\n" not in y


# ── build context (HIGH-2) ────────────────────────────────────────────────────

def test_compose_builds_from_run_root() -> None:
    c = _generate_docker_compose("fastapi", True, "demo", port="8000")
    assert "context: .." in c
    assert "dockerfile: deployment/Dockerfile" in c


def test_dockerfile_copies_code_dir_not_dot() -> None:
    df = _generate_dockerfile("fastapi", "3.11", port="8000")
    assert "COPY code/ ./" in df
    assert "COPY . ." not in df
    assert "COPY requirements.txt ./" in df  # resolved from run-root mirror


# ── end-to-end: written artifacts are internally consistent ───────────────────

def test_generate_deployment_artifacts_end_to_end_consistent() -> None:
    """The full generator, given a /healthz app, must emit probes on /healthz and
    a run-root build context across Dockerfile, compose, k8s, and helm."""
    with tempfile.TemporaryDirectory() as run_dir:
        _write(os.path.join(run_dir, "code", "main.py"), _FASTAPI_HEALTHZ)
        report = generate_deployment_artifacts(run_dir)
        assert report.success

        dep = os.path.join(run_dir, "deployment")
        dockerfile = open(os.path.join(dep, "Dockerfile"), encoding="utf-8").read()
        compose = open(os.path.join(dep, "docker-compose.yml"), encoding="utf-8").read()
        k8s = open(os.path.join(dep, "k8s", "deployment.yaml"), encoding="utf-8").read()
        helm_values = open(os.path.join(dep, "helm", "values.yaml"), encoding="utf-8").read()

        assert "/healthz" in dockerfile and "/health 2>/dev/null" not in dockerfile
        assert "COPY code/ ./" in dockerfile and "COPY . ." not in dockerfile
        assert "context: .." in compose and "dockerfile: deployment/Dockerfile" in compose
        assert "path: /healthz" in k8s and "path: /health\n" not in k8s
        assert "path: /healthz" in helm_values
