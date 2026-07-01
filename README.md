# AutoUnipus

> 老版 U校园(unipus)新视野大学英语 自动答题工具。Python + Playwright,
> 走 AI 兜底答题 + 人工答案库精确命中 + 提交后从 review 反扒标答自动入库。
> 自带 Web UI,可视化配置 / 实时日志 / 答案库管理。

## 项目说明

AutoUnipus 是一个面向老版 U校园页面的本地浏览器自动化研究项目。它通过 Playwright 控制本机浏览器,
在本地完成登录、页面识别、题型分发、日志展示、缓存管理和答案库维护。项目不会内置任何账号、密码、
课程数据、答案数据或第三方 API key。

适合用来学习:

- Playwright 浏览器自动化流程编排
- Flask + 原生前端构建本地 Web UI
- SQLite 缓存与本地 JSON 答案库管理
- OpenAI 兼容接口的多模型 fallback 调用方式
- 复杂页面 DOM 识别、弹窗处理和任务汇总

> 重要:本项目仅供学习、研究和个人自动化测试参考。请遵守所在学校、课程平台和相关服务条款,
> 不要用于考试、作业、刷课、绕过教学要求或其他不当场景。

## 功能概览

- Web UI 配置账号、课程链接、运行模式、题型开关、AI 参数和缓存路径
- 自动模式:按课程目录枚举必修入口并依次处理
- 辅助模式:手动打开页面后,按需触发当前页扫描
- 题型 handler:单选、多选、普通填空、选词填空、翻译 / 简答
- 答案来源链:SQLite 缓存 → 手动答案库 → AI 兜底
- review 反扒:提交后解析标准答案,通过严格条数校验后写入本地缓存
- SSE 实时日志、缓存统计、答案库快速录入 / 批量导入 / 清理脏数据

## 快速开始

```bash
git clone https://github.com/houyuhang915-sudo/AutoUnipus.git
cd AutoUnipus

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install

cp account.example.json account.json
python3 webui_launcher.py
```

启动后访问:

```text
http://127.0.0.1:5500/
```

Windows 用户可将 `python3` 替换为 `python`,虚拟环境激活命令通常为:

```powershell
.\.venv\Scripts\activate
```

## 隐私与本地数据

以下文件只应保存在本地,不应提交到公开仓库:

- `account.json`:真实账号、密码、课程链接、AI key
- `data/`:SQLite 答案缓存、手动答案库、调试 dump
- `log.txt` / `*.log`:运行错误日志
- `.env`:本地环境变量

仓库已通过 `.gitignore` 忽略这些路径。开源前可以用下面的命令再次确认:

```bash
git ls-files account.json data .env log.txt
git log --all -- account.json data .env log.txt
```

如果这些命令输出了历史提交,说明敏感信息曾经进入 Git 历史,需要先清理历史再公开。

## 工作原理

```
┌────────────────────── 答案获取链 ────────────────────────┐
│ 1. SQLite 缓存 (data/answers.db)            ← 优先          │
│      ↓ 未命中 / 答案数对不上                                │
│ 2. 人工答案库 (data/manual_answers.json)                   │
│    三类 key: exact_by_qid / exact_by_task /                │
│    fuzzy_by_title — 主要靠跑过的题自动入库 + 用户手动维护   │
│      ↓ 未命中 / 答案数对不上                                │
│ 3. AI 兜底(任意 OpenAI 兼容协议的 endpoint)                │
│    探测页面 input/textarea/radio 结构 → 告诉 AI 期望返回    │
│    多少条什么形式答案 → 解析 JSON 数组                      │
│    主模型空响应/失败时,按 fallback_models 顺序自动换模型重试 │
└────────────────────────────────────────────────────────────┘
                    ↓
┌─────────────── 进入题目页之后的判断 ─────────────────────┐
│ 看 DOM 是否有 input/textarea/radio/checkbox/select         │
│  · 0 个 → 阅读型(Preview/课文)                            │
│           滚动 + 停留 N 秒 → 找"完成/继续学习"按钮点掉      │
│  · ≥1 个 → 答题型,走上面的答案获取链 + handler 分发          │
└────────────────────────────────────────────────────────────┘
                    ↓
        题型 handler 分发(模拟人手键盘 + 反风控节奏)
   单选 / 多选 / 填空 / 选词填空 / 翻译 / 简答
                    ↓
       提交 → 完成弹窗解析:正确率 / 题数
       客观题 100% 时点"查看答案" → 严格扒标答
                    ↓
       严格校验扒到的条数 == 题数 才写缓存 + 同步进 manual_answers.json
                    ↓
            下一题(或下一个必修练习入口)
                    ↓
        课程跑完 → 控制台打印汇总:耗时 / 各题型分布 / 平均正确率 /
                   答案来源(cache/AI/manual 各命中多少)/ <100% 题列表
```

