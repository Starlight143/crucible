# crucible (package)

本文件只說明目前主線 `crucible/` package 的內部結構。

主文件、主命令、主使用方式以根目錄 [README.md](../README.md) 為準（中文版：[README_zh.md](../README_zh.md)）。

## 目錄結構

```
crucible/
├── __init__.py            # public API re-exports
├── __main__.py            # python -m crucible 入口
├── cli.py                 # CLI facade
├── analysis.py            # analysis / codegen facade
├── research.py            # research / direction facade
├── quality.py             # runtime validation / quality loop facade
├── models.py              # Pydantic models facade
├── bootstrap.py           # env / init helpers facade
├── runtime_api.py         # runtime 單例入口（get_runtime）
├── module_runtime.py      # 主線 runtime 組裝
├── modules/               # import-based section modules
│   ├── section_00_bootstrap_and_utils.py
│   ├── section_01_extraction_and_reformat.py
│   ├── section_02_research_and_llm.py
│   ├── section_03_models_and_context.py
│   ├── section_04_web_research_and_direction.py
│   ├── section_05_analysis_and_codegen.py
│   ├── section_06_runtime_quality_api.py
│   └── section_07_selfcheck_output_main.py
├── features/              # 可選功能模組（feature_registry 統一掛載）
└── web_research/          # 研究 swarm / crew / http clients
```

## 主線 runtime 元件

- `_runtime_loader.py` / `_temp_runtime.py` / `_file_cache.py`：runtime 載入與暫存目錄管理
- `feature_registry.py`：features 動態註冊
- `cancellation.py` / `progress.py` / `streaming.py`：任務生命週期
- `cost_tracker.py` / `error_budget.py` / `context_budget.py` / `context_pressure.py`：預算與壓力管控
- `convergence_guard.py` / `output_validation.py` / `resilience.py` / `http_retry.py`：穩定性與輸出檢查
- `telemetry.py` / `runtime_logging.py` / `run_correlation.py`：可觀測性
- `errors.py` / `hooks.py` / `generate.py` / `smoke_test.py`：例外型別、生命週期 hooks、產生器、煙霧測試

## Section manifest

對照各 section 與 `modules/section_*.py` 對應檔案的細節，請見 [SECTION_MANIFEST.md](SECTION_MANIFEST.md)。

## 補充命令

```powershell
python .\run_crucible.py --help
python .\crucible\smoke_test.py
python -m pytest tests -q -p no:cacheprovider
```

## Public API（`from crucible import ...`）

`get_runtime`、`main`、`run_self_check`、`load_api_key`、`init_llm`、
`AnalysisReport`、`CodeBundle`、`ReviewReport`、`DirectionDecision`、`GateDecision`、
`run_librarian_research`、`run_direction_debate`、
`build_crew`、`build_analysis_crew`、`build_codegen_crew`、
`run_runtime_validation`、`run_quality_loop`、`run_api_version_check`
