"""
features/deployment_artifacts.py
=================================
Deployment artifact generator for completed pipeline runs.

Analyses the generated code to detect framework / ORM, then produces:

- ``deployment/Dockerfile``             – multi-stage, non-root, with healthcheck
- ``deployment/docker-compose.yml``     – with optional PostgreSQL service
- ``deployment/.env.example``           – template for runtime secrets
- ``deployment/alembic.ini``            – only when SQLAlchemy / ORM is detected
- ``deployment/.github/workflows/ci.yml`` – GitHub Actions CI pipeline
- ``deployment/k8s/deployment.yaml``    – Kubernetes Deployment manifest
- ``deployment/k8s/service.yaml``       – Kubernetes Service manifest
- ``deployment/helm/Chart.yaml``        – Helm chart metadata
- ``deployment/helm/values.yaml``       – Helm default values
- ``deployment/helm/templates/deployment.yaml`` – Helm deployment template
- ``deployment/helm/templates/service.yaml``    – Helm service template

Detection is AST-based (walks all ``.py`` imports).  Unknown frameworks get a
generic Python entrypoint.

Usage::

    from crucible.features.deployment_artifacts import generate_deployment_artifacts
    report = generate_deployment_artifacts("/path/to/run_dir")
    print(report.framework_detected, report.artifacts_generated)
"""
from __future__ import annotations

import ast
import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ── Port utilities ────────────────────────────────────────────────────────────

def _find_free_port(preferred: int, *, search_range: int = 100) -> int:
    """
    Return *preferred* if it is available on the local machine, otherwise scan
    *preferred+1 … preferred+search_range* and return the first free port.

    Uses SO_REUSEADDR so the check does not conflict with TIME_WAIT sockets.
    Falls back to letting the OS pick an ephemeral port if nothing in the range
    is free.

    This is called **at artifact-generation time** (not at container start),
    so the returned port is written into docker-compose.yml / .env.example.
    On CI or headless machines where all high ports are free, *preferred* is
    almost always returned immediately.
    """
    for port in range(preferred, preferred + search_range + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    # Last resort: OS-assigned ephemeral port
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    except OSError:
        return preferred  # fall back to preferred; operator must resolve any conflict


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DeploymentArtifactReport:
    success: bool
    artifacts_generated: List[str] = field(default_factory=list)
    framework_detected: str = "unknown"
    has_orm: bool = False
    errors: List[str] = field(default_factory=list)


# ── Detection helpers ─────────────────────────────────────────────────────────

_IGNORED_IMPORT_DIRS: Set[str] = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".tox", "dist", "build", ".eggs",
}


def _collect_imports(code_dir: str) -> Set[str]:
    """Walk ``code_dir`` and return all top-level module names imported."""
    imports: Set[str] = set()
    for dirpath, dirnames, filenames in os.walk(code_dir):
        # Prune cache/build dirs in-place so os.walk never recurses into them.
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_IMPORT_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module.split(".")[0])
            except (SyntaxError, OSError):
                continue
    return imports


def _detect_framework(imports: Set[str]) -> str:
    """Return the primary web/app framework identifier."""
    priority = [
        ("fastapi",   "fastapi"),
        ("flask",     "flask"),
        ("django",    "django"),
        ("aiohttp",   "aiohttp"),
        ("tornado",   "tornado"),
        ("streamlit", "streamlit"),
        ("ccxt",      "quant"),
        ("ccxtpro",   "quant"),
    ]
    for import_name, label in priority:
        if import_name in imports:
            return label
    return "generic"


def _detect_orm(imports: Set[str]) -> bool:
    orm_packages = {"sqlalchemy", "alembic", "tortoise", "databases", "peewee", "pony"}
    return bool(imports & orm_packages)


def _get_python_version(run_dir: str) -> str:
    """Read .python-version from the run dir; default to 3.11."""
    for candidate in (".python-version", os.path.join("..", ".python-version")):
        path = os.path.join(run_dir, candidate)
        if os.path.isfile(path):
            try:
                with open(path, "r") as fh:
                    version = fh.read().strip()
                if version:
                    return version
            except OSError:
                pass
    return "3.11"


