from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

build_crew = _rt.build_crew
build_code_fix_crew = _rt.build_code_fix_crew
build_analysis_crew = _rt.build_analysis_crew
build_codegen_crew = _rt.build_codegen_crew
run_analysis_with_selective_rerun = _rt.run_analysis_with_selective_rerun
run_codegen_stage = _rt.run_codegen_stage
extract_analysis_report = _rt.extract_analysis_report
extract_code_bundle = _rt.extract_code_bundle
extract_review_report = _rt.extract_review_report
format_code_bundle_for_review = _rt.format_code_bundle_for_review

__all__ = [
    "build_crew",
    "build_code_fix_crew",
    "build_analysis_crew",
    "build_codegen_crew",
    "run_analysis_with_selective_rerun",
    "run_codegen_stage",
    "extract_analysis_report",
    "extract_code_bundle",
    "extract_review_report",
    "format_code_bundle_for_review",
]
