# 把整套 Paperloom 首次发布到 GitHub

如果你是来读论文的，请看[使用手册](../MANUAL.md)。这页只给维护者使用。

## 先取得可以上传的目录

如果拿到的是本次生成的 `Paperloom-GitHub.zip`，**解压后进入含 `README.md`、`paperloom`、`web` 和 `mobile` 的那一层**，直接进入下一步。

如果在原开发目录操作，请先运行：

```powershell
python scripts/package_release.py --github-dir output/Paperloom-GitHub
cd output/Paperloom-GitHub
```

导出只复制[分发清单](../scripts/github-files.txt)内的文件，并生成同名 ZIP。目录已存在时不会覆盖，请换一个新目录名。原工作目录中的 `web` 可能有独立的部署历史；导出后它是普通源码目录。不要直接上传原工作目录、自己的论文、模型密钥或签名文件。

## 首次推送

1. 在 **GitHub** 新建仓库，选定账号、仓库名和公开性，**不要**勾选自动生成 README、`.gitignore` 或许可证。复制新仓库的 HTTPS 地址。
2. 在刚才的**干净目录**打开 PowerShell。用下面的命令创建本地提交。如果 Git 提示缺少身份，用 `git config user.name "你的名字"` 和 `git config user.email "你的提交邮箱"` 设置后再提交。

   ```powershell
   git init -b main
   git add .
   git update-index --chmod=+x mobile/android/gradlew
   git status --short
   git commit -m "Initial Paperloom release"
   ```

3. 把第一行示例 URL 换成你复制的仓库地址，按 Git 的登录提示完成推送：

   ```powershell
   git remote add origin https://github.com/OWNER/REPO.git
   git push -u origin main
   ```

4. 打开仓库，确认能直接浏览 `web/dist/app.js`、`mobile/android/` 和 `MANUAL.md`。下载仓库 ZIP 后即可按 README 试读。之后在这个仓库正常提交和推送即可，不要重复执行首次初始化命令。