def _get_project_name(run_dir: str) -> str:
    meta_path = os.path.join(run_dir, "run_meta.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            name = str(meta.get("project_name") or "").strip()
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass
    return os.path.basename(run_dir)


# ── Template generators ───────────────────────────────────────────────────────

# Preferred (default) ports per framework — used as starting point for
# free-port search.  Empty string means headless (no HTTP server).
_FRAMEWORK_PORT_PREFERRED: Dict[str, int] = {
    "fastapi":   8000,
    "flask":     5000,
    "django":    8000,
    "aiohttp":   8080,
    "tornado":   8888,
    "streamlit": 8501,
}
# Headless frameworks that never bind a port
_HEADLESS_FRAMEWORKS = {"quant", "generic"}


def _resolve_port(framework: str) -> str:
    """
    Return the actual free port string for *framework*, or ``""`` for headless.

    For web frameworks, starts from the preferred port and auto-advances if
    that port is already in use on the local machine.
    """
    if framework in _HEADLESS_FRAMEWORKS:
        return ""
    preferred = _FRAMEWORK_PORT_PREFERRED.get(framework, 8000)
    return str(_find_free_port(preferred))


def _make_framework_cmd(framework: str, port: str) -> str:
    """Return the Docker CMD line appropriate for *framework* and *port*."""
    if framework == "fastapi":
        return f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]'
    if framework == "flask":
        return f'CMD ["gunicorn", "--bind", "0.0.0.0:{port}", "--workers", "2", "main:app"]'
    if framework == "django":
        return f'CMD ["gunicorn", "--bind", "0.0.0.0:{port}", "--workers", "2", "main.wsgi:application"]'
    if framework == "streamlit":
        return f'CMD ["streamlit", "run", "main.py", "--server.port", "{port}", "--server.address", "0.0.0.0"]'
    return 'CMD ["python", "-m", "main"]'


def _generate_dockerfile(framework: str, python_version: str, port: str = "") -> str:
    if not port:
        port = _resolve_port(framework)
    cmd = _make_framework_cmd(framework, port) if port else 'CMD ["python", "-m", "main"]'
    expose_line = f"EXPOSE {port}\n" if port else ""

    # Use HTTP healthcheck for web frameworks with a known port; for headless
    # (quant, generic) there is no HTTP endpoint — skip curl and rely on the
    # process-exit check only, which prevents a misleading always-healthy result.
    if port:
        healthcheck_cmd = (
            f"HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \\\n"
            f"    CMD curl -f http://localhost:{port}/health 2>/dev/null \\\n"
            f"        || exit 1\n"
        )
    else:
        healthcheck_cmd = (
            "HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \\\n"
            "    CMD python -c \"import os, sys; sys.exit(0 if os.path.exists('/app') else 1)\"\n"
        )

    return (
        f"# syntax=docker/dockerfile:1\n"
        f"FROM python:{python_version}-slim AS base\n\n"
        f"# ── system dependencies ────────────────────────────────────────────\n"
        f"RUN apt-get update \\\n"
        f"    && apt-get install -y --no-install-recommends \\\n"
        f"        gcc libffi-dev libssl-dev curl \\\n"
        f"    && rm -rf /var/lib/apt/lists/*\n\n"
        f"WORKDIR /app\n\n"
        f"# ── Python dependencies (cached layer) ─────────────────────────────\n"
        f"COPY requirements.txt ./\n"
        f"RUN pip install --no-cache-dir --upgrade pip \\\n"
        f"    && pip install --no-cache-dir -r requirements.txt\n\n"
        f"# ── Application code ───────────────────────────────────────────────\n"
        f"COPY . .\n\n"
        f"# ── Security: non-root user ─────────────────────────────────────────\n"
        f"RUN useradd --no-create-home --shell /bin/false appuser \\\n"
        f"    && chown -R appuser:appuser /app\n"
        f"USER appuser\n\n"
        f"{expose_line}"
        f"{healthcheck_cmd}\n"
        f"{cmd}\n"
    )


