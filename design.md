一、 核心技术栈与项目工程结构后端服务：Python 3.10+ 搭配 FastAPI + Uvicorn，负责本地文件解析、SQLite 数据库读写、以及调用 DeepSeek / Gemini 接口。文档解析：PyMuPDF（fitz，高速提取 PDF 文本与目录书签）、EbookLib + BeautifulSoup4（解析 EPUB，作为额外福利顺手支持）、Python 原生文件读取（自动检测 UTF-8 / Latin-1 编码处理德语特殊字符 ä, ö, ü, ß）。前端与跨平台容器：原生 HTML5 + Tailwind CSS + 原生 JavaScript。Windows / macOS：可用浏览器直接打开，或用 pywebview 封装成无边框的独立桌面软件窗口。Android 端：电脑启动服务后，手机连接同一 Wi-Fi 访问局域网地址（如 [http://192.168.1.](http://192.168.1.)x:8000），通过响应式布局自动切换为触屏抽屉模式。Plaintextgerman_reader_ai/
├── main.py              # FastAPI 入口与路由定义
├── database.py          # SQLite 连接与 CRUD 封装
├── parser.py            # PDF/TXT/EPUB 德语解析与断词清洗
├── ai_service.py        # DeepSeek Search / Chat 与 Gemini API 封装
├── reader_data.db       # 自动生成的本地 SQLite 数据库文件
├── uploads/             # 存放已导入的原始书籍文件
└── static/
    ├── index.html       # 主界面结构
    ├── style.css        # 翻页/滑动排版与高亮样式
    └── app.js           # 划词监听、缓存渲染、语音输入与多轮对话逻辑
二、 数据库持久化设计（SQLite 具体实现）所有书籍、AI 导读、划词解释、追问对话以及自定义 Prompt 全部保存在本地 reader_data.db 中，包含以下 5 张核心表：SQL-- 1. 书籍主表（存储全书级 AI 导读与阅读进度）
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL,          -- 'pdf' / 'txt' / 'epub'
    book_summary TEXT,                -- AI 全书概要
    author_bio TEXT,                  -- AI 作者简介（结合 DeepSeek 搜索）
    cultural_context TEXT,            -- AI 历史与文化背景（结合 DeepSeek 搜索）
    current_chapter INTEGER DEFAULT 0,-- 记录上次读到的章节序号
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 章节表（存储清洗后的正文段落与章节级 AI 导读）
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    chapter_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    paragraphs_json TEXT NOT NULL,    -- JSON 数组格式存储每一自然段: ["段落1", "段落2"]
    chapter_summary TEXT,             -- 本章情节概要
    guiding_questions TEXT,           -- JSON 数组存储阅读探究问题: ["问题1", "问题2"]
    is_guide_ready INTEGER DEFAULT 0, -- 0:未生成, 1:已生成（用于按需懒加载）
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
);

-- 3. 划词解析缓存表（核心防重与高亮定位表）
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL,
    paragraph_index INTEGER NOT NULL, -- 所在自然段序号
    start_offset INTEGER NOT NULL,    -- 划词在段落内的起始字符位置
    end_offset INTEGER NOT NULL,      -- 划词在段落内的结束字符位置
    selected_text TEXT NOT NULL,      -- 用户划选的德语词句
    context_sentence TEXT NOT NULL,   -- 自动捕获的完整上下文句子/段落
    ai_explanation TEXT NOT NULL,     -- AI 生成的词汇/语法/背景解析（Markdown格式）
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);

-- 4. 划词多轮追问记录表
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    annotation_id INTEGER NOT NULL,
    role TEXT NOT NULL,               -- 'user' 或 'assistant'
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(annotation_id) REFERENCES annotations(id) ON DELETE CASCADE
);

