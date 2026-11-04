# 德语智读 · 实施任务与验收标准

> 依据 `design.md` 拆解。状态标记：`[ ]` 待办 · `[~]` 进行中 · `[x]` 完成。
> 验收标准均为**可执行、可观察**的检查，最终在 `tests/` 与人工清单中逐条验证。
>
> **状态：8 个阶段全部完成（2026-11-02）。** 自动化 111 项全绿；真实 DeepSeek 流式解析/追问冒烟通过；桌面/1920/手机 390 视口 Playwright 巡检无 console error。v1.1.0、v1.2.0 追加需求、v1.2.1 界面修复、v1.2.2 缓存清理、v1.3.0 模型预设、v1.4.0 OpenAI/全 Prompt 与 v1.4.1 追问/思考/导读修复均已完成，见文末。具体验收记录见文末「验收记录」。

## 项目结构（设计稿对照）

```
german_reader_ai/  →  当前目录 AIReader/
├── main.py              # FastAPI 入口、路由、后台任务
├── database.py          # SQLite 连接、建表、CRUD
├── parser.py            # PDF/TXT/EPUB 解析、断词修复、智能分章
├── ai_service.py        # DeepSeek 搜索/Chat、Gemini、Prompt 编排
├── desktop.py           # pywebview 桌面壳（可选）
├── uploads/             # 原始书籍文件
├── static/
│   ├── index.html       # 三栏工作台 / 书架 / 弹窗
│   ├── style.css        # 羊皮纸/暗黑主题、翻页、高亮、响应式
│   └── app.js           # 阅读器、划词、缓存、语音、多轮对话
├── tests/               # pytest 单元 + 集成测试
├── reader_data.db       # 首次启动自动生成
└── README.md
```

## 阶段 0 · 工程脚手架

**任务**
- [x] 建立目录（`static/`、`uploads/`、`tests/`）与虚拟环境、`requirements.txt`。
- [x] `database.py` 首次启动自动建库建表、WAL、外键、索引。
- [x] `GET /api/health`、静态页面挂载、启动横幅（含局域网 IP）。

**验收**
1. `pip install -r requirements.txt` 零报错；`.venv` 可直接运行。
2. 删除 `reader_data.db` 后启动，自动生成文件且恰好包含 `books / chapters / annotations / chat_messages / settings` 五张表，`PRAGMA foreign_keys=ON`、`journal_mode=wal` 生效。
3. `GET /api/health` 返回 200 与 `status=ok`、版本号、模型、密钥状态。
4. 浏览器打开 `/` 返回主界面；`/static/app.js` 可访问；服务监听 `0.0.0.0:8000`。

## 阶段 1 · 数据持久化

**任务**
- [x] `database.py`：连接上下文、初始化/迁移、五表 CRUD、级联删除、进度更新、设置读写、缓存查询。
- [x] 复杂读模型：书籍列表含章节数/词数/进度；章节载荷含标注 + 追问；全书搜索。

**验收**
1. 书籍/章节/标注/对话/设置全 CRUD 单测通过；删除书籍级联清空章节、标注、对话，原始文件也被删除。
2. 标注缓存查询 `(chapter_id, paragraph_index, selected_text)` 命中返回原解释与历史消息。
3. 同一数据库重复 `init_db()` 幂等，不丢数据；并发 8 线程写入无 `database is locked`。
4. 重启进程后书籍、进度、标注、追问、设置全部还原。

## 阶段 2 · 文档解析

**任务**
- [x] TXT 编码探测（UTF-8/UTF-8-BOM/Latin-1/CP1252），保留 `ä ö ü ß`。
- [x] PDF（PyMuPDF）文本 + 书签目录 + 元数据；无书签时正则分章；短篇 3000 词自动分节。
- [x] EPUB（EbookLib + BeautifulSoup）按 spine/TOC 分章、元数据。
- [x] 德语跨行连字符修复：`Wissen-\nschaft → Wissenschaft`；专有名词 `Goethe-\nInstitut → Goethe-Institut`。
- [x] 段落结构化、清洗页码/多余空白、标题/作者识别、词数统计。

