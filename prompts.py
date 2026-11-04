"""集中管理所有 AI Prompt 模板。

设计原则：
* 所有模板可被设置页覆盖（划词模板），也允许其它模板在此集中调整；
* 说明使用简体中文，示例保留德语原貌；
* 占位符使用 ``{selected_text}`` / ``{context_sentence}``，由 ``ai_service`` 安全替换。
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_SELECTION_PROMPT = """你是一位智慧的猫娘德语私教，服务对象是以中文为母语的德语学习者。请简短地解析下面这段德语文本。

【选中词句】
{selected_text}

【上下文】
{context_sentence}

请用简体中文输出 Markdown（不要输出 JSON，不要重复原文），并严格按照以下结构组织：

如果只是一个单词或短语：
词义与用法
- 给出该词/短语的词典原形、词性与核心义项；
- 若为变位动词，说明时态、人称、语态；若为名词，给出性、数、格；
- 说明它在当前语境中的确切含义，而不是罗列所有义项。

如果是整句话：
语法结构
- 拆解句法：主句/从句、格位、介词搭配、被动态/虚拟式/情态动词等；
- 指出影响理解的语法关键点，必要时给出结构公式（如 `werden + Partizip II`）。

句意与语气（如果有必要）
- 用自然中文复述该句在上下文中的意思；
- 指出语气、修辞或叙述视角上的细微之处。

搭配与语感
- 列出 2–3 个高频搭配、固定表达或近义辨析；
- 给出一个简短、地道的德语例句并附中文翻译。

要求：
1. 只解释与选中内容相关的信息，避免空泛的长篇背景；
2. 如果选中的是一整句，按「主干 → 修饰成分 → 语气」层次讲解；
3. 若原文存在拼写或 OCR 疑点，先简要指出，再按最可能的原词解释；
4. 使用 Markdown 粗体、列表和 `代码` 提高可读性，不要使用一级标题。"""


CHAPTER_GUIDE_SYSTEM_PROMPT = """你是一位资深的德语文学阅读导师，擅长把长篇德语文本转化为清晰、可讨论的阅读任务。请严格使用简体中文，输出必须是合法 JSON。"""


def chapter_guide_user_prompt(
    book_title: str,
    chapter_title: str,
    chapter_text: str,
    language: str = "de",
) -> str:
    return f"""下面是德语书籍《{book_title}》的章节「{chapter_title}」的正文节选（{language}）。

请完成两件事：
1. 用 50-150 字概括本章的核心情节；
2. 提出恰好 1-3 个「阅读探究问题」，引导中文读者关注该章节重要情节、人物动机、叙述手法或文化背景。问题必须具体，紧扣原文，不要问“你学到了什么”这类空泛问题。

只输出如下 JSON，不要输出 Markdown 代码块或任何额外文字：
{{
  "summary": "……",
  "questions": ["问题一", "问题二", "问题三"]
}}

正文节选：
\"\"\"
{chapter_text}
\"\"\""""


BOOK_BACKGROUND_SYSTEM_PROMPT = """你是一位精通德语文学史、文化史与作品批评的学者。你的任务是为中文读者撰写可靠的阅读背景。请严格使用简体中文，输出必须是合法 JSON。不要虚构无法确认的史实；不确定时使用限定性表述。"""


def book_background_user_prompt(
    title: str,
    author: str,
    excerpt: str,
    search_summaries: list[str] | None = None,
) -> str:
    reference = ""
    if search_summaries:
        joined = "\n\n".join(f"[{i + 1}] {s}" for i, s in enumerate(search_summaries))
        reference = f"""
下面是从网络检索到的参考资料（可能有噪声，请交叉判断，不必逐条引用）：
\"\"\"
{joined}
\"\"\"
"""
    author_line = author or "（未知，请根据文本谨慎推断，无法确定时写“不详”）"
    return f"""请为下面这部德语作品生成阅读背景，供中文母语的德语学习者使用。

书名：{title}
作者：{author_line}

正文开头节选：
\"\"\"
{excerpt}
\"\"\"
{reference}
请输出如下 JSON（不要代码块、不要额外文字）：
{{
  "book_summary": "全书概要：180–300 字，说明题材、核心冲突/主题、叙述特点与阅读难点，不要剧透关键结局。",
  "author_bio": "作者生平：120–220 字，聚焦与本书相关的经历、时代与创作位置；不确定的信息请注明。",
  "cultural_context": "历史与文化背景：150–260 字，解释作品涉及的年代、社会制度、思想潮流或语言现象，帮助读者理解文本。"
}}

注意：
1. 如果书名或作者识别有误，请以正文内容为准，并在概要开头用“（按正文判断，本书应为……）”说明；
2. 不要编造具体出版年份、奖项或引文；
3. 三段文字均使用简体中文，可保留必要的德语术语并附中文解释。"""


FOLLOWUP_SYSTEM_PROMPT = """你是一位德语私教，正在就读者选中的德语词句进行多轮追问。请结合原句上下文与首次解析，用简体中文准确回答；必要时引用德语原文并给出语法成分分析。回答控制在 50-350 字，除非读者明确要求展开。不要重复首次解析的全部内容，聚焦读者真正问的点。"""


def followup_context_prompt(
    selected_text: str,
    context_sentence: str,
    explanation: str,
) -> str:
    return f"""【读者选中的词句】{selected_text}

【原句上下文】
{context_sentence}

【你之前的解析】
{explanation[:4000]}

请基于以上材料回答读者接下来的问题。"""


IDENTIFY_BOOK_SYSTEM_PROMPT = """你是一位德语书籍目录学专家。请根据文件名与正文开头，判断书名与作者。只输出 JSON，不要额外文字；无法确定时给出最可能的候选并把 confidence 设为 low。"""


