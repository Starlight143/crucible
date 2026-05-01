# crucible section manifest

Source: OLD_version/crucible_v14.py

- `00_bootstrap_and_utils.py` -> `section_00_bootstrap_and_utils.py`: lines 1-941 | 環境載入、平台相容、JSON/模板/基本工具函式。
- `01_extraction_and_reformat.py` -> `section_01_extraction_and_reformat.py`: lines 942-2337 | 結構化輸出抽取、reformatter 與結果修復邏輯。
- `02_research_and_llm.py` -> `section_02_research_and_llm.py`: lines 2338-3507 | 研究流程、方向辯論、LLM 初始化與本地快取。
- `03_models_and_context.py` -> `section_03_models_and_context.py`: lines 3508-6210 | Pydantic 模型、project context、modes、crew 建構。
- `04_web_research_and_direction.py` -> `section_04_web_research_and_direction.py`: lines 6211-11167 | Web/context7/GitHub research 與 direction decision 邏輯。
- `05_analysis_and_codegen.py` -> `section_05_analysis_and_codegen.py`: lines 11168-12560 | analysis/codegen 階段與 code bundle/review 範圍控制。
- `06_runtime_quality_api.py` -> `section_06_runtime_quality_api.py`: lines 12561-16027 | runtime validation、quality loop、API version check。
- `07_selfcheck_output_main.py` -> `section_07_selfcheck_output_main.py`: lines 16028-17419 | self-check、輸出保存與 CLI 主流程入口。