**验收**
1. `Wissen-\nschaft` 拼接、`Goethe-\nInstitut` 去换行保留连字符、软连字符 `\u00ad` 清除；不会误伤 `- ` 列表与破折号。
2. 同一份德语含变音符文本以 UTF-8、Latin-1、CP1252 三种编码保存，解析结果一致且无乱码。
3. 带 3 条一级书签的 PDF 被切成 3 章，章题与书签一致；无书签 PDF 按 `Kapitel 1`/`I.` 正则切分；纯 TXT 短篇按 3000 词切节。
4. EPUB 按章节文档切分且标题正确，段落无 HTML 残留。
5. 无文本层扫描 PDF 返回中文错误提示（提示需要 OCR），不产生脏数据。

## 阶段 3 · AI 服务

**任务**
- [x] DeepSeek Chat（OpenAI 兼容）+ Gemini `generateContent` 双模型；超时、重试、错误中文映射。
- [x] DeepSeek 搜索 `POST /v1/tools/search`，按 `rerank_score` 过滤摘要；失败自动降级为模型知识生成，不阻断导入。
- [x] 全书背景三件套（概要/作者/文化背景）、章节导读 + 3 个德语细节问题、划词精讲、多轮追问、书籍识别。
- [x] 自定义划词 Prompt（`{selected_text}` / `{context_sentence}`）实时生效；JSON 容错解析。

**验收**
1. Mock HTTP：搜索返回 2 条高分摘要时进入背景 Prompt；搜索 404/超时时导入仍成功，`source=model_knowledge` 可观测。
2. 缺 Key 调用返回 400 与中文可操作提示；401/429/超时分别映射为明确文案；旧缓存不被破坏。
3. 章节导读稳定得到 1 段概要 + 恰好 3 个问题；非 JSON 回复也能降级提取，不让接口 500。
4. 修改设置中的 Prompt 后，下一次划词请求携带新模板；缺少占位符时自动补上下文，不报错。
5. `POST /api/settings/test` 对真实 Key 返回延迟与模型回复；错误 Key 返回可读失败原因。

## 阶段 4 · API 与业务流程

**任务**
- [x] 导入（上传→解析→落库→后台生成背景 + 第 1/2 章导读）。
- [x] 书库、书籍详情、章节正文/目录、进度、删除、按需导读、下一章预热、背景重生成。
- [x] 划词缓存/强制重生成、追问、清空对话、删除高亮、全书搜索、设置、系统信息。
- [x] 参数校验、404/400 中文错误、后台任务状态、并发去重。

**验收**
1. 上传 TXT 返回 200 与 `book_id`；立即可读书，后台任务稍后把 `book_summary/author_bio/cultural_context` 与第 1、2 章导读写入。
2. 打开第 N 章时请求 `prepare_guide(N+1)` 只触发一次；并发生成有锁，不重复写库。
3. 划词接口：未命中调用 AI 并新增高亮；命中直接返回且 `cached=true`；`force_regenerate=true` 更新解释；偏移越界/不匹配返回 400。
4. 追问按序落库，AI 上下文包含原句、首次解释与历史；接口返回新增 user/assistant 两条。
5. 未登录局域网访问可用；错误响应统一 `{"detail": "中文提示"}`；越权/不存在资源 404。
6. OpenAPI `/docs` 可打开，所有路由有 summary 与请求体示例。

## 阶段 5 · 阅读器前端

**任务**
- [x] 书架视图：封面、进度、继续阅读、删除、导入。
- [x] 三栏工作台：TOC/背景/搜索、正文+导读卡、AI 面板；移动端抽屉。
- [x] 滑动/翻页双模式：CSS 多列分页、键盘 ←/→、按钮、触摸滑动、页码同步。
- [x] 字号 A-/A+、羊皮纸/暗黑主题、进度持久化、刷新恢复。
- [x] 空状态、骨架屏、Toast、确认弹窗、快捷键、无障碍焦点。

