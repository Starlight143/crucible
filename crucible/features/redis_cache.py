from __future__ import annotations
"""Redis-backed research cache for Crucible runs.

The feature stores a compact run summary keyed by a normalized user problem.
Redis is imported softly, so environments without the package still receive a
diagnostic report and a runnable warmup script.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore[assignment]


WARMUP_SCRIPT = r'''from __future__ import annotations
"""Warm Redis cache entries from Crucible run directories."""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

try:
    import redis  # type: ignore
except ImportError as exc:
    raise SystemExit("redis package is required for warmup: pip install redis") from exc


def load_json(path: str) -> Dict[str, Any]:
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())


def user_problem(run_dir: str) -> str:
    for name in ("analysis_result.json", "run_meta.json", "run_snapshot.json"):
        data = load_json(os.path.join(run_dir, name))
        value = data.get("user_problem") or data.get("query") or data.get("prompt")
        if value:
            return str(value)
    return ""


def score(run_dir: str) -> float:
    data = load_json(os.path.join(run_dir, "analysis_result.json"))
    value = next(
        (data[k] for k in ("score", "final_score", "consensus_score") if data.get(k) is not None),
        0,
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm Crucible Redis cache.")
    parser.add_argument("runs_root", help="Directory containing run subdirectories.")
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--prefix", default=os.environ.get("REDIS_CACHE_KEY_PREFIX", "quantsaas:"))
    _ttl_default = 24
    try:
        _ttl_default = int(os.environ.get("REDIS_CACHE_TTL_HOURS", "24"))
    except ValueError:
        pass
    parser.add_argument("--ttl-hours", type=int, default=_ttl_default)
    args = parser.parse_args()
    client = redis.from_url(args.redis_url)
    ttl = max(args.ttl_hours, 1) * 3600
    written = 0
    try:
        entries = os.listdir(args.runs_root)
    except OSError as exc:
        raise SystemExit(f"cannot list runs root: {exc}") from exc
    for entry in entries:
        run_dir = os.path.join(args.runs_root, entry)
        if not os.path.isdir(run_dir):
            continue
        problem = user_problem(run_dir)
        normalized = normalize(problem)
        if not normalized:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        key = f"{args.prefix}{digest}"
        payload = {"run_dir": run_dir, "score": score(run_dir), "timestamp": datetime.now(timezone.utc).isoformat(), "user_problem_snippet": problem[:300]}
        client.setex(key, ttl, json.dumps(payload, indent=2, ensure_ascii=False))
        written += 1
    print(json.dumps({"entries_written": written}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _load_json(path: str) -> Dict[str, Any]:
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _write_text(path: str, content: str) -> None:
    _tmp = path + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(_tmp, path)
    except OSError as exc:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _normalize_problem(text: str) -> str:
    return " ".join(text.lower().strip().split())


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _get_arg_value(args: Any, name: str) -> Any:
    try:
        return getattr(args, name)
    except Exception:
        return None


def _discover_user_problem(run_dir: str, config: FeatureConfig) -> str:
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))
    snapshot = _load_json(os.path.join(run_dir, "run_snapshot.json"))
    return _first_text(
        analysis.get("user_problem"),
        analysis.get("query"),
        meta.get("user_problem"),
        meta.get("query"),
        snapshot.get("user_problem"),
        snapshot.get("query"),
        config.extra.get("user_problem"),
        _get_arg_value(config.args, "user_problem"),
        _get_arg_value(config.args, "query"),
    )


def _score(run_dir: str) -> float:
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    value = next(
        (analysis[k] for k in ("score", "final_score", "consensus_score") if analysis.get(k) is not None),
        0,
    )
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@register("redis_cache")
class RedisCacheFeature(BaseFeature):
    name = "redis_cache"
    label = "Redis Research Cache"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("REDIS_CACHE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        report_path = os.path.join(run_dir, "redis_cache_report.json")
        try:
            warmup_path = os.path.join(run_dir, "redis_cache_warmup.py")
            _write_text(warmup_path, WARMUP_SCRIPT)
            problem = _discover_user_problem(run_dir, config)
            normalized = _normalize_problem(problem)
            try:
                ttl_seconds = max(int(os.environ.get("REDIS_CACHE_TTL_HOURS", "24")), 1) * 3600
            except ValueError:
                ttl_seconds = 24 * 3600
            prefix = os.environ.get("REDIS_CACHE_KEY_PREFIX", "quantsaas:")
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""
            key = f"{prefix}{digest}" if digest else ""
            report: Dict[str, Any] = {"redis_available": redis is not None, "cache_enabled": True, "cache_key": key, "cache_hit": False, "stored": False, "ttl_seconds": ttl_seconds, "warmup_script": warmup_path}
            if redis is None:
                report["error"] = "redis package is not installed"
                _write_json(report_path, report)
                return FeatureResult(feature=self.name, success=True, summary="Redis package unavailable", details=report, duration_seconds=time.monotonic() - start)
            if not normalized:
                report["error"] = "user_problem not found"
                _write_json(report_path, report)
                return FeatureResult(feature=self.name, success=True, summary="No user_problem available for cache key", details=report, duration_seconds=time.monotonic() - start)
            client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), socket_connect_timeout=3, socket_timeout=3)
            cached = client.get(key)
            if cached is not None:
                try:
                    report["cached_value"] = json.loads(cached.decode("utf-8") if isinstance(cached, bytes) else str(cached))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    report["cached_value"] = str(cached)
                report["cache_hit"] = True
            payload = {"run_dir": run_dir, "score": _score(run_dir), "timestamp": datetime.now(timezone.utc).isoformat(), "user_problem_snippet": problem[:300]}
            client.setex(key, ttl_seconds, json.dumps(payload, indent=2, ensure_ascii=False))
            report["stored"] = True
            report["stored_value"] = payload
            _write_json(report_path, report)
            return FeatureResult(feature=self.name, success=True, summary="Redis cache checked and updated", details=report, duration_seconds=time.monotonic() - start)
        except Exception as exc:
            report = {"redis_available": redis is not None, "cache_hit": False, "stored": False, "error": str(exc)}
            try:
                _write_json(report_path, report)
            except Exception:
                pass
            return FeatureResult(feature=self.name, success=False, summary="Redis cache failed", details=report, error=str(exc), duration_seconds=time.monotonic() - start)
