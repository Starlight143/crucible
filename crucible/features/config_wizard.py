from __future__ import annotations
"""features/config_wizard.py
============================
Interactive configuration wizard that generates a complete .env file
from the current run analysis results and user preferences.

Usage::

    from crucible.feature_registry import run_features, FeatureConfig
    import crucible.features.config_wizard  # auto-registers

    config = FeatureConfig()
    results = run_features(
        "/path/to/run_dir",
        enabled_features=["config_wizard"],
        config=config,
    )

Environment variables
---------------------
CONFIG_WIZARD_ENABLED          Master switch; 0 = skip entirely (default: 1).
CONFIG_WIZARD_INCLUDE_OPTIONAL Include all optional vars from .env.example (default: 1).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _find_workspace_root(start: Path) -> Path:
    """Walk up from *start* to find .env.example, .git, or pyproject.toml."""
    current = start.resolve()
    for _ in range(12):
        if (current / ".env.example").exists():
            return current
        if (current / ".git").exists() or (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start.resolve()


def _parse_env_file(path: Path) -> Tuple[Dict[str, str], List[str]]:
    """Parse a .env or .env.example file.

    Returns (values dict, list of raw lines for comment preservation).
    """
    values: Dict[str, str] = {}
    raw_lines: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            raw_lines.append(line)
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                key = key.strip()
                val = val.strip().strip(chr(34) + chr(39))
                if key:
                    values[key] = val
    except Exception:
        pass
    return values, raw_lines


def _extract_run_info(run_dir: Path) -> Dict[str, Any]:
    """Extract relevant info from analysis_result.json."""
    info: Dict[str, Any] = {
        "score": 0,
        "risk_level": "unknown",
        "gate_decision": "unknown",
        "backtest_ran": False,
        "cost": 0.0,
        "run_duration_s": 0.0,
        "features_ran": [],
        "models_used": {},
    }
    ar = run_dir / "analysis_result.json"
    try:
        data = json.loads(ar.read_text(encoding="utf-8"))
        raw_score = data.get("score")
        info["score"] = int(raw_score) if raw_score is not None else 0
        info["risk_level"] = str(data.get("risk_level", "unknown")).lower()
        info["gate_decision"] = str(data.get("gate_decision", "unknown")).lower()
        raw_cost = data.get("cost")
        info["cost"] = float(raw_cost) if raw_cost is not None else 0.0
        info["run_duration_s"] = float(data.get("run_duration_s", 0.0) or 0.0)
        info["features_ran"] = list(data.get("features_ran", []))
        info["models_used"] = dict(data.get("models_used", {}))
    except Exception:
        pass
    rm = run_dir / "run_meta.json"
    try:
        meta = json.loads(rm.read_text(encoding="utf-8"))
        info["models_used"]["primary"] = meta.get("model_id", "")
    except Exception:
        pass
    # Heuristic: if backtest runner was in features or backtest result exists
    br = run_dir / "backtest_result.json"
    info["backtest_ran"] = br.exists() or "backtest_runner" in info["features_ran"]
    return info


def _build_recommendations(
    info: Dict[str, Any],
    example_vars: Dict[str, str],
    include_optional: bool,
) -> List[Tuple[str, str, str]]:
    """Build recommended config as list of (key, value, comment) tuples."""
    recs: List[Tuple[str, str, str]] = []

    score = info["score"]
    cost = info["cost"]
    duration = info["run_duration_s"]
    features_ran = info["features_ran"]
    backtest_ran = info["backtest_ran"]
    primary_model = info["models_used"].get("primary", "")

    # LLM Provider section
    recs.append(("LLM_PROVIDER", example_vars.get("LLM_PROVIDER", "openrouter"),
                 "LLM provider selection (openrouter|alibaba_coding_plan|ollama)"))

    if score >= 75 and primary_model:
        recs.append(("OPENROUTER_PRIMARY_MODEL", primary_model,
                     "These models produced a high-scoring run -- keep them"))
    elif "OPENROUTER_PRIMARY_MODEL" in example_vars:
        recs.append(("OPENROUTER_PRIMARY_MODEL",
                     example_vars["OPENROUTER_PRIMARY_MODEL"], ""))

    if cost > 1.0:
        recs.append(("MODEL_CASCADE_ENABLED", "1",
                     "Cost was high; cascade reduces spend"))

    if duration > 300:
        recs.append(("LIBRARIAN_INTER_QUERY_DELAY_SECONDS", "0.5",
                     "Run was slow; use minimum librarian delay to speed up"))

    if backtest_ran:
        recs.append(("ENHANCED_BACKTEST_RUNNER", "1",
                     "Enable backtest runner for future runs"))
        recs.append(("BACKTEST_LOOKBACK_DAYS", "90",
                     "90-day lookback used in this run"))

    if "citation_verifier" in features_ran:
        recs.append(("CITATION_VERIFY_ENABLED", "1",
                     "Citation verifier ran in this run"))

    # Add remaining vars from .env.example if include_optional
    if include_optional:
        existing_keys = {k for k, _, _ in recs}
        for k, v in example_vars.items():
            if k not in existing_keys:
                recs.append((k, v, ""))

    return recs


def _write_recommended_env(
    recs: List[Tuple[str, str, str]],
    run_dir: Path,
    run_info: Dict[str, Any],
) -> Path:
    """Write recommended_config.env to *run_dir*."""
    ts = datetime.now(timezone.utc).isoformat()
    score = run_info.get("score", 0)
    risk = run_info.get("risk_level", "unknown")
    lines: List[str] = [
        "# ============================================================",
        "# recommended_config.env",
        "# Auto-generated by Crucible config_wizard feature",
        f"# Generated at: {ts}",
        f"# Based on run score: {score}, risk: {risk}",
        "# Copy this file to .env (or merge into your existing .env)",
        "# ============================================================",
        "",
    ]
    for key, value, comment in recs:
        if comment:
            lines.append(f"# {comment}")
        lines.append(f"{key}={value}")
        lines.append("")
    out_path = run_dir / "recommended_config.env"
    _tmp_out = out_path.parent / (out_path.name + ".tmp")
    try:
        _tmp_out.write_text(chr(10).join(lines), encoding="utf-8")
        _tmp_out.replace(out_path)
    except OSError:
        try:
            _tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return out_path


def _compute_diff(
    recs: List[Tuple[str, str, str]],
    current_env: Dict[str, str],
    example_vars: Dict[str, str],
) -> Dict[str, Any]:
    """Compute config diff between recommendations and current .env."""
    rec_dict = {k: v for k, v, _ in recs}
    vars_added: List[str] = []
    vars_changed: List[Dict[str, str]] = []
    vars_removed_optional: List[str] = []

    for key in rec_dict:
        if key not in current_env:
            vars_added.append(key)
        else:
            cur_val = current_env[key]
            rec_val = rec_dict[key]
            # Flag if current is non-empty, non-placeholder, and different
            if (
                cur_val
                and not cur_val.startswith("replace_with_")
                and cur_val != rec_val
            ):
                vars_changed.append({
                    "key": key,
                    "current": cur_val,
                    "recommended": rec_val,
                })

    for key in current_env:
        if key not in rec_dict:
            vars_removed_optional.append(key)

    return {
        "vars_added": vars_added,
        "vars_changed": vars_changed,
        "vars_removed_optional": vars_removed_optional,
    }


@register("config_wizard")
class ConfigWizardFeature(BaseFeature):
    """Generate a recommended .env configuration from run analysis results.

    Reads .env.example and analysis_result.json, produces a tailored
    recommended_config.env and config_diff.json for the current run.

    Usage example::

        from crucible.feature_registry import run_features, FeatureConfig
        import crucible.features.config_wizard

        results = run_features(
            "/path/to/run_dir",
            enabled_features=["config_wizard"],
            config=FeatureConfig(),
        )
    """

    name = "config_wizard"
    label = "Config Wizard"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Generate recommended config files for this run.

        Parameters
        ----------
        run_dir:
            Path to the current pipeline run directory.
        config:
            Shared feature configuration (env vars, LLM, args).

        Returns
        -------
        FeatureResult
            Success status with paths to generated config files.
        """
        env: Dict[str, str] = config.env if config.env is not None else dict(os.environ)
        if env.get("CONFIG_WIZARD_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="CONFIG_WIZARD_ENABLED is not 1.",
            )

        t0 = time.monotonic()
        warnings: List[str] = []
        artifacts: List[str] = []

        include_optional = env.get("CONFIG_WIZARD_INCLUDE_OPTIONAL", "1").strip().lower() not in ("0", "false", "no", "off")
        rdp = Path(run_dir).resolve()
        ws = _find_workspace_root(rdp)

        # Load .env.example
        example_path = ws / ".env.example"
        example_vars, _ = _parse_env_file(example_path)
        if not example_vars:
            warnings.append("Could not find or parse .env.example.")

        # Load current .env
        env_path = ws / ".env"
        current_env, _ = _parse_env_file(env_path)

        # Extract run info
        run_info = _extract_run_info(rdp)

        # Build recommendations
        recs = _build_recommendations(run_info, example_vars, include_optional)

        # Write recommended_config.env
        rec_path: Optional[Path] = None
        try:
            rec_path = _write_recommended_env(recs, rdp, run_info)
            artifacts.append(str(rec_path))
        except Exception as exc:
            warnings.append(f"Failed to write recommended_config.env: {exc}")

        # Compute and write config_diff.json
        diff: Dict[str, Any] = {}
        diff_path = rdp / "config_diff.json"
        try:
            diff = _compute_diff(recs, current_env, example_vars)
            _tmp_diff = diff_path.parent / (diff_path.name + ".tmp")
            try:
                _tmp_diff.write_text(
                    json.dumps(diff, indent=2, default=str), encoding="utf-8"
                )
                _tmp_diff.replace(diff_path)
                artifacts.append(str(diff_path))
            except OSError:
                try:
                    _tmp_diff.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            warnings.append(f"Failed to write config_diff.json: {exc}")

        report: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workspace_root": str(ws),
            "recommendations_count": len(recs),
            "vars_added": diff.get("vars_added", []),
            "vars_changed": diff.get("vars_changed", []),
            "vars_removed_optional": diff.get("vars_removed_optional", []),
            "run_info": run_info,
        }

        _n_added = len(diff.get("vars_added", []))
        _n_changed = len(diff.get("vars_changed", []))
        return FeatureResult(
            feature=self.name,
            success=True,
            summary=(
                f"Config wizard: {len(recs)} vars recommended; "
                f"{_n_added} new, {_n_changed} changed."
            ),
            details={
                "config_wizard": report,
                "artifacts": artifacts,
                "warnings": warnings,
            },
            duration_seconds=time.monotonic() - t0,
        )