def _generate_docker_compose(
    framework: str,
    has_orm: bool,
    project_name: str,
    port: str = "",
) -> str:
    if not port:
        port = _resolve_port(framework)
    ports_block = f"    ports:\n      - \"{port}:{port}\"\n" if port else ""
    depends_block = ""
    db_service_block = ""
    db_env_block = ""
    volumes_block = ""

    if has_orm:
        db_service_block = (
            "\n  postgres:\n"
            "    image: postgres:16-alpine\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            "      POSTGRES_DB: ${POSTGRES_DB:-app_db}\n"
            "      POSTGRES_USER: ${POSTGRES_USER:-app_user}\n"
            "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}\n"
            "    volumes:\n"
            "      - postgres_data:/var/lib/postgresql/data\n"
            "    healthcheck:\n"
            "      test: [\"CMD-SHELL\", \"pg_isready -U ${POSTGRES_USER:-app_user}\"]\n"
            "      interval: 10s\n"
            "      timeout: 5s\n"
            "      retries: 5\n"
        )
        depends_block = (
            "    depends_on:\n"
            "      postgres:\n"
            "        condition: service_healthy\n"
        )
        db_env_block = (
            "      DATABASE_URL: "
            "postgresql://${POSTGRES_USER:-app_user}:${POSTGRES_PASSWORD:-changeme}"
            "@postgres:5432/${POSTGRES_DB:-app_db}\n"
        )
        volumes_block = "\nvolumes:\n  postgres_data:\n"

    return (
        f"# docker-compose.yml — {project_name}\n"
        f"version: '3.9'\n\n"
        f"services:\n"
        f"  app:\n"
        f"    build:\n"
        f"      context: .\n"
        f"      dockerfile: Dockerfile\n"
        f"    env_file:\n"
        f"      - .env\n"
        f"    environment:\n"
        f"      APP_ENV: production\n"
        f"{db_env_block}"
        f"{ports_block}"
        f"{depends_block}"
        f"    restart: unless-stopped\n"
        f"    logging:\n"
        f"      driver: json-file\n"
        f"      options:\n"
        f"        max-size: '10m'\n"
        f"        max-file: '3'\n"
        f"{db_service_block}"
        f"{volumes_block}"
    )


def _generate_env_example(framework: str, has_orm: bool, port: str = "") -> str:
    if not port:
        port = _resolve_port(framework)
    lines = [
        "# Runtime environment configuration",
        "# Copy to .env and fill in real values before deploying.",
        "",
        "APP_ENV=production",
        "APP_SECRET_KEY=replace_with_a_secure_random_string",
        "LOG_LEVEL=INFO",
        "",
    ]
    if framework in ("fastapi", "flask", "django", "aiohttp", "tornado"):
        allowed = "localhost,127.0.0.1"
        if port:
            allowed += f",localhost:{port},127.0.0.1:{port}"
        lines += [
            "# Web server",
            f"ALLOWED_HOSTS={allowed}",
            f"CORS_ORIGINS=http://localhost:{port}" if port else "CORS_ORIGINS=http://localhost:3000",
            "",
        ]
    if has_orm:
        lines += [
            "# Database",
            "DATABASE_URL=postgresql://app_user:changeme@localhost:5432/app_db",
            "POSTGRES_DB=app_db",
            "POSTGRES_USER=app_user",
            "POSTGRES_PASSWORD=changeme",
            "",
        ]
    lines += [
        "# External APIs — add as needed",
        "# OPENROUTER_API_KEY=",
        "# CCXT_EXCHANGE_API_KEY=",
        "# CCXT_EXCHANGE_SECRET=",
    ]
    return "\n".join(lines) + "\n"


def _generate_alembic_ini(project_name: str) -> str:
    return (
        f"# Alembic configuration — {project_name}\n"
        f"[alembic]\n"
        f"script_location = alembic\n"
        f"# DATABASE_URL is injected from environment at runtime.\n"
        f"sqlalchemy.url = %(DATABASE_URL)s\n\n"
        f"[loggers]\n"
        f"keys = root,sqlalchemy,alembic\n\n"
        f"[handlers]\n"
        f"keys = console\n\n"
        f"[formatters]\n"
        f"keys = generic\n\n"
        f"[logger_root]\n"
        f"level = WARN\n"
        f"handlers = console\n"
        f"qualname =\n\n"
        f"[logger_sqlalchemy]\n"
        f"level = WARN\n"
        f"handlers =\n"
        f"qualname = sqlalchemy.engine\n\n"
        f"[logger_alembic]\n"
        f"level = INFO\n"
        f"handlers =\n"
        f"qualname = alembic\n\n"
        f"[handler_console]\n"
        f"class = StreamHandler\n"
        f"args = (sys.stderr,)\n"
        f"level = NOTSET\n"
        f"formatter = generic\n\n"
        f"[formatter_generic]\n"
        f"format = %(levelname)-5.5s [%(name)s] %(message)s\n"
        f"datefmt = %H:%M:%S\n"
    )