> ⚠️ 关于"逆向官方答案接口":之前实测 老版 U校园(`u.unipus.cn`)的 `/v3/answer/`
> `/v3/content/` 接口与 yuanarcsin / DMCSWCG 文档里的 schema 已经不一致,
> 不同的 AES key 候选 + JWT 自签都试过都不通。所以 **目前不再走任何逆向数据源**,
> 完全依赖 Manual + AI + review 反扒 这套组合。代码保留在 `core/sources/api_source.py`、
> `legacy_source.py`、`content_source.py`,但默认未注册到 resolver。

## 鲁棒性 / 反风控 / 防污染细节

- **键盘节奏**:填空/翻译题用 `keyboard.type(delay=25-60ms)` 模拟手敲,提交前 2-4 秒人类停顿
- **频繁弹窗冷却**:检测"操作过快/提交失败"自动关弹窗 + 60s 冷却
- **MutationObserver 弹窗杀手**:每个题目页注入持续监控,录音机/麦克风权限/网络错误等噪音弹窗冒一次自动点掉一次
- **答案数严格校验**:resolver 拿到答案后,要求条数**严格等于**页面真实题数(数 input/textarea/radio/checkbox/select),否则跳过该来源继续 fallback。能挡:
  - 缓存被部分覆盖坏(如 12 → 2)
  - AI 偶发只回了 3 / 5 条
  - 单选 review 把"字母 + 选项文本"双双抓回(5 题 → 8 条)
- **review 严格模式**:正确率 < 100% 时只信带 `rightAnswer/correctAnswer/referenceAnswer/standardAnswer/answer-key/answer-correct/answer-right` 类名的元素,或"正确答案: X"文本,或绿色文字(`getComputedStyle().color` 中绿值显著高于红/蓝)。只有 100% 才允许从 readonly input/checked radio 退一步兜底,避免把"我们刚提交的错答案"误存为标答
- **review 答案剥外层括号**:U校园 经常把标答渲染成 `(answer)` 形式,自动剥成 `answer` 再入库
- **harvest 严格条数等于**:扒到的标答数 ≠ 完成题数(`本次共完成 N 道题`)就不写缓存,只在日志列出参考标答给人工核对
- **主观题不缓存**:页面只有 textarea(纯论述/写作)时,AI 答案标记 `cacheable=False` 不写缓存,避免一次答错永久污染下一轮
- **AI 多模型 fallback**:中转/上游对特定 prompt 偶尔会"软拒答"返回 0 token。配置 `ai.fallback_models` 后,主模型空响应自动换下一个模型重试,直到拿到非空答案或全部用完
- **AI 题面砍半重试**:同一模型第一次空响应时,自动把题面截到前 1500 字再发一次(挡长 prompt 限流/审核)

## 安装

```bash
pip install -r requirements.txt
playwright install
```

只需第一次运行 `playwright install`(下载内置 Chromium)。已装 Edge / Chrome 的可省略。

第一次启动需要复制一份配置:

```bash
cp account.example.json account.json
# 用编辑器或 webui 填入账号密码 + AI key
```

