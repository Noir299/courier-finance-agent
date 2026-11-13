# 快递财务智能体

面向快递网点的离线财务工作台，支持客户账单与发件成本查询、运单匹配、历史出港验证报表、结算日报、单据归档和任务管理。Python 后端流式读取大文件并建立 SQLite 索引。本地 Ollama 模型负责识别问题和选择工具，计算与导出由后端执行。

## 从源码运行

准备 Python 3.11 或更新版本和本机 Ollama。在项目根目录执行以下命令。

```text
python -m pip install -r requirements.txt
python -B app/server.py --data-dir ./data --port 8765
```

打开 [本地工作台](http://127.0.0.1:8765/)。需要用模型查询时，先在本机 Ollama 准备 `qwen2.5:7b`。服务默认连接 `127.0.0.1:11434`，可用 `FINANCE_OLLAMA_URL` 指定其他本机端口，用 `FINANCE_MODEL` 和 `FINANCE_MODEL_CONTEXT` 配置已安装模型及上下文。离线部署时需提前准备 Python 依赖、Ollama 和模型权重。

通过「数据中心」导入客户账单和发件成本。页面会识别 `客户账单数据.zip` 和 `发件成本数据.zip`。首次运行也可以用 `--bill` 和 `--cost` 指定本地文件。历史出港验证使用客户账单中的「运费」和 T−5 至 T 的发件成本原「总金额」。试用时可输入「生成 YYYY-MM-DD 日报」，日期填入实际有数据的账单计费日。结算日报需要按页面提供的模板导入标准结算明细。

## 项目目录

- `app/index.html` 和 `app/sample_bridge.js` 是工作台页面与接口适配。
- `app/server.py`、`app/tool_router.py` 和 `app/data_engine.py` 提供本机服务、工具路由及账单导入。
- `app/daily_report.py`、`app/outbound_report.py` 和 `app/build_outbound_bill.py` 负责日报、历史出港验证和运费索引。
- `app/windows_launcher.cs`、`run_offline.py`、`启动.cmd` 和 `启动.exe.config` 用于 Windows 离线包的启动流程。离线包需要另备编译后的启动器、WebView2 SDK、便携运行环境、本地模型和索引。

业务原件、SQLite 数据库、导出报表、模型权重和便携运行环境由部署方在本地准备。工作台生成的会话、索引和任务状态保存在 `--data-dir` 指定的目录。