**验收**
1. 1440×900 三栏比例协调；≤900px 左右栏变抽屉且有遮罩；380px 宽度可正常阅读与翻页。
2. 翻页模式页数 = 实际列数，窗口缩放/字号变化后重排并保留阅读比例；无文字被裁剪；翻页动画顺滑。
3. 键盘 ←/→、底部按钮、触屏左右滑动均可翻页；滑动模式滚动到底自动定位下一页按钮状态。
4. 字号/主题/模式/上次书籍/章节/页码刷新后恢复；离线（无外网）时静态页面与已读内容仍可用。
5. 空书架、导入中、AI 生成中、生成失败均有明确视觉反馈，无白屏。

## 阶段 6 · 划词与 AI 私教

**任务**
- [x] 选区 offset 计算（段落、起止、上下文）、浮动工具条、自动解析开关。
- [x] 暖黄高亮渲染、点击高亮 0 请求复现、重新生成。
- [x] Markdown 安全渲染、追问多轮、语音识别（zh-CN/de-DE）、德文 TTS、复制。
- [x] 跨段选择、空白选择、超长选择等边界处理。

**验收**
1. 对同一段落中任意位置划词，`start_offset/end_offset` 与数据库段落文本 `slice` 完全一致；包含高亮时偏移仍准确。
2. 新建解释后立即出现暖黄高亮；刷新或重启后高亮恢复；点击高亮不发 AI 请求，直接显示原解释与历史追问。
3. 重新生成替换解释但保留追问；追问消息按序展示并持久化；清空对话生效。
4. 含 `<script>`、`**粗体**` 的 AI 回复被安全渲染：标签转义、Markdown 生效，无 XSS。
5. Chrome/Edge 语音输入可切换中/德；不支持时麦克风按钮给出提示且不报错；TTS 按德语朗读选中词。
6. 跨段选择给出中文提示且不产生错误标注；长度 1–500 字符限制生效。

## 阶段 7 · 设置、桌面与交付

**任务**
- [x] 设置弹窗：模型切换、Key 掩码、Prompt 编辑/重置、测试连接、阅读偏好、局域网地址复制。
- [x] `desktop.py` pywebview 壳 + 浏览器回退；`start.bat` 一键启动；首次自动读取 `DeepSeek API Key.txt`。
- [x] README：安装、启动、Android 访问、API 使用、数据与隐私、故障排查。

**验收**
1. 设置保存后立即生效（切 Gemini 后下一次调用即走 Gemini）；`GET /api/settings` 永不返回完整 Key。
2. 测试连接按钮显示成功延迟或中文失败原因；Prompt 重置恢复默认模板。
3. `start.bat` 双击能启动并自动打开浏览器；pywebview 已安装时打开独立窗口，未安装时无报错回退。
4. 手机同一 Wi-Fi 访问 `http://<局域网IP>:8000` 可完成导入、阅读、划词、追问。
5. README 步骤在干净环境可复现；含常见问题（端口占用、Key、扫描 PDF、语音权限）。

## 阶段 8 · 测试与最终验收

**任务**
- [x] pytest：解析器、数据库、API（Mock AI）、缓存与并发。
- [x] 构造 TXT/PDF/EPUB 样例书并跑通真实导入。
- [x] 真实 DeepSeek Key 冒烟：背景、导读、划词、追问。
- [x] 浏览器控制台零报错；响应式与双阅读模式截图人工检查。

**验收**
1. `pytest -q` 全绿，覆盖阶段 1/2/3/4 的关键验收点。
2. 样例 PDF/EPUB/TXT 导入后章节、段落、词数、目录均正确。
3. 使用项目内真实 Key 成功生成一次背景与一次划词解释，并写入数据库（不提交明文到代码）。
4. 页面无 console error；所有按钮有响应；服务重启后阅读位置与高亮一致。

---

## 设计稿之外的增强（不影响验收，追求体验）