## 启动

### Web UI(推荐)

```bash
python3 webui_launcher.py                    # 默认 5500,自动开浏览器
python3 webui_launcher.py --port 5500
python3 webui_launcher.py --no-browser
python3 AutoUnipus.py --webui                # 等价写法
```

> macOS 上的 5000 端口被 AirPlay Receiver 占用,默认改成了 5500。

WebUI 能做的事:
- 表单编辑账号/密码/课程链接,自动生成 `account.json`
- 勾选启用哪些题型 handler,做隔离测试
- **试运行模式**:填答案但不点提交,先肉眼校对
- **第一遍收割模式**(`harvest_first_pass`):没拿到答案也强行空提交,触发 review 扒标答进库,第二遍跑就 100% 命中
- **阅读型停留时长**:Preview / 课文这种页停留多少秒
- **AI fallback 模型链**:每行一个模型名,主模型空响应时按顺序重试
- 切换 自动 / 辅助 模式;辅助模式点"扫描当前页"按需触发
- 实时日志(SSE 推流)
- 答案缓存查看 / 清空
- **手动答案库面板**(下文详述)
- AI 连通测试(发一条最小请求验证中转站 + key + 模型可用)

### CLI

```bash
python3 AutoUnipus.py
```

直接读 `account.json` 跑对应模式。

## 配置 `account.json`

```jsonc
{
  "username": "",
  "password": "",
  "Automode": true,                // true=自动模式 false=辅助模式
  "Driver": "Edge",                // Edge | Chrome
  "class_url": [
    "https://u.unipus.cn/user/student/mycourse/courseCatalog?courseId=...&school_id=...&eccId=...&classId=...&coursetype=0"
  ],

  "handlers": [
    "single-choice",
    "multi-choice",
    "word-blank",
    "blank",
    "translation"
  ],

  "dry_run": false,                // 填答案但不点提交,方便校对
  "harvest_first_pass": false,     // 没拿到答案也强行提交,从 review 扒标答进库
  "reading_duration_sec": 8,       // 阅读型页面停留秒数(Preview / 纯课文)

  "ai": {
    "enabled": true,
    "base_url": "https://api.example.com/v1",   // 任意 OpenAI 兼容协议的 endpoint
    "api_key": "sk-xxx",
    "model": "your-model-name",
    "fallback_models": [           // 主模型空响应时按顺序自动换模型重试
      "another-model-1",
      "another-model-2"
    ],
    "timeout": 30
  },

  "cache": {
    "enabled": true,
    "path": "data/answers.db"
  }
}
```

辅助模式下 `class_url` 可以留空。`ai.fallback_models` 留空 = 不做模型 fallback,只对主模型做"题面砍半"重试。

> 部分中转站即便 `stream:false` 也可能强制返回 SSE,
> 已在 `ai_source.py` / `webui/server.py` 各内置一份 `_parse_sse_chunks` 兜底拼装,
> 同时也能识别"形式合法但 content 空"的响应(中转站偶发的 0 token 输出)。

## 必修练习的检测逻辑

```
1. 打开课程目录,等到 .icon-bixiu(必修标记)出现
2. 对每一行有 .icon-bixiu 的行容器,扫行内所有 icon-XXX 形式的可见 iconfont
   (排除箭头/搜索/设置等装饰图标 + 必修标记本身)
3. 给每个图标打 data-autounipus-idx="N" 序号
4. 按序号依次点击。回目录后重新打标(避免句柄失效)
```

这样一个 14 章左右的课会枚举出 30+ 个练习入口,**包括 Preview / Structure analysis & writing
等用 `icon-yulan` / `icon-bianji` / `icon-wenben` 等不同图标类的项**(老版只看 `icon-lianxi`
全漏)。每个入口进入后再用页面 input 数判断是阅读型还是答题型。

## 跑完一个课程的汇总

每个 `class_url` 跑完后日志末尾会打印类似下面这样的统计:

