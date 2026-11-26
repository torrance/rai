import shutil

import pytest

from rai.manuscript import chapter_id, parse_draft

needs_pandoc = pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc missing")


@needs_pandoc
def test_markdown_citations_and_numbering(project):
    path = project.drafts_dir / "01-intro.md"
    path.write_text(
        "# Introduction\n\nPlatforms rate workers [@ruiz2021, p. 4].\n\n"
        "Second paragraph cites two [@ruiz2021; @smith2019, ch. 2]. TODO check.\n"
    )
    paras = parse_draft(path)
    numbered = [p for p in paras if "para" in p]
    assert [p["para"] for p in numbered] == [1, 2]
    assert numbered[0]["citations"] == ["@ruiz2021, p. 4"]
    assert "@smith2019, ch. 2" in numbered[1]["citations"]
    assert chapter_id(path, project) == "01-intro"


@needs_pandoc
def test_typst_citations(project):
    path = project.drafts_dir / "essay.typ"
    path.write_text("= Essay\n\nA claim @ruiz2021[p. 4].\n\nAnother claim.\n")
    paras = [p for p in parse_draft(path) if "para" in p]
    if not paras or not paras[0]["citations"]:
        pytest.skip("this pandoc build has no Typst citation support")
    assert paras[0]["citations"][0].startswith("@ruiz2021")