- 全书搜索与段落跳转；阅读进度百分比；章节词数/预计阅读时间。
- 划词工具条复制/朗读；AI 回复 Markdown 渲染；追问历史一键清空。
- 后台任务状态可观测（书籍背景、章节导读），失败可重试。
- 局域网二维码（可选）、PWA manifest、键盘快捷键、减少动画偏好。
- 全链路中文错误提示与日志落盘。

---

## 验收记录（2026-11-02）

### 自动化测试

```
$ .venv\Scripts\python -m pytest -q
111 passed in 15.00s
```

覆盖：断词四种形态、TXT 三种编码、PDF 书签/正则分章/扫描件、EPUB、3000 词分节、五表 CRUD 与级联、并发写入、导入 API、标注缓存/强制重生成/追问、设置脱敏、搜索、二维码、错误映射、DeepSeek/Gemini 请求形状。

### 真实 DeepSeek 冒烟

* 启动横幅与 `GET /api/health` 正常；
* `POST /api/books/import` 导入示例 TXT：5 章、1334 词、章节/段落正确，`Wissen-\nschaft` 修复为 `Wissenschaft`、`Goethe-\nInstitut` 保持连字符；
* 全书背景生成成功，`jobs.background.result.source = search+model`，网络检索接口可用；
* 前两章导读自动生成，每章 1 段概要 + 3 个问题；
* 划词 `verhaftet` 生成 Markdown 精讲；再次选择命中缓存；强制重生成成功；
* 追问「为什么这里用 wurde，而不是 ist verhaftet？」得到多段语法回答并持久化；
* 设置页「保存并测试连接」返回 `连接成功 · deepseek-chat · 约 1.6–2.5 s`。

### 浏览器巡检（Playwright + 本机 Edge）

* 1440×900、1920×1080、390×844 三种视口；`console error = 0`；
* 书架、三栏阅读、目录/背景/搜索、导读折叠与重生成、翻页/滑动切换、键盘翻页、页数、字号重排；
* 点击已缓存高亮：网络面板中 **没有** `/api/annotate` 请求；
* 新建划词、删除划词、清空追问、确认弹窗、导入弹窗文件校验与完整导入后删除；
* 设置四个标签、真实连接测试、JSON 设置持久化；
* 暗黑主题切换与持久化；
* XSS 注入测试：`<script>`、`<img onerror>`、`javascript:` 链接全部被转义，未执行、未生成危险 DOM；
* 手机抽屉、遮罩、AI 面板、设置弹窗、翻页模式均正常。

### 已知限制（不影响主流程）

* pywebview 桌面壳的代码与依赖清单已提供，但当前环境未安装 pywebview，因此未做真实窗口启动；浏览器模式为默认且完整可用。
* Gemini 仅通过单元测试验证请求/响应形状（没有第二家真实 Key）；DeepSeek 已真实跑通。
* 浏览器语音识别在 headless 环境无法验证；代码已处理不支持、无权限、局域网 HTTP 限制，并给出中文提示。
* Android 真机未接入，使用 390px 移动端仿真 + 局域网二维码接口验证；实际端口/防火墙因网络环境而异。
* 「自动识别书名」只在元数据缺失且作者为空时触发，避免覆盖人工输入。

---

## 追加需求（v1.1.0，2026-11-02）

### 1. 模型思考开关与强度

- [x] 设置 → AI 模型新增「模型思考」开关与「低 / 中 / 高 / 最高」四档强度。
- [x] DeepSeek 使用 `thinking: {type: enabled/disabled}` + `reasoning_effort`；Gemini 2.5 使用 `thinkingBudget`，Gemini 3/4 使用 `thinkingLevel`。
- [x] 设置持久化到 `settings`（`thinking_enabled` / `thinking_effort`），API 返回并支持热更新。
- [x] 不支持 thinking 参数的模型/网关自动降级一次，并缓存能力，避免反复失败。
- [x] AI 面板顶部显示当前模式（例：`DeepSeek · deepseek-flash · 思考·中`；关闭时为「直答」）。
- [x] 连通性测试结果显示当前思考模式。