```
============================================================
📋 课程汇总: 新视野大学英语(第三版)读写思政数字课程2
============================================================
  耗时             20分56秒
  必修练习入口数   33
  · 答题型         11 (其中已提交 19)
  · 阅读型         14
  · 主观题         8

  本次平均正确率   100.0%   (满分 11 题 / 部分对 0 题 / 失败 0 题)

  答案来源:        cache 命中 12 题 · AI 调用 7 题 · 人工答案库命中 0 题

  ⚠ 以下题目正确率 < 100% 或有异常,可能需要人工核对:
    · 练习 28/33  task=u5g246   83%   行: 5-3 Text A: Language focus
============================================================
```

> 平均正确率只统计**有客观题评分**的题(主观题不算入)。

## WebUI 答案库面板

- **快速录入**:输入 task_id / qid / 标题 + 一行一个答案,直接合并到 `manual_answers.json`
- **批量导入**:粘贴 OCR / 知乎 那种"标题 + 编号答案"块,空行分隔。自动剥 `1.`/`1)`/`①`/`一、` 前缀
- **从缓存补回**:把 SQLite 缓存里的答案一键同步到 `exact_by_task`(只补缺,不覆盖手填)
- **清掉某题的脏数据**:输入 task_id 一键删 SQLite + manual lib 双侧条目,**典型场景**:发现某题被错误答案锁住时(比如 AI 把主观题答成单词永久缓存了),清掉后下次跑会重新查询
- **直接编辑 JSON**:高级模式,改完保存

`manual_answers.json` 结构:

```jsonc
{
  "exact_by_qid":   { "<32 位 hex qid>": ["A", "B", "C"] },
  "exact_by_task":  { "u2g72":           ["successful", "cooperative", ...] },
  "fuzzy_by_title": { "1-2 Text A Words in use": ["...", "..."] }
}
```

匹配优先级:`exact_by_qid` > `exact_by_task` > `fuzzy_by_title`(大小写无关,去标点)。
**保存后无需重启,运行中按 mtime 自动重读。**

## 题型支持

| 题型 | 状态 | 说明 |
|---|---|---|
| 单选 | ✅ | `input[type=radio][value=X]`,容器必须真有 radio 才认领(避免抢 textarea) |
| 多选 | ✅ | 兼容 `"A,B,C"` / `"ABC"` / `["A","B","C"]`;同样要求容器有 checkbox |
| 填空(普通) | ✅ | `keyboard.type` 模拟手敲,带强力清空(移 readonly + value='' + Backspace) |
| 选词填空 | ✅ | 适配 `<select>` 下拉与 textInput 两种 |
| 翻译 / 简答 | ✅ | textarea / contenteditable,同样人类节奏 |
| 主观题(无评分) | ⚠️ AI 兜底 | 不写缓存(避免错答案永久锁),每次都重新查询 |
| 阅读型(无 input) | ✅ | 滚动 + 停留 N 秒 + 找"完成"按钮点掉 |
| 听力 | ✅ 仅人工答案 | 标题含 listening/听力 等关键词时跳 AI(AI 没音频),只走 manual lib |
| 拖拽题 / 视频 / 学习生词 | ❌ | TODO |

## 项目结构

