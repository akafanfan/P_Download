# Pornhub Downloader

基于 `pornhub_api` + `base_api` 的 Pornhub 视频下载工具，支持单视频下载和 Model 批量下载（含多用户列表）。

## 环境要求

| 项目 | 要求 |
|------|------|
| Python | **3.12+**（推荐 3.12） |
| 系统 | Windows / Linux / macOS |
| 网络 | 建议配置代理（国内访问） |

## 依赖安装

```bash
pip install requests beautifulsoup4 PyYAML av
```

另外需要安装项目自带的本地包（根据你的项目结构）：

```bash
# 如果 base_api / pornhub_api 是本地源码
pip install -e ./base_api
pip install -e ./pornhub_api
```

> `av`（PyAV）用于 remux（把下载的 TS 片段转成最终 MP4）。Windows 上如果安装失败，可尝试：
> ```bash
> pip install av --no-binary av
> ```
> 或使用预编译 wheel。

## 项目结构（示例）

```
PD/
├── config.yml          # 配置文件
├── main.py             # 主程序（或你当前的脚本名）
├── base_api/           # 底层下载核心
├── pornhub_api/        # Pornhub API 封装
└── downloads/          # 默认下载目录
```

## 配置说明 `config.yml`

### 多用户模式（推荐）

```yaml
# ========== 全局公共配置 ==========
mode: model
proxy: http://127.0.0.1:7897
quality: "480"
remux: true
request_delay: 2
download_max_retry: 4
retry_sleep: 5

# ========== 多用户列表 ==========
userlist:
  - name: blondessa
    target: https://www.pornhub.com/model/blondessa
    save_path: ./downloads/blondessa
    interval: "all"
    model_start_page: 2
    model_end_page: 2

  - name: another_model
    target: https://www.pornhub.com/model/xxx
    save_path: ./downloads/xxx
    interval: "2026-01-01 00:00:00"
    model_start_page: 1
    model_end_page: 5
```

### 单用户兼容写法（旧配置仍可用）

```yaml
mode: model
target: https://www.pornhub.com/model/blondessa
save_path: ./downloads
interval: "all"
model_start_page: 2
model_end_page: 2

proxy: http://127.0.0.1:7897
quality: "480"
remux: true
request_delay: 2
download_max_retry: 4
retry_sleep: 5
```

### 配置项说明

| 配置项 | 类型 | 说明 | 默认值 |
|--------|------|------|--------|
| `mode` | str | `single` 单视频 / `model` 批量 | `single` |
| `target` | str | 单视频或单 Model 链接（无 userlist 时使用） | - |
| `userlist` | list | 多用户列表，每项独立配置 | `[]` |
| `name` | str | 用户名称（仅日志和回写 interval 用） | - |
| `save_path` | str | 下载保存目录 | `./downloads` |
| `interval` | str | 增量过滤时间。`all` = 不过滤；否则只下载比该时间更新的视频 | `all` |
| `model_start_page` | int | 起始页（≥1） | `1` |
| `model_end_page` | int | 终止页。`≤0` 表示无上限 | `20` |
| `quality` | str | 清晰度，如 `480` / `720` / `1080` | `480` |
| `proxy` | str | HTTP/HTTPS 代理 | 空 |
| `proxy_auth` | str | 代理认证（如有） | 空 |
| `remux` | bool | 是否将 TS 转成 MP4 | `true` |
| `request_delay` | float | 每次请求/下载间隔（秒） | `4` |
| `download_max_retry` | int | 下载失败最大重试次数 | `3` |
| `retry_sleep` | float | 超时重试等待时间（秒） | `3` |

### interval 规则

- `"all"`：不过滤，全部下载
- `"2026-01-01 00:00:00"`：只下载发布时间 **晚于** 该时间的视频
- 当某个用户从 **第 1 页** 跑完后，会自动把该用户的 `interval` 回写为当天零点，方便下次增量更新

## 使用方法

1. 编辑 `config.yml`
2. 运行：

```bash
python main.py
```

### 单视频模式

```yaml
mode: single
target: https://www.pornhub.com/view_video.php?viewkey=xxxxxx
save_path: ./downloads
```

### 多 Model 模式

配置好 `userlist` 后直接运行即可，程序会按顺序依次处理每个用户。

## 功能特性

- 支持 Model 视频列表分页抓取
- 当前页视频 **反序** 下载
- 按发布时间做增量过滤（`interval`）
- 文件名自动清理 Windows 非法字符（`\ / * ? : " < > |`）
- 下载前强制使用安全文件名，降低 `Errno 22` 概率
- 失败 URL 汇总输出，方便后续重试
- 支持代理
- 多用户独立配置（路径、页码、interval 互不影响）
- 从第 1 页完整跑完后自动回写对应用户的 `interval`

## 常见问题

### 1. `av.error.ArgumentError: Invalid argument ... returned 22`

- 发生在 **remux** 阶段（TS → MP4）
- 常见原因：文件名仍含特殊字符、相对路径、残留临时文件、杀毒软件占用
- 处理建议：
  - 检查 `downloads` 目录是否有同名 `.mp4` / `.tmp`，先删除
  - 尽量使用较短、干净的 `save_path`
  - 临时关闭实时杀毒扫描再试

### 2. 下载超时

适当增大配置：

```yaml
download_max_retry: 6
retry_sleep: 8
request_delay: 3
```

### 3. 页面解析不到视频

- 确认 `target` 是否为正确的 Model 主页
- 检查代理是否正常
- Pornhub 页面结构变化时，可能需要更新选择器

## 注意事项

- 仅供个人学习与研究使用，请遵守当地法律法规及网站服务条款
- 大量下载请控制频率，避免对目标站点造成压力
- Windows 下文件名长度和特殊字符限制较严格，已做基础清理，但仍建议标题不要过长

## License

仅供个人使用，请勿用于商业用途。
```
