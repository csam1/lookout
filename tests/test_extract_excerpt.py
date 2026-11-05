from lookout.excerpt import make_chunks, select_excerpts
from lookout.extract import extract


def test_strips_chrome_scripts_and_hidden_text():
    html = """
    <html><body>
      <header>Site banner with a long enough line of navigation text in it</header>
      <script>var secret = "do not leak";</script>
      <article>
        <header><h1>Article title</h1></header>
        <p>First real paragraph of the article body with enough words.</p>
        <p hidden>Hidden attribute text that must not be returned to agents.</p>
        <p aria-hidden="true">Aria hidden text that must not be returned either.</p>
        <p style="font-size:0">Zero font size injection text should be dropped.</p>
        <p style="opacity: 0">Transparent injection text should be dropped too.</p>
        <ul><li>A list item that is long enough to keep</li><li>Short</li></ul>
      </article>
    </body></html>
    """
    page = extract(html)
    text = page.markdown
    assert page.blocks[0] == "# Article title"  # header inside article is kept
    assert "First real paragraph" in text
    assert "- A list item that is long enough to keep" in text
    for gone in ("Site banner", "do not leak", "Hidden attribute", "Aria hidden", "Zero font", "Transparent"):
        assert gone not in text


def test_nested_blocks_are_not_duplicated():
    page = extract("<body><ul><li><p>Paragraph inside a list item, long enough.</p></li></ul></body>")
    assert page.blocks == ["Paragraph inside a list item, long enough."]


def test_div_only_page_falls_back_to_body_text():
    page = extract("<body><div>Plain text in a div. It has two sentences.</div></body>")
    assert page.blocks == ["Plain text in a div. It has two sentences."]


def test_pre_blocks_keep_line_breaks():
    page = extract("<body><pre>line one\nline two</pre></body>")
    assert page.blocks == ["```\nline one\nline two\n```"]


def test_published_at_from_time_tag():
    page = extract('<body><time datetime="2025-01-02">Jan 2</time><p>Body text long enough to keep.</p></body>')
    assert page.published_at == "2025-01-02"


def test_bm25_ranks_matching_chunk_first():
    blocks = [
        "Opening paragraph about the weather in spring and gardening tips. " * 5,
        "Kubernetes pod autoscaling uses metrics to change replica counts. " * 5,
        "Closing paragraph about recipes and cooking. " * 5,
    ]
    excerpts = select_excerpts(blocks, "kubernetes autoscaling replicas", max_chars=2000)
    assert excerpts[0].startswith("Kubernetes pod autoscaling")
    assert all("gardening" not in e for e in excerpts)


def test_no_match_falls_back_to_first_chunk():
    blocks = ["First block of text here.", "Second block of text here."]
    assert select_excerpts(blocks, "zebra", max_chars=1000) == ["First block of text here.\n\nSecond block of text here."]


def test_total_length_within_budget():
    blocks = [f"Block {i} talks about search engines and ranking at length. " * 4 for i in range(20)]
    excerpts = select_excerpts(blocks, "search ranking", max_chars=250)
    assert 0 < sum(len(e) for e in excerpts) <= 250


def test_chunks_split_long_blocks():
    long_block = "Sentence number one is here. " * 100
    chunks = make_chunks([long_block], target=200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