```
AutoUnipus/
├── AutoUnipus.py                CLI 入口(支持 --webui)
├── webui_launcher.py            WebUI 启动器
├── account.example.json         配置模板(复制成 account.json 后修改)
├── account.json                 [.gitignore] 真实账号配置
├── requirements.txt
├── data/                        [.gitignore] 运行时数据
│   ├── answers.db               SQLite 答案缓存
│   ├── manual_answers.json      人工答案库
│   └── debug/                   review 扒不到答案时自动 dump 的 HTML
├── core/
│   ├── config.py                配置加载 / 校验 / 序列化
│   ├── logger.py                日志(含 webui pub/sub)
│   ├── runner.py                主流程编排:必修枚举 + 阅读分类 + 答题 + harvest + 汇总
│   ├── crypto/                  AES-128-ECB / JWT HS256(逆向源用,目前未启用)
│   ├── unipus/
│   │   ├── login.py             登录流程
│   │   ├── page_info.py         提取 courseInstanceId / taskId / openId
│   │   ├── api_client.py        答案接口客户端
│   │   └── sniffer.py           网络抓包工具(给 ContentAPI 源用,目前未启用)
│   ├── sources/
│   │   ├── manual_source.py     [启用] 人工答案库,mtime 自动重读
│   │   ├── ai_source.py         [启用] OpenAI 兼容 + 结构感知 + SSE 拼装 + 多模型 fallback
│   │   ├── api_source.py        [未启用] 老版逆向接口
│   │   ├── legacy_source.py     [未启用] DMCSWCG 暴力反推
│   │   ├── content_source.py    [未启用] /v3/content AES 解密
│   │   └── sniff_source.py      [未启用] 抓包扒明文
│   ├── cache/store.py           SQLite 缓存
│   └── handlers/                题型适配器(同上一节支持的题型)
├── webui/
│   ├── server.py                Flask 后端 + SSE 日志 + 答案库 API
│   └── static/{index.html,app.js,style.css}
└── res/
    └── fetcher.py               [已弃用] v1 暴力反推方案,保留作参考
```

## 已知边界

1. **登录图形验证码 / 滑块需手动**。
2. **U校园 review 模式只读**:发现某题正确率 < 100% 时,review DOM 里的 input 是只读副本,
   没法直接覆盖重做。脚本不再尝试"再做一次"流程,只把扒到的标答存进库,**下次跑同一题直接 100%**。
3. **主观题(其他主观题... 暂无评分)**没有机器评分,review 也不展示标答。AI 答完不写缓存,
   每次都重查;如果 AI 答得好可在 webui 手动录入对应 task_id 固化。
4. **AI 中转间歇性软拒答**:返回 200 + `choices: []` + `completion_tokens: 0`(没报错,但内容空)。
   常见在长 prompt / 教材课文上。配置 `ai.fallback_models` 链上 2-3 个备用模型可以基本兜住,
   极端情况配合 `harvest_first_pass=true` 直接靠空提交触发 review 扒标答。
5. **重复触发风控**(`操作过频繁`):脚本会自动关弹窗 + 60s 冷却。如果反复触发,把
   `core/handlers/blank.py` / `translation.py` 里的 `delay=25-60ms` 调更大,或在配置里
   把 handler 数减少分批跑。
6. **U校园 周期性更新 DOM**:课程目录的图标类、review 页的标答 class 名都可能变。
   如果某天扒不到答案,严格 harvest 失败时会把 review 页面 outerHTML 自动 dump 到
   `data/debug/review_<task_id>_<时间戳>.html`,贴出来对照可在 `core/runner.py` 的
   `_EXTRACT_REVIEW_STRICT_JS` / `_TAG_REQUIRED_EXERCISES_JS` 里加 selector。

## 致谢

- [Duster-Cule/UnipusHelperPro](https://github.com/Duster-Cule/UnipusHelperPro) — 题型清单与流程参考
- [yuanarcsin/unipus_auto](https://github.com/yuanarcsin/unipus_auto) — 老版本逆向方案(已实测在我的部署上不通,代码保留作参考)
- [DMCSWCG/UnipusGetAnswer](https://github.com/DMCSWCG/UnipusGetAnswer) — 暴力反推思路(同上)

## 免责声明

本项目仅供学习、研究和个人自动化测试参考,不鼓励也不支持任何违反学校规定、课程平台规则、
考试 / 作业要求或服务条款的使用方式。使用者应自行承担运行本项目带来的账号、课程记录、
平台风控、数据准确性和合规风险。

项目不会主动收集、上传或共享账号密码、API key、课程链接、答案缓存或日志数据。所有运行数据默认保存在本机,
请不要把 `account.json`、`data/`、日志文件或任何包含个人信息的截图提交到公开仓库。
