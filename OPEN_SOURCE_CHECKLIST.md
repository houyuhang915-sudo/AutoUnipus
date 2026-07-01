# AutoUnipus 开源前检查清单

这份清单用于把本地项目整理成适合上传 GitHub 的公开仓库。建议发布前逐项核对。

## 1. 敏感信息

- 确认 `account.json`、`.env`、`data/`、`log.txt` 不在 Git 跟踪列表中。
- 确认 `account.example.json` 只保留空账号、空密码、空 API key 和示例课程链接。
- 如果曾经提交过真实账号、API key、答案缓存或课程数据，需要用 `git filter-repo` 或 BFG 清理 Git 历史后再公开。

检查命令:

```bash
git ls-files account.json data .env log.txt
git log --all -- account.json data .env log.txt
```

## 2. 仓库内容

- 保留源码、配置模板、README、LICENSE、依赖文件和必要资源。
- 不上传虚拟环境、缓存、运行日志、个人课程数据、浏览器调试 dump。
- 当前仓库里 `QRcode.jpg` 已处于删除状态；如果它不是项目运行必需资源，建议保持删除。

## 3. 文档

- README 应说明项目用途、安装方式、启动方式、配置项、项目结构、已知边界和免责声明。
- Web UI 默认端口应与代码保持一致，目前为 `5500`。
- 对外发布时建议在仓库描述里写清楚适用范围，避免用户误以为这是官方工具。

## 4. 合规与边界

- 明确声明项目仅供自动化、浏览器控制、课程平台接口研究和个人学习环境测试使用。
- 不建议把本项目用于违反学校、课程平台、考试或作业规则的场景。
- 不收集、不上传用户账号、密码、API key、课程数据或答案缓存。

## 5. 发布前验证

```bash
python3 -m compileall AutoUnipus.py core webui webui_launcher.py
python3 webui_launcher.py --no-browser
```

启动后访问:

```text
http://127.0.0.1:5500/
```

## 6. GitHub 建议

- 新建仓库后先推送源码，再在 GitHub 上补充 Topics: `python`, `playwright`, `flask`, `automation`。
- 如果希望别人参与，后续可以再补 `CONTRIBUTING.md` 和 Issue 模板。
- 第一个 Release 建议打 `v0.1.0`，说明这是个人研究项目，平台页面变化可能导致功能失效。
