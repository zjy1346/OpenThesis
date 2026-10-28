from openthesis.disclosure_index import cached_page_texts, remember_page_texts


def test_byte_identical_originals_reuse_text_despite_path_and_mtime(tmp_path):
    first, second = tmp_path / "one.pdf", tmp_path / "another-location.pdf"
    first.write_bytes(b"same public filing bytes")
    second.write_bytes(first.read_bytes())
    remember_page_texts(first, ["original text"])
    assert cached_page_texts(second) == ("original text",)
    second.write_bytes(b"replaced original contents")
    assert cached_page_texts(second) is None
    assert cached_page_texts(first) == ("original text",)
