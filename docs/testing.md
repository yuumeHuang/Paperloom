# 在自己的电脑上验证

在仓库根目录运行。测试使用仓库自编的两页论文、模拟模型和临时 WebDAV 服务；无需真实 API Key、付费调用、个人论文或云端账号。

## 不用模型，跑通 PDF → 文献包

Windows 若已双击 `启动研读.cmd` 安装依赖，可以直接用同一 Python 执行：

```powershell
python run.py convert web/dist/demo/source.pdf --output output/first-paper --no-llm
python run.py verify output/first-paper/paper.folio
```

第一条命令的输出目录必须不存在或为空。第二条应显示「校验通过」。在阅读器点「导入文献包」可打开结果；要看预先写好的双语对照，请导入 `web/dist/demo/demo.folio`。

## 运行回归测试

在全新检出的源码中安装 Python 3.11+、Node.js 22+，再运行以下 PowerShell 命令。JavaScript 同步测试会从 `mobile` 的开发依赖加载 XML 解析器；不需要安装安卓 SDK。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q tests
npm.cmd ci --prefix mobile --ignore-scripts
node --test tests/reader.test.mjs tests/sync.test.mjs
```

Python 覆盖提取、排序、自动校验、打包、准备端 API 和 WebDAV。JavaScript 覆盖文献包导入、阅读状态、书签及同步冲突。若只想确认浏览器是否连到准备端，启动后访问 `http://127.0.0.1:8765/api/health`，应看到 `service` 为 `paperloom`。
