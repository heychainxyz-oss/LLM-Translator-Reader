"""Playwright UI checks for the German AI Reader (development helper).

Usage: .venv\\Scripts\\python.exe tools\\ui_check.py [base_url]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
OUT = Path(__file__).resolve().parents[1] / "docs"
OUT.mkdir(exist_ok=True)


def attach(page, label, errors):
    page.on("console", lambda msg: errors.append((label, msg.type, msg.text)))
    page.on("pageerror", lambda err: errors.append((label, "pageerror", str(err))))


def wait_annotation(page, timeout_steps=60):
    for step in range(timeout_steps):
        page.wait_for_timeout(1000)
        try:
            text = page.locator("#explanation").inner_text().strip()
        except Exception:
            text = ""
        if (
            page.locator("#ai-content").is_visible()
            and page.locator("#chat-section").is_visible()
            and len(text) > 30
            and page.locator("#annotation-cache-badge").inner_text().strip() != "生成中"
        ):
            return step
    return -1


def wait_chat(page, timeout_steps=60):
    for step in range(timeout_steps):
        page.wait_for_timeout(1000)
        if page.locator(".chat-msg.typing").count() == 0 and page.locator("#chat-send").is_enabled():
            locator = page.locator(".chat-msg.assistant:not(.typing) .chat-bubble")
            if locator.count() and len(locator.last.inner_text().strip()) > 10:
                return step
    return -1


def select_word(page, paragraph_selector, word):
    page.evaluate(
        """([selector, word]) => {
            const p = document.querySelector(selector);
            if (!p) throw new Error('paragraph missing');
            const text = p.textContent;
            const idx = text.indexOf(word);
            if (idx < 0) throw new Error('word not found');
            const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
            let node, pos = 0, target = null, targetOffset = 0;
            while ((node = walker.nextNode())) {
                if (pos + node.textContent.length > idx) {
                    target = node;
                    targetOffset = idx - pos;
                    break;
                }
                pos += node.textContent.length;
            }
            if (!target) throw new Error('text node missing');
            const range = document.createRange();
            range.setStart(target, targetOffset);
            range.setEnd(target, targetOffset + word.length);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            p.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
        [paragraph_selector, word],
    )


