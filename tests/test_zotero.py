import httpx

from nfh.config import ZoteroSettings
from nfh.zotero import Zotero, make_cite_key

ITEMS = [
    {
        "key": "AAA",
        "data": {
            "itemType": "journalArticle",
            "title": "Platform Labour",
            "creators": [
                {"creatorType": "author", "firstName": "Ana", "lastName": "Ruiz"}
            ],
            "date": "2021-03",
            "DOI": "10.1/xyz",
            "tags": [{"tag": "lit-review"}, {"tag": "core"}],
            "collections": ["C1"],
            "extra": "Citation Key: ruiz2021platform",
        },
    },
    {
        "key": "BBB",
        "data": {
            "itemType": "book",
            "title": "Work and Time",
            "creators": [{"creatorType": "author", "name": "Collective"}],
            "date": "1999",
            "tags": [{"tag": "lit-review"}],
            "collections": [],
        },
    },
]


def make_client(tmp_path, seen):
    storage = tmp_path / "Zotero" / "storage" / "ATT1"
    storage.mkdir(parents=True)
    (storage / "paper.pdf").write_bytes(b"%PDF-1.4")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        path = request.url.path
        if path.endswith("/collections"):
            return httpx.Response(200, json=[{"key": "C1", "data": {"name": "Review"}}])
        if path.endswith("/children"):
            if "/AAA/" in path:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "data": {
                                "itemType": "attachment",
                                "key": "ATT1",
                                "linkMode": "imported_file",
                                "contentType": "application/pdf",
                                "filename": "paper.pdf",
                            }
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        if path.endswith("/items/top"):
            tags = request.url.params.get_list("tag")
            items = ITEMS
            if "collections/C1" in path:
                items = [i for i in items if "C1" in i["data"]["collections"]]
            if tags == ["lit-review || core"] or not tags:
                pass
            elif tags == ["lit-review", "core"]:
                items = [i for i in items if len(i["data"]["tags"]) == 2]
            return httpx.Response(200, json=items)
        return httpx.Response(404)

    settings = ZoteroSettings(data_dir=str(tmp_path / "Zotero"))
    z = Zotero(settings)
    z.client = httpx.Client(transport=httpx.MockTransport(handler))
    return z


def test_tag_any_and_all(tmp_path):
    seen = []
    z = make_client(tmp_path, seen)
    any_items = z.items(tags=["lit-review", "core"], tag_match="any")
    assert {i.key for i in any_items} == {"AAA", "BBB"}
    assert any("lit-review+%7C%7C+core" in str(u) or "%7C%7C" in str(u) for u in seen)
    all_items = z.items(tags=["lit-review", "core"], tag_match="all")
    assert [i.key for i in all_items] == ["AAA"]


def test_collection_scope_files_and_keys(tmp_path):
    z = make_client(tmp_path, [])
    items = z.items(collection="Review")
    assert [i.key for i in items] == ["AAA"]
    item = items[0]
    assert item.cite_key == "ruiz2021platform"
    assert item.file and item.file.name == "paper.pdf"
    assert item.year == "2021" and item.authors == ["Ana Ruiz"]


def test_fallback_cite_key():
    key = make_cite_key(ITEMS[1]["data"], "1999")
    assert key == "collective1999work"


def test_key_sources(tmp_path):
    native = {**ITEMS[0], "data": {**ITEMS[0]["data"], "citationKey": "ruizNative"}}
    assert Zotero._to_item(native).key_source == "zotero"
    assert Zotero._to_item(native).cite_key == "ruizNative"
    assert Zotero._to_item(ITEMS[0]).key_source == "extra"
    assert Zotero._to_item(ITEMS[1]).key_source == "generated"
