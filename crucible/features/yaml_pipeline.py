"""
features/yaml_pipeline.py
==========================
YAML-driven pipeline configuration reader, validator, and CLI generator.

Reads a ``pipeline.yaml`` file from the workspace root (walking up from
run_dir), validates its schema against expected keys, and produces:

1. An equivalent ``--feature`` CLI invocation string.
2. A ``pipeline_config_report.json`` file containing:
   - ``features_enabled``         — list of enabled feature names.
   - ``estimated_run_time_minutes`` — heuristic per-feature time sum.
   - ``validation_warnings``      — list of schema / value issues found.
   - ``cli_equivalent``           — shell command string.
   - ``source_file``              — resolved path to the pipeline.yaml used.
3. If no ``pipeline.yaml`` exists, a ``pipeline.yaml.example`` template is
   written to the workspace root so operators have a starting point.

Pipeline YAML schema (all keys optional):
==========================================
features:               # list of feature names to enable
  - options_analyzer
  - market_stream
  - ...

pipeline:               # boolean feature flags (overrides features list)
  run_backtest: true
  run_tearsheet: false
  ...

gates:                  # quality thresholds
  min_sharpe: 0.5
  max_drawdown: 0.25
  min_coverage: 0.7

llm:                    # LLM configuration
  model: claude-sonnet-4-5
  temperature: 0.2
  max_tokens: 4096

Environment variables
---------------------
YAML_PIPELINE_CONFIG    Filename of the pipeline config (default: 'pipeline.yaml').
YAML_PIPELINE_ENABLED   '1' to enable this feature (default: '1').
"""
from __future__ import annotations

import json
import os
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import BaseFeature, FeatureConfig, FeatureResult, register

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Known valid feature names for validation
_KNOWN_FEATURES = frozenset([
    'options_analyzer', 'alt_data_connectors', 'market_stream',
    'trading_platform', 'scheduler', 'yaml_pipeline',
    'backtest_runner', 'tearsheet', 'monte_carlo', 'factor_analyzer',
    'risk_attribution', 'dynamic_correlation', 'cointegration_analyzer',
    'regime_detector', 'signal_analyzer', 'quant_analytics',
    'portfolio_backtest', 'transaction_cost_model',
    'code_quality', 'security_scan', 'test_generator', 'type_coverage',
    'dependency_auditor', 'ci_cd', 'deployment_artifacts',
    'grafana_dashboard', 'prometheus_exporter', 'mlflow_sink',
    'notification_hooks', 'webhook_templates', 'report_exporter',
    'tearsheet', 'auth_manager', 'chat_bot', 'interactive_mode',
    'sandbox_executor', 'celery_worker', 'redis_cache', 'batch_runner',
    'checkpoint', 'diff_aware', 'run_deduplication', 'run_registry',
    'project_memory', 'project_profile', 'multi_project_compare',
    'independent_validator', 'llm_quality_scorer', 'citation_verifier',
    'agent_metrics', 'model_cascade', 'semantic_cache', 'few_shot_injector',
    'prompt_ab_test', 'prompt_version_tracker', 'api_version_autopatch',
    'external_data_connectors', 'document_ingestion', 'global_knowledge_base',
    'multilang_codegen', 'github_repo_analyzer', 'notion_export',
    'auto_remediator', 'watch_mode', 'config_wizard', 'report_annotations',
    'run_diff',
])

# Heuristic per-feature run time estimates (minutes)
_FEATURE_RUN_TIME: Dict[str, float] = {
    'backtest_runner': 5.0,
    'portfolio_backtest': 5.0,
    'monte_carlo': 4.0,
    'tearsheet': 2.0,
    'options_analyzer': 1.0,
    'market_stream': 1.0,
    'alt_data_connectors': 2.0,
    'trading_platform': 1.0,
    'scheduler': 0.5,
    'yaml_pipeline': 0.5,
    'code_quality': 2.0,
    'security_scan': 2.0,
    'test_generator': 3.0,
    'type_coverage': 1.5,
    'dependency_auditor': 1.0,
    'ci_cd': 2.0,
    'deployment_artifacts': 1.5,
    'grafana_dashboard': 1.0,
    'prometheus_exporter': 0.5,
    'mlflow_sink': 1.0,
    'document_ingestion': 3.0,
    'multilang_codegen': 4.0,
    'github_repo_analyzer': 2.0,
}
_DEFAULT_FEATURE_TIME = 1.0  # minutes for unlisted features

