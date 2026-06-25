# IPDown

多 IP 并行下载工具 + 深信服认证脚本集合。

## 组件

| 文件 | 说明 |
|------|------|
| `ipdown.py` | 多 IP 并行下载器，纯 Python 实现 |
| `sangfor_auth.py` | 深信服 AC Portal 自动认证 |
| `bind.ps1` | 网络 IP 管理（绑定/释放/扫描） |
| `ipdown.ps1` | ~~旧版实现，已废弃~~ |

## 快速开始

### 环境要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（包管理）

### 安装依赖

```bash
uv sync
```

### 配置深信服认证

```bash
cp .env.example .env
# 编辑 .env 填写用户名密码
```

## 使用方法

### IP Down 下载器

```bash
# 基本用法 - 自动检测网关适配器 IP 并行下载
uv run ipdown.py https://example.com/file.zip

# 指定输出文件名
uv run ipdown.py https://example.com/file.zip -o output.zip

# 手动指定 IP
uv run ipdown.py https://example.com/file.zip -i 192.168.1.100,192.168.1.101

# 指定适配器
uv run ipdown.py https://example.com/file.zip -a "以太网"

# 使用代理
uv run ipdown.py https://example.com/file.zip --proxy 127.0.0.1:7890

# 跳过证书验证
uv run ipdown.py https://example.com/file.zip -k
```

**参数说明：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-o, --output` | 输出文件名 | 从 URL 提取 |
| `-i, --interface` | 指定 IP（逗号分隔） | 自动检测 |
| `-a, --adapter` | 适配器名称 | 自动检测网关 |
| `-p, --parts` | 分片数 | IP 数量 |
| `-t, --timeout` | 连接超时（秒） | 30 |
| `-T, --total-timeout` | 单片总超时（秒） | 600 |
| `-r, --retries` | 最大重试次数 | 3 |
| `--proxy` | HTTP 代理 | 无 |
| `-k, --insecure` | 跳过证书验证 | 否 |

**特性：**
- 纯 Python，无需 curl
- 自动检测网关适配器，绑定源 IP 下载
- 支持断点续传（基于 manifest）
- 自动重试 + 进度条

### 深信服认证

```bash
uv run sangfor_auth.py

# 指定 IP 段
uv run sangfor_auth.py --ips 192.168.132.200,192.168.132.201
```

### 使用预编译 EXE

从 [Releases](../../releases) 下载最新版本，无需 Python 环境。

**IP Down：**

```bash
ipdown.exe https://example.com/file.zip
ipdown.exe https://example.com/file.zip -o output.zip
ipdown.exe https://example.com/file.zip -i 192.168.1.100,192.168.1.101
```

**深信服认证：**

将 `.env` 放在 exe 同级目录或当前工作目录：

```bash
sangfor_auth.exe
```

> **注意：** 打包后的 exe 从当前工作目录读取 `.env`，请确保在正确目录下运行。

### IP 管理

```powershell
.\bind.ps1 list                    # 查看当前 IP
.\bind.ps1 add 3                   # 自动添加 3 个空闲 IP
.\bind.ps1 add 192.168.132.200     # 绑定指定 IP
.\bind.ps1 remove 192.168.132.200  # 移除 IP
.\bind.ps1 scan                    # 扫描子网
```

## 打包为可执行文件

```bash
.\build.bat
```

打包产物位于 `dist/` 目录：
- `ipdown.exe` - 下载器
- `sangfor_auth.exe` - 认证工具

> 打包后将 `.env` 放在 exe 同级目录或当前工作目录即可。

## 项目结构

```
ipdown/
├── ipdown.py            # 多 IP 并行下载器
├── sangfor_auth.py      # 深信服认证
├── bind.ps1             # IP 管理脚本
├── ipdown.ps1           # ~~旧版实现，已废弃~~
├── auth.bat             # 认证快捷启动
├── build.bat            # Nuitka 打包脚本
├── pyproject.toml       # 项目配置
├── .env.example         # 环境变量模板
└── .python-version      # Python 版本锁定
```

## License

[Apache License 2.0](LICENSE)
