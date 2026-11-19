# 安卓 App 与 WebDAV 同步（0.3.4）

## 使用

手机从仓库下载 [Paperloom-0.3.4.apk](../web/dist/downloads/Paperloom-0.3.4.apk?raw=1)，按 Android 系统提示安装。App 包含离线阅读器，不需要登录或配置模型。支持 Android 7.0 / API 24 及以上，请使用更新后的 Android System WebView。

PC 文献准备完成后，点击“阅读文献”，将 `.folio` 加入本机书库。打开“WebDAV 同步”，填写服务地址、相对目录、账号和应用专用密码，测试连接并保存配置，再点击“立即同步”。安卓 App 和其他阅读器配置相同地址与目录即可下载文献。教学示例默认不上传。

“测试连接”会只读检查基础目录及同步子目录，提示缺失的目录数量；“立即同步”会保存并使用当前表单，避免测试与同步使用不同配置。同步先检查目录存在性，仅对缺失目录使用 MKCOL，并读取验证创建结果。安卓空 MKCOL 使用零字节请求体，确保 Content-Length 为 0。失败信息包含 HTTP 状态、方法与相对路径，不显示密码。

Android 系统返回键和侧滑返回共用同一处理顺序：弹窗、侧栏、查找面板、样式面板、书库。只在书库首页返回时移到后台。页面使用 Capacitor SystemBars 的动态安全区，覆盖状态栏、刘海与导航区域；旧 WebView 已由原生布局处理的留白不会重复添加。夜间模式同步调整系统栏图标颜色。实现依据：[Android WebView 安全区](https://developer.android.com/develop/ui/views/layout/webapps/understand-window-insets)、[Capacitor SystemBars](https://capacitorjs.com/docs/apis/system-bars)。

0.3.4 为状态栏的安全区增加固定、不透明的背景，颜色与日夜阅读主题一致，避免正文滚动到系统图标后面。旧 WebView 使用原生留白时，窗口背景也会随主题更新。保留系统安全区处理，不改变状态栏高度。实现依据：[Android 对系统栏下方内容的处理](https://developer.android.com/develop/ui/views/layout/edge-to-edge)。

同步文献包、段落书签（包括取消书签）和阅读进度／语言模式。文献本身包含原文、译文、目录、图表和来源 PDF，因此每台设备均可离线阅读。模型配置、模型密钥、准备任务缓存和 WebDAV 密码不会进入同步状态文件。

手动同步始终可用。开启自动同步后，在 App / 页面前台运行时同步；系统后台调度不在本版范围内。关闭页面或杀掉 App 后不会常驻后台上传。浏览器密码仅保留当前会话；安卓版可选择通过 Android Keystore 加密保存登录信息。

0.3.4 同步时保留正在阅读的页面位置、语言、段落切换、折叠状态和图表弹窗。相同文献版本收到书签变化时只刷新书签标记。其他设备的进度和语言设置会更新未打开的文献；正在阅读的文献继续使用当前会话位置。下载到新版文献包时存入书库，下次重新打开生效；旧版本页面的进度不会写入新版文献记录，书签可通过已提供的段落别名同步。

从本机移除文献会保留云端副本，并记录本机隐藏状态，后续同步不会马上重新下载。同步结果会显示“云端 N 篇、本机 M 篇”，并明确说明被隐藏的数量；同步设置可展开查看对应标题。点击侧栏或设置里的“恢复并同步云端书库”即可恢复并下载，无需再点一次同步。取消书签会同步到其他设备。“断开连接”会停止同步并移除保存的连接信息，本机和云端文献继续保留。

不同服务地址、相对目录或账号分别保存同步记录。切换连接会载入对应记录，并将当前本机文献加入该书库；先前连接中仅存在于云端的记录不会带入新目录。切回原连接可继续使用其尚未发布的本机变更。

0.3.3 将本机隐藏状态也按连接分别保存。旧版全局隐藏列表只迁移一次，绑定到最后使用的连接；恢复后的空列表会持久化，重启不会重新迁入旧隐藏记录。文献从本地存储成功移除后才记录隐藏状态。恢复不删除云端包、进度或书签，正在同步时不能同时发起恢复操作。教学示例不计入云端与本机的同步数量；已配置同步的空书库不会自动塞入示例，主动移除过的示例也不会反复出现。

0.3.2 每次读取当前云端快照后，会将既无本地文献也无当前云端记录的旧缓存条目隔离到该连接专用的本机记录中。条目的进度与书签保留，后续重新出现本地副本或云端记录时恢复合并。缓存里较新的文献包引用如果没有实际本地副本，不会覆盖当前云端的包引用；阅读变更仍按时间戳合并。这同时兼容从未分连接保存清单的旧版本迁移，以及 0.3.0/0.3.1 已迁移的旧缓存。

云端快照提到文献包并不保证文件存在。拥有匹配本地副本的设备会先检查文件，缺失时重新上传；HEAD 不受支持时用 GET 并验证大小和哈希。接收端遇到单篇 404 会继续接收其他文献并明确显示部分完成，保留待恢复记录。新快照只发布确实存在的包；未接收的新版本不会把其阅读位置应用到旧本地文档。隔离记录不发送到云端，也不含密码。

## 接入方式

- 安卓：原生 OkHttp 网络传输，不依赖浏览器 CORS。系统文件选择器导入／导出 `.folio`，无需读取所有文件的权限。
- PC：同源 FastAPI 代理，凭据只在当前请求内使用；大文件流式上传／下载，不以整包 Base64 包装。
- 独立网页：直接连接 HTTPS WebDAV。服务需允许阅读器 Origin、`GET/HEAD/PUT/MKCOL/PROPFIND/OPTIONS`、`Authorization/Content-Type/Depth/If-None-Match`。不满足 CORS 的服务可使用安卓 App 或 PC 阅读器。

默认需要 HTTPS，不关闭 TLS 证书验证。局域网 HTTP 仅在用户主动勾选后由安卓或 PC 代理使用；独立 HTTPS 网页不能访问 HTTP WebDAV。地址填写 WebDAV 服务最终地址，不使用重定向或 URL 中的密码／token。

部分厂商文件管理器按 MIME 将导出文件改为 `.bin`。软件尝试保留 `.folio` 名称；不支持文档重命名时显示实际保存名称，安卓和网页可直接重新导入该 `.bin` 文献包，内容仍通过同一清单和哈希校验。

## 同步协议

```text
<配置的地址>/<相对目录>/v1/
  packages/<文献包 SHA-256>.folio
  devices/<设备 UUID>.<状态 SHA-256>.json
```

每台设备保存独立状态文件；其中每篇文献有包版本、阅读进度和逐段书签寄存器。时间戳为 `[毫秒, 逻辑计数, 设备 UUID]`，按最新变更合并，取消书签保留为 false 记录。新文献包先上传，成功后再发布引用它的状态。下载必须核对整包大小和 SHA-256，并通过原有 `.folio` 清单校验，之后才进入书库。

每份状态以内容 SHA-256 命名，只新增快照；同一设备两个窗口同时修改也会保存到不同文件。服务即使忽略条件请求或不提供 ETag，也不会因此覆盖另一份状态。快照写入后会读取并核对文件名哈希；所有已知快照按寄存器时间戳合并。已验证的不可变快照使用最多 16 MB 内存缓存，减少重复读取。同一浏览器继续使用 Web Locks 避免重复同步。

兼容读取 0.2.0 的 `<设备 UUID>.json`，不会修改旧文件。参与同步的各端应更新到 0.3.4；旧版不能读取新的快照文件名。并发文献修改采用较新版本；旧文献包与快照保留在远端，不自动删除。设备时间偏差超过一天会被拒绝，请校正时间后重试。

边界：文献包 180 MB、单个设备状态 2 MB、最多 100 个设备、2,000 篇文献与 2,000 份同步快照。达到快照上限会明确提示整理目录；本版没有自动清理云端历史。同步测试的运行方式见 [测试说明](testing.md)。

## 构建与签名

仅在自行编译时需要此步骤。以下是 Windows PowerShell 命令，在仓库根目录运行。先将 Node.js 22 或以上（含 npm）加入 PATH，并在 Android Studio 的 SDK Manager 安装 Android SDK Command-line Tools (latest)，按提示接受 SDK 许可。脚本会准备 Java 21、Android SDK 36 和 Gradle 所需组件；首次构建需联网。

```powershell
npm.cmd ci --prefix mobile --ignore-scripts
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_android_tools.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_android.ps1
```

默认从 `%LOCALAPPDATA%\Android\Sdk` 下读取命令行工具和许可；SDK 安装在其他位置时，给 `setup_android_tools.ps1` 追加 `-ExistingSdk "你的 SDK 完整路径"`。构建成功后 APK 位于 `output/Paperloom-0.3.4.apk`。工具安装在项目 `.android-tools` 内。Web 源码复制到 `mobile/www` 后再交给 Capacitor，排除站点的 APK 下载目录，避免 APK 嵌套打包。

交付 APK 为本地签名 release，包名 `app.paperloom.reader`、versionCode 8、versionName 0.3.4。签名材料在本机 `.android-tools/signing`，不进入源码包、APK 资源或站点。未来更新需继续使用同一签名，请独立备份该目录；丢失签名后无法覆盖安装原 App。

如果从源码构建，你的 APK 将使用自己的签名，不能直接覆盖仓库中用原签名安装的版本。运行自动化检查见 [测试说明](testing.md)。

实现参考：[Capacitor Android 插件](https://capacitorjs.com/docs/plugins/android)、[大文件桥接注意事项](https://capacitorjs.com/docs/apis/http)、[Android Keystore](https://developer.android.com/privacy-and-security/keystore)、[WebDAV RFC 4918](https://www.rfc-editor.org/rfc/rfc4918.html)。