# Valid LLM model prefixes / names for validation
_KNOWN_MODEL_PREFIXES = (
    'claude', 'gpt', 'gemini', 'mistral', 'llama', 'command',
    'text-davinci', 'o1', 'o3',
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def _find_workspace_root(run_dir: str, config_filename: str) -> Tuple[str, Optional[str]]:
    """Walk up from run_dir to find workspace root containing config_filename.

    Returns (workspace_root, config_path_or_None).
    Walks a maximum of 6 directory levels to avoid traversing the entire FS.
    """
    current = os.path.abspath(run_dir)
    for _ in range(7):
        candidate = os.path.join(current, config_filename)
        if os.path.isfile(candidate):
            return current, candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(run_dir), None


def _load_yaml(path: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Load a YAML file with yaml (preferred) or a minimal fallback parser.

    Returns (data_dict, error_message_or_None).
    The fallback only handles simple key: value and list items; complex YAML
    should always be loaded via PyYAML.
    """
    try:
        import yaml  # type: ignore[import]
        with open(path, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return {}, 'YAML root is not a mapping'
        return data, None
    except ImportError:
        pass
    except Exception as exc:
        return {}, str(exc)

    # Minimal fallback: parse only top-level key: value and list items
    data: Dict[str, Any] = {}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
        current_key: Optional[str] = None
        current_list: Optional[List[Any]] = None
        for line in lines:
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith('#'):
                continue
            if stripped.startswith('  - ') or stripped.startswith('- '):
                item = stripped.lstrip().lstrip('- ').strip()
                if current_list is not None:
                    current_list.append(item)
            elif ':' in stripped and not stripped.startswith(' '):
                key, _, val = stripped.partition(':')
                key = key.strip()
                val = val.strip()
                if val:
                    # Try to coerce booleans and numbers
                    if val.lower() in ('true', 'yes'):
                        data[key] = True
                    elif val.lower() in ('false', 'no'):
                        data[key] = False
                    else:
                        try:
                            data[key] = int(val)
                        except ValueError:
                            try:
                                data[key] = float(val)
                            except ValueError:
                                data[key] = val
                    current_key = key
                    current_list = None
                else:
                    current_key = key
                    current_list = []
                    data[key] = current_list
    except OSError as exc:
        return {}, str(exc)

    return data, None


def _validate_schema(data: Dict[str, Any]) -> List[str]:
    """Return a list of validation warning strings for *data*.

    Does not raise; warnings are collected and reported in the output report.
    """
    warnings: List[str] = []

    # features list
    features = data.get('features')
    if features is not None:
        if not isinstance(features, list):
            warnings.append("'features' must be a list of strings")
        else:
            for f in features:
                if not isinstance(f, str):
                    warnings.append(f"Feature entry '{f}' is not a string")
                elif f not in _KNOWN_FEATURES:
                    warnings.append(f"Unknown feature name: '{f}'")

    # pipeline booleans
    pipeline = data.get('pipeline')
    if pipeline is not None:
        if not isinstance(pipeline, dict):
            warnings.append("'pipeline' must be a mapping of feature flags")
        else:
            for k, v in pipeline.items():
                if not isinstance(v, bool):
                    warnings.append(
                        f"pipeline.{k} should be a boolean (true/false), got '{v}'"
                    )

    # gates thresholds
    gates = data.get('gates')
    if gates is not None:
        if not isinstance(gates, dict):
            warnings.append("'gates' must be a mapping of threshold values")
        else:
            float_keys = {'min_sharpe', 'max_drawdown', 'min_coverage',
                          'max_volatility', 'min_cagr'}
            for k, v in gates.items():
                if k in float_keys:
                    try:
                        fv = float(v)
                        if k == 'min_sharpe' and fv < -10:
                            warnings.append(f"gates.{k}={fv} is unusually low")
                        if k == 'max_drawdown' and not (0.0 <= fv <= 1.0):
                            warnings.append(
                                f"gates.{k}={fv} should be in (0, 1] as a fraction"
                            )
                    except (TypeError, ValueError):
                        warnings.append(f"gates.{k} must be a number, got '{v}'")

    # llm config
    llm = data.get('llm')
    if llm is not None:
        if not isinstance(llm, dict):
            warnings.append("'llm' must be a mapping")
        else:
            model = llm.get('model', '')
            if model and not any(
                model.lower().startswith(p) for p in _KNOWN_MODEL_PREFIXES
            ):
                warnings.append(
                    f"llm.model='{model}' does not match a recognised model prefix"
                )
            temp = llm.get('temperature')
            if temp is not None:
                try:
                    ft = float(temp)
                    if not (0.0 <= ft <= 2.0):
                        warnings.append(
                            f"llm.temperature={ft} is outside typical range [0.0, 2.0]"
                        )
                except (TypeError, ValueError):
                    warnings.append(f"llm.temperature must be a float, got '{temp}'")
            max_tok = llm.get('max_tokens')
            if max_tok is not None:
                try:
                    it = int(max_tok)
                    if not (256 <= it <= 200000):
                        warnings.append(
                            f"llm.max_tokens={it} is outside plausible range [256, 200000]"
                        )
                except (TypeError, ValueError):
                    warnings.append(f"llm.max_tokens must be an integer, got '{max_tok}'")

    return warnings


def _extract_features(data: Dict[str, Any]) -> List[str]:
    """Return the deduplicated list of enabled feature names from *data*.

    Merges ``features`` list with ``pipeline.*: true`` boolean flags.
    """
    features: List[str] = []
    seen: set = set()

    # Explicit features list
    for f in (data.get('features') or []):
        if isinstance(f, str) and f not in seen:
            features.append(f)
            seen.add(f)

    # pipeline boolean flags
    pipeline = data.get('pipeline') or {}
    if isinstance(pipeline, dict):
        for k, v in pipeline.items():
            if v is True:
                # Strip run_ prefix if present
                name = k[4:] if k.startswith('run_') else k
                if name not in seen:
                    features.append(name)
                    seen.add(name)

    return features


def _build_cli_string(data: Dict[str, Any], features: List[str]) -> str:
    """Return an equivalent CLI invocation string."""
    parts = ['python run_crucible_enhanced.py run']
    for f in features:
        parts.append(f'--feature {f}')

    llm = data.get('llm') or {}
    if llm.get('model'):
        parts.append(f"--llm-model {llm['model']}")
    if llm.get('temperature') is not None:
        parts.append(f"--temperature {llm['temperature']}")
    if llm.get('max_tokens') is not None:
        parts.append(f"--max-tokens {llm['max_tokens']}")

    gates = data.get('gates') or {}
    if gates.get('min_sharpe') is not None:
        parts.append(f"--min-sharpe {gates['min_sharpe']}")
    if gates.get('max_drawdown') is not None:
        parts.append(f"--max-drawdown {gates['max_drawdown']}")

    return ' \\\n    '.join(parts)


def _estimate_run_time(features: List[str]) -> float:
    """Return heuristic total run time in minutes for the feature list."""
    return sum(
        _FEATURE_RUN_TIME.get(f, _DEFAULT_FEATURE_TIME) for f in features
    )


# ---------------------------------------------------------------------------
# Default template
# ---------------------------------------------------------------------------

_PIPELINE_YAML_EXAMPLE = textwrap.dedent('''\
    # pipeline.yaml.example
    # ======================
    # Crucible pipeline configuration template.
    # Copy to pipeline.yaml and customise.
    # All keys are optional; defaults will be used for missing values.

    # List of features to enable for this pipeline run.
    features:
      - options_analyzer
      - market_stream
      - backtest_runner
      - tearsheet
      - code_quality
      - security_scan
      - deployment_artifacts

    # Boolean feature flags (alternative to the features list above).
    # Keys prefixed with run_ are stripped; run_backtest enables backtest_runner.
    pipeline:
      run_backtest: true
      run_tearsheet: true
      run_monte_carlo: false

    # Quality gate thresholds. Pipeline fails if strategy does not meet these.
    gates:
      min_sharpe: 0.5        # Minimum annualised Sharpe ratio
      max_drawdown: 0.20     # Maximum allowable drawdown fraction (0–1)
      min_coverage: 0.70     # Minimum test coverage fraction (0–1)

    # LLM configuration.
    llm:
      model: claude-sonnet-4-5
      temperature: 0.2
      max_tokens: 4096
''')


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_yaml_pipeline(run_dir: str) -> Dict[str, Any]:
    """Read, validate, and process the pipeline YAML configuration.

    Parameters
    ----------
    run_dir:
        Pipeline run directory.

    Returns
    -------
    Dict with keys: source_file, features_enabled, estimated_run_time_minutes,
    validation_warnings, cli_equivalent.
    """
    config_filename = _env('YAML_PIPELINE_CONFIG', 'pipeline.yaml')
    workspace_root, config_path = _find_workspace_root(run_dir, config_filename)

    if config_path is None:
        # Write a template so the operator has a starting point
        example_path = os.path.join(workspace_root, f'{config_filename}.example')
        _tmp_ex = example_path + '.tmp'
        try:
            with open(_tmp_ex, 'w', encoding='utf-8') as fh:
                fh.write(_PIPELINE_YAML_EXAMPLE)
            os.replace(_tmp_ex, example_path)
        except OSError:
            try:
                os.unlink(_tmp_ex)
            except OSError:
                pass
            example_path = 'write-failed'

        report: Dict[str, Any] = {
            'source_file': None,
            'features_enabled': [],
            'estimated_run_time_minutes': 0.0,
            'validation_warnings': [
                f"'{config_filename}' not found in workspace; "
                f"example template written to: {example_path}"
            ],
            'cli_equivalent': 'python run_crucible_enhanced.py run',
            'example_template_path': example_path,
        }
    else:
        data, load_error = _load_yaml(config_path)
        warnings: List[str] = []
        if load_error:
            warnings.append(f'YAML parse warning: {load_error}')
        warnings.extend(_validate_schema(data))

        features = _extract_features(data)
        cli = _build_cli_string(data, features)
        est_time = _estimate_run_time(features)

        report = {
            'source_file': config_path,
            'features_enabled': features,
            'estimated_run_time_minutes': round(est_time, 1),
            'validation_warnings': warnings,
            'cli_equivalent': cli,
            'raw_config': data,
        }

    # Write report
    report_path = os.path.join(run_dir, 'pipeline_config_report.json')
    _tmp_report = report_path + '.tmp'
    try:
        with open(_tmp_report, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        os.replace(_tmp_report, report_path)
    except OSError:
        try:
            os.unlink(_tmp_report)
        except OSError:
            pass

    return report


# ---------------------------------------------------------------------------
# Feature registration
# ---------------------------------------------------------------------------

@register('yaml_pipeline')
class YamlPipelineFeature(BaseFeature):
    """YAML-driven pipeline configuration reader, validator, and CLI generator."""

    name = 'yaml_pipeline'
    label = 'YAML Pipeline Config'
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        t0 = time.monotonic()
        if _env('YAML_PIPELINE_ENABLED', '1').lower() in ('0', 'false', 'no', 'off'):
            return FeatureResult(
                feature=self.name,
                success=True,
                summary='yaml_pipeline disabled via YAML_PIPELINE_ENABLED.',
                details={'enabled': False},
                duration_seconds=time.monotonic() - t0,
            )
        try:
            report = run_yaml_pipeline(run_dir)
            source = report.get('source_file') or '(none — example template written)'
            n_features = len(report.get('features_enabled', []))
            n_warnings = len(report.get('validation_warnings', []))
            est = report.get('estimated_run_time_minutes', 0.0)
            summary = (
                f'YAML pipeline: {n_features} features from {source}; '
                f'est. {est}min; {n_warnings} warning(s)'
            )
            return FeatureResult(
                feature=self.name,
                success=True,
                summary=summary,
                details={
                    'features_enabled': report.get('features_enabled', []),
                    'estimated_run_time_minutes': est,
                    'validation_warnings': report.get('validation_warnings', []),
                },
                duration_seconds=time.monotonic() - t0,
            )
        except Exception as exc:
            return FeatureResult(
                feature=self.name,
                success=False,
                summary=str(exc),
                error=str(exc),
                duration_seconds=time.monotonic() - t0,
            )
