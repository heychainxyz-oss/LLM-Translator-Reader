"""解析器验收测试：断词、编码、PDF 书签/正则分章、EPUB、短篇分节。"""

from __future__ import annotations

from pathlib import Path

import pytest
import pymupdf as fitz
from ebooklib import epub

import parser


def make_pdf(path: Path, pages: list[str], toc: list[list] | None = None, title: str = "Der Prozess") -> Path:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 540, 760), text, fontsize=11, fontname="helv")
    if toc:
        doc.set_toc(toc)
    doc.set_metadata({"title": title, "author": "Franz Kafka"})
    doc.save(path)
    doc.close()
    return path


def make_epub(path: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier("test-epub-1")
    book.set_title("Die Verwandlung")
    book.set_language("de")
    book.add_author("Franz Kafka")
    chapters = []
    for index, title in enumerate(["Erstes Kapitel", "Zweites Kapitel", "Drittes Kapitel"], start=1):
        item = epub.EpubHtml(title=title, file_name=f"chap_{index}.xhtml", lang="de")
        item.content = (
            f"<h1>{title}</h1>"
            "<p>Als Gregor Samsa eines Morgens aus unruhigen Träumen erwachte, "
            "fand er sich in seinem Bett zu einem ungeheuren Ungeziefer verwandelt.</p>"
            "<p>Er lag auf seinem panzerartig harten Rücken und sah, wenn er den Kopf ein wenig hob, "
            "seinen gewölbten, braunen Bauch.</p>"
        )
        book.add_item(item)
        chapters.append(item)
    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    epub.write_epub(str(path), book)
    return path


class TestHyphenation:
    def test_lowercase_join(self):
        assert parser.repair_hyphenation("Die Wissen-\nschaft ist groß.") == "Die Wissenschaft ist groß."

    def test_proper_noun_keeps_hyphen(self):
        assert (
            parser.repair_hyphenation("Das Goethe-\nInstitut ist berühmt.")
            == "Das Goethe-Institut ist berühmt."
        )

    def test_uppercase_join(self):
        assert parser.repair_hyphenation("DIE WISSEN-\nSCHAFT") == "DIE WISSENSCHAFT"

    def test_soft_hyphen(self):
        assert parser.repair_hyphenation("Wissen\u00ad-\nschaft") == "Wissenschaft"
        assert parser.repair_hyphenation("Wissen\u00adschaft") == "Wissenschaft"

    def test_does_not_touch_dash_lists(self):
        text = "Punkt eins - Punkt zwei\n\nNächster Absatz"
        assert parser.repair_hyphenation(text) == text

    def test_across_two_newlines_not_joined(self):
        text = "Ein Satz endet hier.\n\nNächster Absatz."
        assert parser.repair_hyphenation(text) == text


class TestEncodingAndParagraphs:
    @pytest.mark.parametrize("encoding", ["utf-8", "latin-1", "cp1252"])
    def test_german_umlauts(self, tmp_path, encoding):
        text = "Der Prozess\nFranz Kafka\n\nKapitel 1\n\nDie Straße war still. Äpfel, Öl und Übermut. Größe zählt."
        path = tmp_path / "book.txt"
        path.write_bytes(text.encode(encoding))
        parsed = parser.parse_file(path)
        joined = "\n".join(ch.text for ch in parsed.chapters)
        assert "Straße" in joined
        assert "Äpfel" in joined
        assert "Größe" in joined
        assert parsed.title == "Der Prozess"
        assert parsed.author == "Franz Kafka"

    def test_utf8_bom(self, tmp_path):
        path = tmp_path / "bom.txt"
        path.write_bytes(b"\xef\xbb\xbf" + "Kapitel 1\n\nÜbermut und Öl.".encode("utf-8"))
        parsed = parser.parse_file(path)
        assert "Übermut" in parsed.chapters[0].text

    def test_paragraph_cleanup(self):
        text = "Erster Absatz.\n\n\n\nZweiter Absatz.\n12\n\nDritter Absatz."
        paragraphs = parser.split_paragraphs(text)
        assert paragraphs == ["Erster Absatz.", "Zweiter Absatz.", "Dritter Absatz."]

    def test_hard_wrapped_txt(self):
        text = (
            "Dies ist ein Satz, der über eine Zeile\n"
            "hinweg weitergeht und nicht endet,\n"
            "sondern erst hier endet. Danach beginnt\n\n"
        )
        paragraphs = parser.split_paragraphs(text)
        assert len(paragraphs) == 1
        assert "Zeile hinweg" in paragraphs[0]


class TestPDF:
    def test_toc_chapters(self, tmp_path):
        pages = [
            "ERSTES KAPITEL\n\nJemand musste Josef K. verleumdet haben, denn ohne dass er etwas Böses getan hätte, wurde er eines Morgens verhaftet. Die Köchin brachte diesmal nicht das Frühstück.\n\nEr stand auf und sah aus dem Fenster.",
            "ZWEITES KAPITEL\n\nEr ging langsam die Treppe hinunter und trat auf die Straße. Die Häuser standen dicht beieinander, und die Menschen gingen vorbei, ohne ihn zu beachten.\n\nDann entschloss er sich, zum Gericht zu gehen.",
            "DRITTES KAPITEL\n\nIm Gerichtsgebäude war es kühl und dunkel. Ein Beamter führte ihn durch lange Gänge, in denen es nach Papier und Staub roch.\n\nSchließlich blieb der Beamte vor einer Tür stehen.",
        ]
        path = make_pdf(
            tmp_path / "toc.pdf",
            pages,
            toc=[[1, "Erstes Kapitel", 1], [1, "Zweites Kapitel", 2], [1, "Drittes Kapitel", 3]],
        )
        parsed = parser.parse_file(path)
        assert parsed.title == "Der Prozess"
        assert parsed.author == "Franz Kafka"
        assert parsed.toc_source == "pdf-toc"
        assert [ch.title for ch in parsed.chapters] == [
            "Erstes Kapitel",
            "Zweites Kapitel",
            "Drittes Kapitel",
        ]
        assert "verhaftet" in parsed.chapters[0].text

    def test_regex_chapters_without_toc(self, tmp_path):
        pages = []
        for index in range(1, 4):
            pages.append(
                f"Kapitel {index}\n\n"
                f"Dies ist der Text von Kapitel {index}. Er enthält deutsche Umlaute äöüß und mehrere Sätze. "
                "Der Inhalt ist lang genug, um als eigener Abschnitt zu gelten. "
                "Noch ein Satz, damit die Zeilen gefüllt sind."
            )
        path = make_pdf(tmp_path / "no_toc.pdf", pages)
        parsed = parser.parse_file(path)
        assert parsed.toc_source == "headings"
        assert [ch.title for ch in parsed.chapters] == ["Kapitel 1", "Kapitel 2", "Kapitel 3"]

    def test_hyphenation_across_pdf_lines(self, tmp_path):
        path = make_pdf(
            tmp_path / "hyphen.pdf",
            [
                "Die Wissen-\nschaft von der Schuld ist seltsam. Das Goethe-\nInstitut liegt in der Nähe. "
                "Noch ein weiterer Satz, damit genug Text vorhanden ist."
            ],
        )
        parsed = parser.parse_file(path)
        text = parsed.chapters[0].text
        assert "Wissenschaft" in text
        assert "Goethe-Institut" in text

    def test_scanned_pdf_error(self, tmp_path):
        doc = fitz.open()
        doc.new_page()
        path = tmp_path / "blank.pdf"
        doc.save(path)
        doc.close()
        with pytest.raises(parser.ParserError) as info:
            parser.parse_file(path)
        assert info.value.code == "scanned_pdf"
        assert "OCR" in info.value.message


class TestEPUB:
    def test_spine_and_toc(self, tmp_path):
        path = make_epub(tmp_path / "book.epub")
        parsed = parser.parse_file(path)
        assert parsed.title == "Die Verwandlung"
        assert parsed.author == "Franz Kafka"
        assert parsed.toc_source == "epub-toc"
        assert [ch.title for ch in parsed.chapters] == [
            "Erstes Kapitel",
            "Zweites Kapitel",
            "Drittes Kapitel",
        ]
        assert "Ungeziefer" in parsed.chapters[0].text
        assert "<p>" not in parsed.chapters[0].text


class TestChunking:
    def test_short_story_single_section(self):
        text = "\n\n".join(["Dies ist ein kurzer deutscher Absatz mit genug Wörtern."] * 5)
        chapters = parser.chunk_into_sections(text)
        assert len(chapters) == 1
        assert chapters[0].title == "Volltext"

    def test_long_text_chunked_at_paragraph_boundaries(self):
        paragraphs = [f"Absatz {index} " + "Wort " * 60 for index in range(120)]
        text = "\n\n".join(paragraphs)
        chapters = parser.chunk_into_sections(text)
        assert len(chapters) >= 2
        assert all(ch.paragraphs for ch in chapters)
        assert all(ch.title.startswith("Abschnitt") for ch in chapters)
        assert sum(ch.word_count for ch in chapters) == parser.count_words(text)

    def test_split_by_headings_requires_two(self):
        text = (
            "Kapitel 1\n\nNur ein Kapitel mit etwas Text darin, der ausreicht und deutlich länger ist "
            "als fünfundzwanzig Wörter, damit die Funktion es nicht als zu kurz verwirft. "
            "Noch ein Satz über die Wissenschaft und das Lesen."
        )
        chapters = parser.split_by_headings(text)
        assert len(chapters) == 1
        assert chapters[0].title == "Kapitel 1"
