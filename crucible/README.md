# crucible

本文件只保留目前主線 package 的內部結構與補充說明。

主文件、主命令、主使用方式與發版規則，全部以根目錄 [README.md](/E:/文件/程式碼打包/crucible/README.md) 為準。

## 主線內部結構

- `run_crucible.py`：根目錄主啟動器
- `__main__.py`：package 入口
- `cli.py`：CLI facade
- `analysis.py`：analysis / codegen facade
- `research.py`：research / direction facade
- `quality.py`：runtime validation / quality loop facade
- `models.py`：Pydantic models facade
- `bootstrap.py`：env / init helpers facade
- `runtime_api.py`：runtime 單例入口
- `module_runtime.py`：主線 runtime 組裝
- `modules/`：import-based section modules
- `sections/`：由舊版切分出的對照切片

## 補充命令

```powershell
python .\run_crucible.py --help
python .\crucible\smoke_test.py
python -m pytest tests -q -p no:cacheprovider
```

## 備註

- 根目錄不再保留 `crucible.py` 相容 shim
- V14 與更早版本歷史已移到 `OLD_version/`，由備份 README 保存描述
- GitHub 主線 repo 只保留目前主線與之後的更新
