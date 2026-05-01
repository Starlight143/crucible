from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

run_runtime_validation = _rt.run_runtime_validation
run_quality_review = _rt.run_quality_review
run_quality_fix = _rt.run_quality_fix
run_quality_loop = _rt.run_quality_loop
resolve_runtime_profile = _rt.resolve_runtime_profile
run_api_version_check = _rt.run_api_version_check
inject_api_issues_into_review = _rt.inject_api_issues_into_review

__all__ = [
    "run_runtime_validation",
    "run_quality_review",
    "run_quality_fix",
    "run_quality_loop",
    "resolve_runtime_profile",
    "run_api_version_check",
    "inject_api_issues_into_review",
]