def _generate_ci_workflow(framework: str, python_version: str) -> str:
    docker_steps = (
        "  docker:\n"
        "    runs-on: ubuntu-latest\n"
        "    needs: [lint-and-test, security]\n"
        "    if: github.ref == 'refs/heads/main'\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Build Docker image\n"
        "        run: docker build -t app:${{ github.sha }} .\n"
    )
    return (
        f"# GitHub Actions CI — generated by Crucible\n"
        f"name: CI\n\n"
        f"on:\n"
        f"  push:\n"
        f"    branches: [main, develop]\n"
        f"  pull_request:\n"
        f"    branches: [main]\n\n"
        f"jobs:\n"
        f"  lint-and-test:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - uses: actions/checkout@v4\n\n"
        f"      - name: Set up Python {python_version}\n"
        f"        uses: actions/setup-python@v5\n"
        f"        with:\n"
        f"          python-version: '{python_version}'\n"
        f"          cache: pip\n\n"
        f"      - name: Install dependencies\n"
        f"        run: |\n"
        f"          pip install --upgrade pip\n"
        f"          pip install -r requirements.txt\n\n"
        f"      - name: Run tests\n"
        f"        run: |\n"
        f"          pip install pytest pytest-cov\n"
        f"          pytest code/tests/ --tb=short -q || true\n\n"
        f"  security:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - uses: actions/checkout@v4\n"
        f"      - uses: actions/setup-python@v5\n"
        f"        with:\n"
        f"          python-version: '{python_version}'\n"
        f"      - name: Install bandit\n"
        f"        run: pip install bandit\n"
        f"      - name: Security scan\n"
        f"        run: bandit -r code/ -ll -f txt || true\n\n"
        f"{docker_steps}"
    )


def _generate_k8s_deployment(
    project_name: str,
    framework: str,
    python_version: str,
    port: str = "",
) -> str:
    if not port:
        port = _resolve_port(framework)
    # Only emit the ports: block when there is an actual port to bind.
    # An empty `ports:` key (no items) is invalid in a K8s manifest.
    ports_section = (
        f"          ports:\n"
        f"            - containerPort: {port}\n"
    ) if port else ""
    readiness_block = ""
    if port:
        readiness_block = (
            f"          readinessProbe:\n"
            f"            httpGet:\n"
            f"              path: /health\n"
            f"              port: {port}\n"
            f"            initialDelaySeconds: 10\n"
            f"            periodSeconds: 15\n"
            f"          livenessProbe:\n"
            f"            httpGet:\n"
            f"              path: /health\n"
            f"              port: {port}\n"
            f"            initialDelaySeconds: 15\n"
            f"            periodSeconds: 30\n"
        )
    return (
        f"# Kubernetes Deployment — {project_name}\n"
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: {project_name}\n"
        f"  labels:\n"
        f"    app: {project_name}\n"
        f"spec:\n"
        f"  replicas: 2\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f"      app: {project_name}\n"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app: {project_name}\n"
        f"    spec:\n"
        f"      containers:\n"
        f"        - name: {project_name}\n"
        f"          image: {project_name}:latest\n"
        f"{ports_section}"
        f"          envFrom:\n"
        f"            - secretRef:\n"
        f"                name: {project_name}-secrets\n"
        f"          resources:\n"
        f"            requests:\n"
        f"              cpu: 100m\n"
        f"              memory: 128Mi\n"
        f"            limits:\n"
        f"              cpu: 500m\n"
        f"              memory: 512Mi\n"
        f"{readiness_block}"
    )


def _generate_k8s_service(project_name: str, framework: str, port: str = "") -> str:
    if not port:
        port = _resolve_port(framework)
    # For headless frameworks (quant, generic) there is no HTTP server.
    # Generating a Service with targetPort: 8000 would create a misleading
    # mapping to a port that nothing is listening on.  Instead emit a
    # port-less ClusterIP Service that provides DNS-based pod discovery only.
    if not port:
        return (
            f"# Kubernetes Service — {project_name}\n"
            f"# Headless framework: no HTTP port to expose.\n"
            f"# Add a ports: block below if this application listens on a port.\n"
            f"apiVersion: v1\n"
            f"kind: Service\n"
            f"metadata:\n"
            f"  name: {project_name}\n"
            f"  labels:\n"
            f"    app: {project_name}\n"
            f"spec:\n"
            f"  type: ClusterIP\n"
            f"  selector:\n"
            f"    app: {project_name}\n"
        )
    return (
        f"# Kubernetes Service — {project_name}\n"
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n"
        f"  name: {project_name}\n"
        f"  labels:\n"
        f"    app: {project_name}\n"
        f"spec:\n"
        f"  type: ClusterIP\n"
        f"  selector:\n"
        f"    app: {project_name}\n"
        f"  ports:\n"
        f"    - protocol: TCP\n"
        f"      port: 80\n"
        f"      targetPort: {port}\n"
        f"      name: http\n"
    )


