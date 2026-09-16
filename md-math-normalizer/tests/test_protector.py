from md_math_normalizer.protector import protect_markdown


def test_fenced_code_protected():
    text = "```\na=b\n```\n"
    spans = protect_markdown(text)
    assert any(s.kind.name == "FENCED_CODE" for s in spans)
    assert spans[0].start == 0
    assert spans[0].end == len(text)


def test_inline_code_protected():
    text = "x `a=1` y"
    spans = protect_markdown(text)
    assert any(s.kind.name == "INLINE_CODE" for s in spans)


def test_html_and_urls_protected():
    text = '<div>https://example.com/a.png</div>'
    spans = protect_markdown(text)
    kinds = {s.kind.name for s in spans}
    assert "RAW_HTML" in kinds
    assert "URL" in kinds


def test_markdown_link_and_image_protected():
    text = '[a](https://example.com) ![b](https://example.org)'
    spans = protect_markdown(text)
    kinds = {s.kind.name for s in spans}
    assert "MARKDOWN_LINK_DESTINATION" in kinds
    assert "MARKDOWN_IMAGE" in kinds


def test_escaped_sequence_protected():
    text = r"\\$x"
    spans = protect_markdown(text)
    assert any(s.kind.name == "ESCAPED_SEQUENCE" for s in spans)
