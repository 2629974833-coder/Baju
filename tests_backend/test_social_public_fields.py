import pytest

from backend.store import Store


@pytest.mark.parametrize("platform", ["douyin", "tiktok", "instagram", "youtube"])
def test_social_signed_media_stays_in_worker_not_panel(tmp_path, platform):
    store = Store(tmp_path / "workspace.sqlite3")
    signed = "https://media.example.test/source.mp4?signature=private-sample"
    asset = {"id": "clip", "siteId": platform, "kind": "video", "title": "Sample",
             "pageUrl": f"https://www.{platform}.com/video/sample", "sourceUrl": signed,
             "shortMedia": {"platform": platform, "postId": "sample", "watermark": "unknown"},
             "_youtubeFormats": [{"url": signed}], "_youtubeDuration": 19}
    store.mutate(lambda state: state["assets"].append(asset))
    public = store.public_state()["assets"][0]
    assert "sourceUrl" not in public
    assert "_youtubeFormats" not in public and "_youtubeDuration" not in public
    assert public["pageUrl"] == asset["pageUrl"]
    assert store.read()["assets"][0]["sourceUrl"] == signed