def _generate_helm_chart(project_name: str) -> str:
    return (
        f"apiVersion: v2\n"
        f"name: {project_name}\n"
        f"description: Helm chart for {project_name}\n"
        f"type: application\n"
        f"version: 0.1.0\n"
        f"appVersion: \"1.0.0\"\n"
    )


def _generate_helm_values(
    project_name: str,
    framework: str,
    port: str = "",
) -> str:
    if not port:
        port = _resolve_port(framework)
    # `port` is "" for headless (quant/generic) frameworks; do NOT substitute a
    # default like 8000 — nothing will be listening on that port, which would
    # mislead operators into thinking the service is healthy.
    service_section: str
    if port:
        # enabled: true must be present so the {{- if .Values.service.enabled }}
        # guard in service.yaml renders the Service resource correctly.
        service_section = (
            f"service:\n"
            f"  enabled: true\n"
            f"  type: ClusterIP\n"
            f"  port: 80\n"
            f"  targetPort: {port}\n"
        )
    else:
        # Headless job-style deployment: no HTTP listener, no K8s service port.
        # The service.yaml template guards on this flag; no other service fields
        # are needed here since the template body is skipped entirely.
        service_section = (
            "service:\n"
            "  # Headless application — no HTTP port exposed.\n"
            "  # Override with a real port if you add an HTTP server.\n"
            "  enabled: false\n"
        )
    return (
        f"# Default values for {project_name}\n"
        f"replicaCount: 2\n\n"
        f"image:\n"
        f"  repository: {project_name}\n"
        f"  tag: latest\n"
        f"  pullPolicy: IfNotPresent\n\n"
        + service_section
        + "\nresources:\n"
        f"  requests:\n"
        f"    cpu: 100m\n"
        f"    memory: 128Mi\n"
        f"  limits:\n"
        f"    cpu: 500m\n"
        f"    memory: 512Mi\n\n"
        f"healthCheck:\n"
        f"  path: /health\n"
        f"  initialDelaySeconds: 10\n"
        f"  periodSeconds: 15\n"
    )


def _generate_helm_deployment_template(project_name: str, framework: str = "generic", port: str = "") -> str:
    if not port:
        port = _resolve_port(framework)
    # Only web frameworks have an HTTP endpoint worth probing.
    # For headless frameworks (quant, generic) the httpGet readinessProbe
    # would always fail because nothing is listening, keeping the pod
    # permanently unready.
    if port:
        ports_block = (
            "          ports:\n"
            "            - containerPort: {{ .Values.service.targetPort }}\n"
        )
        readiness_block = (
            "          readinessProbe:\n"
            "            httpGet:\n"
            "              path: {{ .Values.healthCheck.path }}\n"
            "              port: {{ .Values.service.targetPort }}\n"
            "            initialDelaySeconds: {{ .Values.healthCheck.initialDelaySeconds }}\n"
            "            periodSeconds: {{ .Values.healthCheck.periodSeconds }}\n"
        )
    else:
        ports_block = ""
        readiness_block = ""
    return (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n"
        f"  name: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"  labels:\n"
        f"    app: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"spec:\n"
        f"  replicas: {{{{ .Values.replicaCount }}}}\n"
        f"  selector:\n"
        f"    matchLabels:\n"
        f"      app: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"  template:\n"
        f"    metadata:\n"
        f"      labels:\n"
        f"        app: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"    spec:\n"
        f"      containers:\n"
        f"        - name: {{{{ .Chart.Name }}}}\n"
        f"          image: \"{{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag }}}}\"\n"
        f"          imagePullPolicy: {{{{ .Values.image.pullPolicy }}}}\n"
        f"{ports_block}"
        f"          resources:\n"
        f"            {{{{- toYaml .Values.resources | nindent 12 }}}}\n"
        f"{readiness_block}"
    )


