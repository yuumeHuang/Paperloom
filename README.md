# Paperloom · 研读

把科研 PDF 变成可以逐段切换原文和译文的论文。**电脑负责准备，手机或网页负责阅读。** 一篇论文对应一个 `.folio` 文件，里面有正文、译文、目录、图表和原 PDF。点击文中的 `Fig. 1` 或 `Table 1` 就能看图表。

## 先试读，无需模型账号

- **Windows 电脑**：安装 64 位 Python 3.11 或以上（推荐 3.12，带 pip）。在本仓库点 **Code → Download ZIP**，完整解压，双击 **`启动研读.cmd`**。首次启动需要联网安装依赖；浏览器打开后，在「我的书库」点教学示例的「开始研读」。
- **安卓手机**：下载 [Paperloom 0.3.4 安装包](web/dist/downloads/Paperloom-0.3.4.apk?raw=1)，按系统提示安装。支持 Android 7.0 及以上。打开就能读教学示例，不需要运行 PC 服务。

**想读自己的论文？** 按 [中文使用手册](MANUAL.md) 配置模型，在电脑上选一份 PDF，点「开始准备」。完成后点「阅读文献」，也可以把 `.folio` 发给手机离线读。自有 WebDAV 是可选项，只在需要多端同步时配置。

## 开发与维护

| 入口 | 用途 |
| --- | --- |
| `paperloom/`、[提示词](paperloom/prompts/) | PC 服务：PDF 提取、版面分析、翻译、独立模型校验及打包 |
| `web/dist/` | 网页和安卓共用的阅读器；静态文件可直接部署到 HTTPS 站点 |
| `mobile/android/` | 安卓工程；[从源码构建 APK、配置 WebDAV](docs/android-webdav.md) |
| [架构](docs/architecture.md) · [文献包格式](docs/format.md) · [测试](docs/testing.md) | 理解实现、校验包或运行回归测试 |
| [上传 GitHub](docs/github.md) | 从现有工作目录导出完整源码、教学示例和当前 APK，并首次推送 |