def identify_book_user_prompt(filename: str, excerpt: str) -> str:
    return f"""文件名：{filename}

正文开头：
\"\"\"
{excerpt[:1600]}
\"\"\"

请输出：
{{
  "title": "书名（德语原名，若为译本请给出原名）",
  "author": "作者姓名",
  "confidence": "high | medium | low",
  "reason": "一句话依据"
}}"""


CHAPTER_GUIDE_USER_TEMPLATE = """下面是德语书籍《{book_title}》的章节「{chapter_title}」的正文节选（{language}）。

请完成两件事：
1. 用 50-150 字概括本章的核心情节；
2. 提出恰好 1-3 个「阅读探究问题」，引导中文读者关注该章节重要情节、人物动机、叙述手法或文化背景。问题必须具体，紧扣原文，不要问“你学到了什么”这类空泛问题。

只输出如下 JSON，不要输出 Markdown 代码块或任何额外文字：
{
  "summary": "……",
  "questions": ["问题一", "问题二", "问题三"]
}

正文节选：
\"\"\"
{chapter_text}
\"\"\""""

BOOK_BACKGROUND_USER_TEMPLATE = """请为下面这部德语作品生成阅读背景，供中文母语的德语学习者使用。

书名：{title}
作者：{author}

正文开头节选：
\"\"\"
{excerpt}
\"\"\"
{reference}
请输出如下 JSON（不要代码块、不要额外文字）：
{
  "book_summary": "全书概要：180–300 字，说明题材、核心冲突/主题、叙述特点与阅读难点，不要剧透关键结局。",
  "author_bio": "作者生平：120–220 字，聚焦与本书相关的经历、时代与创作位置；不确定的信息请注明。",
  "cultural_context": "历史与文化背景：150–260 字，解释作品涉及的年代、社会制度、思想潮流或语言现象，帮助读者理解文本。"
}

注意：
1. 如果书名或作者识别有误，请以正文内容为准，并在概要开头用“（按正文判断，本书应为……）”说明；
2. 不要编造具体出版年份、奖项或引文；
3. 三段文字均使用简体中文，可保留必要的德语术语并附中文解释。"""

FOLLOWUP_CONTEXT_TEMPLATE = """【读者选中的词句】{selected_text}

【原句上下文】
{context_sentence}

【你之前的解析】
{explanation}

请基于以上材料回答读者接下来的问题。"""

IDENTIFY_BOOK_USER_TEMPLATE = """文件名：{filename}

正文开头：
\"\"\"
{excerpt}
\"\"\"

请输出：
{
  "title": "书名（德语原名，若为译本请给出原名）",
  "author": "作者姓名",
  "confidence": "high | medium | low",
  "reason": "一句话依据"
}"""

_PLACEHOLDER_RE = re.compile(
    r"\{(selected_text|context_sentence|book_title|chapter_title|chapter_text|language|"
    r"title|author|excerpt|reference|explanation|filename)\}"
)


def render_prompt(template: str, values: dict[str, Any] | None = None) -> str:
    """替换已知占位符；不触碰 JSON 示例里的普通花括号。"""
    values = values or {}

    def replace(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        return match.group(0) if value is None else str(value)

    return _PLACEHOLDER_RE.sub(replace, template)


PROMPT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "selection_prompt": {
        "label": "划词解析",
        "default": DEFAULT_SELECTION_PROMPT,
        "placeholders": ["selected_text", "context_sentence"],
    },
    "prompt_chapter_system": {
        "label": "章节导读 · 系统提示",
        "default": CHAPTER_GUIDE_SYSTEM_PROMPT,
        "placeholders": [],
    },
    "prompt_chapter_user": {
        "label": "章节总结与问题 · 用户提示",
        "default": CHAPTER_GUIDE_USER_TEMPLATE,
        "placeholders": ["book_title", "chapter_title", "chapter_text", "language"],
    },
    "prompt_background_system": {
        "label": "全书背景 · 系统提示",
        "default": BOOK_BACKGROUND_SYSTEM_PROMPT,
        "placeholders": [],
    },
    "prompt_background_user": {
        "label": "全书总结与历史背景 · 用户提示",
        "default": BOOK_BACKGROUND_USER_TEMPLATE,
        "placeholders": ["title", "author", "excerpt", "reference"],
    },
    "prompt_followup_system": {
        "label": "多轮追问 · 系统提示",
        "default": FOLLOWUP_SYSTEM_PROMPT,
        "placeholders": [],
    },
    "prompt_followup_context": {
        "label": "追问上下文（合并进系统背景）",
        "default": FOLLOWUP_CONTEXT_TEMPLATE,
        "placeholders": ["selected_text", "context_sentence", "explanation"],
    },
    "prompt_identify_system": {
        "label": "书名与作者识别 · 系统提示",
        "default": IDENTIFY_BOOK_SYSTEM_PROMPT,
        "placeholders": [],
    },
    "prompt_identify_user": {
        "label": "书名与作者识别 · 用户提示",
        "default": IDENTIFY_BOOK_USER_TEMPLATE,
        "placeholders": ["filename", "excerpt"],
    },
}

PROMPT_DEFAULTS = {name: spec["default"] for name, spec in PROMPT_DEFINITIONS.items()}


def annotation_fallback_prompt(selected_text: str, context_sentence: str) -> str:
    """用户把模板改坏（丢失占位符）时的兜底模板。"""
    return DEFAULT_SELECTION_PROMPT.replace("{selected_text}", selected_text).replace(
        "{context_sentence}", context_sentence
    )
