# AI 量化研究台跨平台构建

Windows 和 macOS 使用同一套选股、同步、监控、答疑与复盘代码。平台差异仅是
Electron 安装格式、PyInstaller 原生 sidecar 和代码签名。

## macOS Apple Silicon

```bash
git clone --branch feature/cross-platform-research-client --single-branch \
  https://github.com/Frankie-Foo/ai-quant-trading-system.git
cd ai-quant-trading-system
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-macos-research.txt
mkdir -p client/build/bootstrap
cp /path/to/research-bootstrap.zip client/build/bootstrap/research-bootstrap.zip
cd client
npm ci
npm run dist:mac:analyst -- --arm64
```

输出位于 `client/release-macos-analyst/`。没有 bootstrap 时客户端仍可启动，但
首次需要同步完整历史数据。bootstrap 不包含 API Key，只包含带哈希清单的 accepted
研究快照。

## Windows

```powershell
cd client
$env:BOOTSTRAP_DATA_ROOT = "C:\path\to\trading-system\data"
npm ci
npm run dist:win:analyst
```

Windows 测试包当前未做 Authenticode 签名。macOS 测试包当前未做 Developer ID
签名和 notarization。不要把环境变量文件、Key 或签名证书提交到 Git。