def main() -> None:
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)

        # ------------------------------ desktop ------------------------------
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        attach(page, "desktop", errors)
        page.goto(f"{BASE}/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        print("library title:", page.title())
        print("book cards:", page.locator("#book-grid .book-card").count())
        page.screenshot(path=str(OUT / "ui-library.png"), full_page=True)

        # theme toggle
        before = page.evaluate("() => document.documentElement.dataset.theme")
        page.locator("#library-theme-toggle").click()
        page.wait_for_timeout(500)
        after = page.evaluate("() => document.documentElement.dataset.theme")
        print("theme toggle:", before, "->", after)
        page.wait_for_timeout(1800)
        page.screenshot(path=str(OUT / "ui-library-dark.png"), full_page=True)
        page.locator("#library-theme-toggle").click()
        page.wait_for_timeout(500)

        page.locator("#book-grid .book-card").first.click()
        page.wait_for_timeout(2200)
        page.screenshot(path=str(OUT / "ui-reader.png"))
        print("chapter:", page.locator(".chapter-title").inner_text())

        # paginated
        page.locator("#mode-switch [data-mode=paginated]").click()
        page.wait_for_timeout(800)
        print("pages:", page.locator("#footer-label").inner_text())
        print("column gap:", page.locator("#reading-content").evaluate("el => getComputedStyle(el).columnGap"))
        page.screenshot(path=str(OUT / "ui-reader-paginated.png"))
        page.locator("#footer-next").click()
        page.wait_for_timeout(500)
        print("after next:", page.locator("#footer-label").inner_text())
        page.locator("#footer-prev").click()
        page.wait_for_timeout(500)
        page.locator("#mode-switch [data-mode=scroll]").click()
        page.wait_for_timeout(500)

        # cached annotation or fresh selection
        marks = page.locator("mark.hl").count()
        print("existing marks:", marks)
        if marks == 0:
            select_word(page, "#p-0", "verhaftet")
            page.wait_for_timeout(900)
            print("annotation wait:", wait_annotation(page))
        else:
            page.locator("mark.hl").first.click()
            page.wait_for_timeout(500)
        print("selection:", page.locator("#selection-word").inner_text())
        page.screenshot(path=str(OUT / "ui-annotation.png"))

        # chat
        page.locator("#chat-input").fill("为什么这里用 wurde，而不是 ist verhaftet？")
        page.locator("#chat-send").click()
        print("chat wait:", wait_chat(page))
        print("chat messages:", page.locator(".chat-msg").count())
        page.screenshot(path=str(OUT / "ui-chat.png"))

        # search
        page.locator("[data-sidebar-tab=search]").click()
        page.locator("#search-input").fill("verhaftet")
        page.wait_for_timeout(1200)
        print("search results:", page.locator(".search-result").count())
        page.screenshot(path=str(OUT / "ui-search.png"))

        # settings tabs
        page.locator("#reader-settings").click()
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "ui-settings-ai.png"))
        for tab, name in (("prompt", "prompt"), ("reading", "reading"), ("about", "about")):
            page.locator(f'[data-settings-tab="{tab}"]').click()
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / f"ui-settings-{name}.png"))
        print("settings visible:", page.locator("#modal-settings").is_visible())
        page.locator("#modal-settings [data-close-modal]").last.click()
        page.wait_for_timeout(300)

        # dark reader
        page.locator("#reader-theme-toggle").click()
        page.wait_for_timeout(500)
        print("reader theme:", page.evaluate("() => document.documentElement.dataset.theme"))
        page.wait_for_timeout(1800)
        page.screenshot(path=str(OUT / "ui-reader-dark.png"))
        page.locator("#reader-theme-toggle").click()
        page.wait_for_timeout(300)
        context.close()

        # ------------------------------- mobile ------------------------------
        mobile_context = browser.new_context(
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=1,
        )
        mobile = mobile_context.new_page()
        attach(mobile, "mobile", errors)
        mobile.goto(f"{BASE}/", wait_until="networkidle")
        mobile.wait_for_timeout(1200)
        mobile.screenshot(path=str(OUT / "ui-mobile-library.png"), full_page=True)
        mobile.locator("#book-grid .book-card").first.click()
        mobile.wait_for_timeout(2200)
        mobile.screenshot(path=str(OUT / "ui-mobile-reader.png"))
        print("mobile title:", mobile.locator(".chapter-title").inner_text())
        print("mobile toggles:", mobile.locator("#left-toggle").is_visible(), mobile.locator("#right-toggle").is_visible())
        mobile.locator("#left-toggle").click()
        mobile.wait_for_timeout(500)
        print("left open:", mobile.evaluate("() => document.body.classList.contains('left-open')"))
        mobile.screenshot(path=str(OUT / "ui-mobile-toc.png"))
        mobile.locator("#sidebar-backdrop").click(force=True)
        mobile.wait_for_timeout(400)
        mobile.locator("#right-toggle").click()
        mobile.wait_for_timeout(500)
        print("right open:", mobile.evaluate("() => document.body.classList.contains('right-open')"))
        mobile.screenshot(path=str(OUT / "ui-mobile-ai.png"))
        mobile.locator("#sidebar-backdrop").click(force=True)
        mobile.wait_for_timeout(300)
        # selection on mobile
        select_word(mobile, "#p-0", "verhaftet")
        mobile.wait_for_timeout(1200)
        print("mobile toolbar:", mobile.locator("#selection-toolbar").is_visible())
        print("mobile right open after select:", mobile.evaluate("() => document.body.classList.contains('right-open')"))
        mobile.screenshot(path=str(OUT / "ui-mobile-selection.png"))
        mobile_context.close()
        browser.close()

    serious = [item for item in errors if item[1] in ("error", "pageerror")]
    print("console errors:", json.dumps(serious, ensure_ascii=False, indent=2))
    print("screenshots in:", OUT)


if __name__ == "__main__":
    main()