-- 5. 全局配置表（持久化保存 API Key 与用户自定义 Prompt）
CREATE TABLE settings (
    key TEXT PRIMARY KEY,             -- 如 'deepseek_key', 'gemini_key', 'active_model', 'selection_prompt'
    value TEXT NOT NULL
);
三、 各功能模块具体实现方式1. 德语文档导入、连字符修复与章节切分（parser.py）德语跨行连字符修复（Silbentrennung）：提取 PDF/TXT 文本后，先用正则 re.sub(r'([a-zäöüß])-\s*\n\s*([a-zäöüß])', r'\1\2', text) 将跨行断开的德语单词（如 Wissen-\nschaft）无缝拼接还原为 Wissenschaft；若后半部分首字母大写（如专有名词复合词 Goethe-\nInstitut），则消去换行但保留连字符。智能分章策略：若 PDF 含有内置书签目录（doc.get_toc()），直接按一级书签页码切分章节。若为无书签 PDF 或纯 TXT，通过正则匹配德语常见章节标题（如 ^(?:Kapitel|KAPITEL|[IVXLCDM]+\.|[0-9]+\.)\s+.*）切分；若全书无章节标题（如短篇小说），则每满 3000 词在自然段结尾处自动切分为一个阅读小节（Section）。段落结构化存储：每章正文按双换行符拆分为自然段列表，序列化为 paragraphs_json 存入数据库。前端将每个自然段渲染为 <p data-p-idx="0">...</p>，确保在翻页模式和滑动模式下字符偏移量（Offset）完全一致。2. 全书背景搜索与章节导读生成（ai_service.py）调用 DeepSeek 网络搜索 API 生成背景：导入书籍时，提取书名与作者（若文件名不规范，截取前 1000 字让模型快速识别书名与作者）。后端向 POST [https://api.deepseek.com/v1/tools/search](https://api.deepseek.com/v1/tools/search) 发送请求，携带请求头 Authorization: Bearer {DEEPSEEK_API_KEY}，请求体设置 {"query": "{书名} {作者} Hintergrund Entstehung Interpretation", "count": 5, "need_content": False}。筛选返回结果中 rerank_score 较高（如大于等于 1.5）的条目，提取其 summary 字段（600~800 字高密度摘要）作为参考资料，拼接进 Prompt 发送给 DeepSeek Chat（或 Gemini），一次生成【全书概要】【作者生平】【历史文化背景】并存入 books 表。章节概要与探究问题（懒加载触发）：导入时仅生成第 1 章和第 2 章的导读（总结核心情节走向 + 提出 3 个引导读者关注德语细节或人物动机的探究问题）。当你在界面打开第 $N$ 章时，前端静默请求 /api/chapters/{N+1}/prepare_guide，后台自动预生成下一章导读，做到阅读零等待、Token 不浪费。3. 划词解析、缓存复用与多轮追问上下文自动捕获：前端监听鼠标抬起（mouseup）或触屏选区变化，获取选中的 selected_text、所在 <p> 标签的序号 paragraph_index、起止偏移量，并自动提取该段文字作为 context_sentence。缓存命中与重新生成：请求发往后端 /api/annotate 时，先在 annotations 表查询：WHERE chapter_id = ? AND paragraph_index = ? AND selected_text = ?。若命中记录且 force_regenerate == False，直接返回数据库里的 ai_explanation 和历史 chat_messages。若未命中（或用户点击了 UI 上的【🔄 重新生成】按钮传入 force_regenerate = True），则将 {selected_text} 和 {context_sentence} 填入你在设置中编写的自定义 Prompt 模板，调用 AI 生成解释并写入/更新数据库。语音与文字追问：调用浏览器原生 SpeechRecognition API（支持设定识别语言为 zh-CN 或 de-DE），点击麦克风说话即刻转为文字填入输入框。发送追问时，后端将原句上下文、首次 AI 解析以及 chat_messages 表中的历史记录打包发给模型，实现针对该语法点或文化背景的深度追问。四、 用户界面（UI）显示方式与布局设计界面采用三栏式响应式工作台设计，色彩采用护眼羊皮纸色（暖白/暗黑可选），布局结构如下：Plaintext+--------------------------------------------------------------------------------------------------+
| 📚 德语智读 (German AI Reader)     [📖 Der Prozess - K.1]   [滑动/翻页切换]  [A-/A+]  [⚙️ 设置] |
+----------------------+--------------------------------------------------+------------------------+
| 【左侧导航栏】       | 【中间核心阅读区】                               | 【右侧 AI 私教面板】   |
| (可折叠 Drawer)      |                                                  |                        |
|                      | +----------------------------------------------+ | 📌 当前选中词句：      |
| [目录] [全书背景]    | | 💡 本章导读 & 探究问题 (点击可折叠)          | | "verhaftet"            |
|                      | | • 概要：银行襄理约瑟夫·K在三十岁生日早晨...  | | [🔄 重新生成]          |
| 1. 第一章: 逮捕  <-- | | • 探究1：K面对突如其来的逮捕表现出怎样的态度?| +------------------------+
| 2. 第二章: 初次审讯  | | • 探究2：注意本章中大量使用的第一虚拟式...   | | 🤖 AI 语法与词汇精讲： |
| 3. 第三章: 在空会议厅| +----------------------------------------------+ | 1. 词义：verhaften 的  |
| ...                  |                                                  |    第二分词，意为逮捕。|
|                      | Jemand musste Josef K. verleumdet haben, denn    | 2. 语法：与末尾的      |
|                      | ohne dass er etwas Böses getan hätte, wurde er   |    wurde 构成被动态，  |
|                      | eines Morgens [verhaftet]⭐.                     |    且前面 hätte 为第二 |
|                      |                                                  |    虚拟式表非真实条件。|
|                      | Die Köchin der Frau Grubach, seiner              | 3. 表达：现代日常与法律|
|                      | Zimmervermieterin, die ihm jeden Tag gegen acht  |    德语中依然常用。    |
|                      | Uhr das Frühstück brachte, kam diesmal nicht.    |                        |
|                      | [Das war noch niemals geschehen.]⭐              | +--------------------+ |
|                      |                                                  | | 💬 针对此处继续追问| |
|                      |                                                  | | 你: 为什么用hätte? | |
|                      |                                                  | | AI: 因为在 ohne    | |
|                      |                                                  | | dass 从句中...     | |
|                      |                                                  | +--------------------+ |
| [➕ 导入 PDF/TXT]    |         [ ◀ 上一页 ]   第 1 / 12 页   [ 下一页 ▶ ]| [输入框...   ] [🎙️][发送]|
+----------------------+--------------------------------------------------+------------------------+
UI 核心交互亮点：双阅读模式无缝切换：滑动模式（Scroll）：整章自然上下滚动，适合电脑鼠标滚轮或手机竖屏阅读。翻页模式（Paginated）：利用 CSS 多列布局（column-width + overflow: hidden）将章节正文动态切分为固定高度的书页，支持键盘 ← / → 方向键、页面底部按钮或手机左右划屏翻页。章节导读卡片置顶：每章第一页顶部内嵌一张柔和底色的【💡 本章导读 & 探究问题】卡片，读完开头导读后可一键折叠收起，不占阅读空间。缓存高亮与一键复现：当你打开任意一章，前端会自动根据 annotations 表的偏移量，把所有曾经查过的德语词句渲染成暖黄色背景高亮（如上图带 ⭐ 标记处）。鼠标直接点击高亮词句，右侧面板立刻弹出数据库里保存好的解释与之前的追问记录，完全不消耗 API 额度；划选未高亮的新词句，则自动触发 AI 解析并新增高亮。自定义 Prompt 实时编辑：点击右上角【⚙️ 设置】弹窗，可随时切换 DeepSeek / Gemini 模型、填入 API Key，并在文本框内自由修改你的划词 Prompt（支持 {selected_text} 和 {context_sentence} 占位符）。