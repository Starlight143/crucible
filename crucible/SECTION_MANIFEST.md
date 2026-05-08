# crucible section manifest

Module-by-module breakdown of `crucible/modules/`:

- `section_00_bootstrap_and_utils.py`: 環境載入、平台相容、JSON/模板/基本工具函式。
- `section_01_extraction_and_reformat.py`: 結構化輸出抽取、reformatter 與結果修復邏輯。
- `section_02_research_and_llm.py`: 研究流程、方向辯論、LLM 初始化與本地快取。
- `section_03_models_and_context.py`: Pydantic 模型、project context、modes、crew 建構。
- `section_04_web_research_and_direction.py`: Web/context7/GitHub research 與 direction decision 邏輯。
- `section_05_analysis_and_codegen.py`: analysis/codegen 階段與 code bundle/review 範圍控制。
- `section_06_runtime_quality_api.py`: runtime validation、quality loop、API version check。
- `section_07_selfcheck_output_main.py`: self-check、輸出保存與 CLI 主流程入口。