**验收**：`POST /api/settings {"thinking_enabled": false, "thinking_effort": "low"}` 后重新读取一致；真实 DeepSeek 调用 payload 带正确 `thinking`/`reasoning_effort`；关闭思考时请求包含 `{"type":"disabled"}`；设置页切换后 AI 徽标立即更新；pytest 覆盖 payload、降级与持久化。

### 2. 章节导读字号

- [x] 导读概要 `13.5px → 15.5px`，探究问题 `12.5px → 14.5px`，行高与题号同步放大。
- [x] 手机端 15px / 14px，窄屏 13.5px；翻页模式自动重新分页。

**验收**：1440/390 截图可读；计算样式为 15.5px/14.5px；翻页模式页数随字号重算，无文字裁切。

### 3. 修复「只显示思考过程」

- [x] 严禁把 DeepSeek `reasoning_content` 当作回答；只返回 `content`。
- [x] 思考开启时提高输出上限（DeepSeek 至少 8192 tokens，Gemini 预算 + 回答）；若 `content` 为空、或 `finish_reason=length/MAX_TOKENS`，自动关闭思考重试一次。
- [x] Gemini 过滤 `part.thought=true` 的思考片段，只拼接正式回答。
- [x] 重试后仍只有思考时返回明确中文错误 `reasoning_only`，不再把思维链展示给用户。
- [x] 真实 DeepSeek 划词“verleumde”在思考·中模式下生成 699 字正式解析，无思考痕迹。

**验收**：pytest mock 覆盖「首轮仅 reasoning → 自动关闭思考重试成功」「两轮都仅 reasoning → 明确报错」「Gemini thought part 过滤」「thinking 参数不支持降级」；真实 DeepSeek 思考调用返回最终回答。

---

## 追加需求（v1.2.0，2026-11-02）

### 1. 流式输出

- [x] 新增 `POST /api/annotate/stream` 与 `POST /api/annotations/{id}/chat/stream`，使用 SSE（`text/event-stream`）逐段输出。
- [x] DeepSeek 使用 `stream: true` 解析 `delta.content`；Gemini 使用 `:streamGenerateContent?alt=sse`，过滤 `thought` 片段。
- [x] 流式路径保留思考开关、强度、输出上限、参数降级与“仅思考 → 关闭思考重试”逻辑。
- [x] 前端使用 `fetch` + `ReadableStream` 解析 SSE；解析与追问都逐字渲染；缓存解析也通过流返回。
- [x] 流内错误使用 `event: error`，不会把 HTTP 200 的半截内容写入数据库。

**验收**：真实 DeepSeek 划词流式首包 9.0s、341 个 delta、10.8s 完成；真实追问流式 3.1s 完成；curl/requests 可逐行读取；pytest 覆盖流式成功、缓存流、追问流与错误事件。

### 2. 精简界面

- [x] 移除右侧「AI 私教」标题栏（含模型徽标与关闭按钮）；空状态、选中卡片直接贴顶。
- [x] “当前选中”卡片不再展示上下文句子，只保留选中词与朗读/重新生成/删除。
- [x] 移除“AI 解析”标签；保留“可滚动”“收起/展开解析”控制。
- [x] 移除章节标题上方的 `KAPITEL xx · AUTHOR` 眉题。
- [x] 移除空状态下的“划选/追问/🎙”三条提示。
- [x] 移动端抽屉改用顶部按钮或遮罩关闭。

**验收**：`.ai-head`、`#selection-context`、`.explanation-label`、`.chapter-eyebrow`、`.ai-tips` 在 DOM 中均为 0；1440/390 截图无布局塌陷；无 console error。

### 3. 滚轮翻页

- [x] 翻页模式下监听阅读区 `wheel`，累计滚动阈值 32px，360ms 冷却，触控板小幅滚动也能触发。
- [x] 保留 `Ctrl + 滚轮` 浏览器缩放；滑动模式不拦截滚轮。
- [x] 翻页后同步页码、进度条与位置保存。