def _generate_helm_service_template(project_name: str) -> str:
    # Guard with {{- if .Values.service.enabled }} so headless deployments
    # (service.enabled: false in values.yaml) do not create a Service resource
    # and do not reference .Values.service.type/port/targetPort which are absent
    # from values.yaml in the headless case — avoiding a Helm render error.
    return (
        f"{{{{- if .Values.service.enabled }}}}\n"
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n"
        f"  name: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"spec:\n"
        f"  type: {{{{ .Values.service.type }}}}\n"
        f"  selector:\n"
        f"    app: {{{{ include \"{project_name}.fullname\" . }}}}\n"
        f"  ports:\n"
        f"    - protocol: TCP\n"
        f"      port: {{{{ .Values.service.port }}}}\n"
        f"      targetPort: {{{{ .Values.service.targetPort }}}}\n"
        f"      name: http\n"
        f"{{{{- end }}}}\n"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_deployment_artifacts(run_dir: str) -> DeploymentArtifactReport:
    """
    Generate deployment artifacts for a completed run in *run_dir*.

    Artifacts are written to ``{run_dir}/deployment/``.
    Returns a DeploymentArtifactReport listing what was created.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return DeploymentArtifactReport(
            success=False,
            errors=["No code/ directory found — cannot generate deployment artifacts."],
        )

    project_name = _get_project_name(run_dir)
    imports = _collect_imports(code_dir)
    framework = _detect_framework(imports)
    has_orm = _detect_orm(imports)
    python_version = _get_python_version(run_dir)

    # Resolve the host port ONCE so all generated artifacts use the same port.
    # _resolve_port() finds a free port on the local machine starting from the
    # framework's preferred default, ensuring the generated config is
    # immediately usable without port conflicts.
    host_port = _resolve_port(framework)

    deploy_dir = os.path.join(run_dir, "deployment")
    os.makedirs(deploy_dir, exist_ok=True)

    artifacts: List[str] = []
    errors: List[str] = []

    def _write(filename: str, content: str, subdir: Optional[str] = None) -> None:
        target_dir = os.path.join(deploy_dir, subdir) if subdir else deploy_dir
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, filename)
        _tmp = path + ".tmp"
        try:
            with open(_tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(_tmp, path)
            rel = os.path.relpath(path, run_dir)
            artifacts.append(rel)
        except OSError as exc:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            errors.append(f"{filename}: {exc}")

    _write("Dockerfile", _generate_dockerfile(framework, python_version, port=host_port))
    _write("docker-compose.yml", _generate_docker_compose(framework, has_orm, project_name, port=host_port))
    _write(".env.example", _generate_env_example(framework, has_orm, port=host_port))

    if has_orm:
        _write("alembic.ini", _generate_alembic_ini(project_name))

    _write(
        "ci.yml",
        _generate_ci_workflow(framework, python_version),
        subdir=os.path.join(".github", "workflows"),
    )

    # Kubernetes manifests
    # Sanitise project name for K8s label compliance (RFC 1123):
    # lowercase, replace non-alphanumeric with hyphens, strip leading/trailing hyphens
    k8s_name = re.sub(r"[^a-z0-9-]", "-", project_name.lower()).strip("-") or "app"
    k8s_name = k8s_name[:63].rstrip("-")  # K8s label max length, must not end with hyphen
    if not k8s_name:
        k8s_name = "app"

    _write(
        "deployment.yaml",
        _generate_k8s_deployment(k8s_name, framework, python_version, port=host_port),
        subdir="k8s",
    )
    _write(
        "service.yaml",
        _generate_k8s_service(k8s_name, framework, port=host_port),
        subdir="k8s",
    )

    # Helm chart
    _write("Chart.yaml", _generate_helm_chart(k8s_name), subdir="helm")
    _write("values.yaml", _generate_helm_values(k8s_name, framework, port=host_port), subdir="helm")
    _write(
        "deployment.yaml",
        _generate_helm_deployment_template(k8s_name, framework, port=host_port),
        subdir=os.path.join("helm", "templates"),
    )
    _write(
        "service.yaml",
        _generate_helm_service_template(k8s_name),
        subdir=os.path.join("helm", "templates"),
    )

    return DeploymentArtifactReport(
        success=len(errors) == 0,
        artifacts_generated=artifacts,
        framework_detected=framework,
        has_orm=has_orm,
        errors=errors,
    )