**验收**：Playwright `mouse.wheel(0, 700)` 后页码从 `第 1 / 4 页` 变为 `第 2 / 4 页`。

### 4. 按住空格语音输入

- [x] 阅读区，或空输入框内，按住空格开始 `SpeechRecognition`，松开停止；输入框已有文字时按空格仍是正常空格。
- [x] 识别结果写入追问输入框，并自动聚焦；松开后 `Enter` 直接发送。
- [x] 无选区、无选区面板或浏览器不支持时不拦截。
- [x] 对话框增加说明「按住空格语音输入 · 松开后 Enter 发送」。

**验收**：注入模拟 `SpeechRecognition` 后，按住/松开空格得到 `Hallo Welt`，焦点为 `#chat-input`；再按 Enter 成功创建用户消息并收到流式回答；无 console error。

---

## v1.2.1 界面修复（2026-11-02）

- [x] 用户消息气泡不再被 HTML 模板缩进/换行撑大；内容包进 `.chat-text`，仅正文保留换行。
- [x] AI 回复补上 `.markdown` 容器，列表 marker 缩进到气泡内部（`padding-left: 1.7em`）；输入框从 46px 收紧到 42px，最大高度 96px。
- [x] 发送后释放输入框焦点；点击 AI 讲解/追问区也会释放焦点；`#explanation` 可聚焦，因此在 AI 讲解区按住空格可正常触发语音，不再被 Windows 输入法截获。
- [x] 空输入框内仍可直接按住空格语音；已有文字时空格保持正常输入。
- [x] 阅读界面左栏移除「+ 导入新书」按钮；导入入口只保留书架页/空书架/导入弹窗，不再出现在阅读器。

**验收**：559px 窄屏下短消息用户气泡高度 `135.6px → 56px`；AI 列表 `ul` 左内边距 20.4px，圆点在气泡内；发送后 `document.activeElement` 为 `BODY`，点击 AI 回复后仍为 `BODY`；注入模拟识别后按住空格得到 `Hallo Welt`。阅读器 DOM 中 `#reader-import` 数量为 0，书架的导入按钮仍可用。

---

## v1.2.2 缓存与 Prompt 维护（2026-11-02）

- [x] 删除全部划词解释/高亮与追问记录：`annotations = 0`，`chat_messages = 0`。
- [x] 清空章节导读与全书背景：`is_guide_ready = 1` 的章节为 0，`book_summary` / `author_bio` / `cultural_context` 均为空。
- [x] 清空 DeepSeek / Gemini API Key；确认环境变量 `DEEPSEEK_API_KEY` 未设置；按用户确认直接删除 `DeepSeek API Key.txt`，避免重启自动回填。
- [x] 执行 SQLite WAL checkpoint 与 `VACUUM`，清理旧值所在页；把当前设置中的划词 Prompt 写入 [`prompts.DEFAULT_SELECTION_PROMPT`](prompts.py#L11)（618 字符，逐字一致）。
- [x] 前端普通 API 请求增加 `cache: "no-store"`，并将静态资源版本升至 v1.2.2。

**验收**：数据库统计为 `books=1, chapters=5, annotations=0, chat_messages=0`；`has_deepseek_key=false`、`has_gemini_key=false`；Key 文件不存在；当前 Prompt 与默认 Prompt 完全相等；`node --check` 与 **99 passed** 通过；服务重启后缓存没有自动回填。

---

## v1.3.0 模型预设更新（2026-11-04）

- [x] 按 2026 官方文档更新 DeepSeek 预设：`deepseek-flash`（默认，1M，支持图像）、`deepseek-v4-pro`（旗舰推理，不支持图像）。
- [x] 更新 Gemini 预设：`gemini-3.8-flash`（默认）、`gemini-4-argon`、`gemini-3.5-flash-lite`。
- [x] 设置页把自由文本模型名改为「官方预设下拉 + 自定义兼容模型」；自定义模型才显示输入框，并显示上下文/图像/思考限制提示。
- [x] 默认值与 API 缺省值同步更新；旧 `deepseek-chat`、`deepseek-reasoner`、`gemini-2.5-flash` 等在启动时迁移到新预设，自定义网关名保持不变。
- [x] Gemini 3.8/4 继续使用 `thinkingLevel`（关闭思考时最低到 low）；DeepSeek 继续使用 `thinking` + `reasoning_effort`。

**验收**：设置页默认显示 `deepseek-flash` 与 `gemini-3.8-flash`；切换到 `deepseek-v4-pro`、自定义 `my-gateway-model` 后保存并重开仍保持；恢复官方预设后截图无 console error；旧模型迁移测试和当前默认值测试通过；pytest **101 passed**。

---

## v1.4.0 OpenAI 兼容与全 Prompt 自定义（2026-11-04）

- [x] 新增第三种供应商 **OpenAI 兼容**：可配置 API Key（可留空）、Base URL、模型名；按标准 `POST /chat/completions` 调用，支持非流式与 SSE 流式。
- [x] OpenAI 兼容实现发送可选顶层 `reasoning_effort`（v1.4.1 起与统一思考面板联动）；遇到 `max_tokens`、`response_format` 等可选参数不兼容时自动降级重试。
- [x] 设置页改为供应商分区：选择 DeepSeek 只显示 DeepSeek Key/模型，选择 Gemini 只显示 Gemini，选择 OpenAI 只显示 OpenAI Key/Base URL/模型；思考面板在三家供应商下共用。
- [x] 删除「连接与数据」最底部的版本/前端/解析引擎/API 文档方框。
- [x] 新增 9 个可编辑 Prompt：划词、章节系统、章节用户、全书背景系统、全书背景用户、追问系统、追问上下文、书名识别系统、书名识别用户；支持占位符与逐项恢复默认。
- [x] Prompt 覆盖存于 settings，空值回退 `prompts.PROMPT_DEFAULTS`；章节问题数放宽到 1–3，允许用户 Prompt 只要求 1 个问题。

**验收**：供应商切换只显示对应配置（Playwright 检查 DeepSeek/Gemini/OpenAI 三个视图）；OpenAI 设置通过 UI 保存/恢复；Prompt 下拉 9 项、章节用户 Prompt 可编辑并保存、恢复默认后与代码默认完全一致；打开设置不显示旧 about 方框；pytest **109 passed**，无 console error。

---

## v1.4.1 追问、OpenAI 思考与导读修复（2026-11-04）

- [x] 追问上下文不再以重复的 user 消息 + 假 assistant 确认插入；合并进一条 system 背景，后续只追加真实 `user/assistant` 历史，和普通 chatbot 一致。
- [x] OpenAI 兼容服务支持统一「模型思考」开关和强度：按顶层 `reasoning_effort` 发送；关闭为 `none`，低/中/高/最高映射 low/medium/high/high；网关拒绝该参数时自动省略并缓存，不阻断回答；流式与非流式都支持。
- [x] OpenAI 兼容模式下恢复显示思考面板；设置页仍只显示所选供应商的 Key/Base URL/模型。
- [x] 未配置可用模型时，章节导读不再自动排队失败任务：后端跳过自动生成，前端直接显示「未配置可用模型，请配置可用模型之后重试。」并提供重新生成按钮与打开设置入口。
- [x] 保存设置后若当前章节尚无导读，会立即重绘导读卡片；配置完成后可直接点「重新生成」。
- [x] 章节问题数继续允许 1–3 个；导读错误接口统一返回明确中文提示。

**验收**：OpenAI 非流式/流式 payload 均带正确 `reasoning_effort`，关闭时发送 `none`，参数不支持时降级；追问 messages 只有一条 system 背景、无假确认、历史按轮追加；无 Key 打开章节时导读卡显示指定文案且无自动任务，按钮打开设置；pytest **111 passed**，Playwright 无 console error。

