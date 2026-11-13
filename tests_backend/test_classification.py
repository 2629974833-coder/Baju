import pytest
from fastapi.testclient import TestClient

from backend.classification import classify_asset
from backend.scanner import Scanner
from tests_backend.test_app import app_at, until


def video(source, **extra):
    return {"kind": "video", "sourceUrl": source, **extra}


@pytest.mark.parametrize("marker", ["member-previews", "trailer", "teaser", "preview"])
def test_explicit_preview_beats_episode_page_and_length(marker):
    asset = video(
        f"https://media.example/{marker}/episode-1.mp4?token=secret",
        pageUrl="https://example.com/episodes/1",
        duration=3600,
    )
    result = classify_asset(asset)
    assert result["kind"] == "preview" and result["confidence"] == "high"
    assert "secret" not in str(result) and "https://" not in str(result)


def test_lustflix_short_episode_and_audio_are_distinct():
    source = "https://lustflix.tv/api/media/series/1/"
    page = "https://lustflix.tv/#/watch/C04/1"
    assert (
        classify_asset(video(source + "master.m3u8", pageUrl=page, duration=31.2))["kind"]
        == "episode"
    )
    assert classify_asset(video(source + "stream_audio0.m3u8", pageUrl=page))["kind"] == "audio"
    assert (
        classify_asset(video(source + "master.m3u8", pageUrl=page.replace("/1", "/2")))["kind"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "role,kind",
    [
        ("master", "episode"),
        ("stream_0", "episode"),
        ("stream_2", "episode"),
        ("stream_audio0", "audio"),
    ],
)
def test_paid_playback_media_role_is_classified_without_interpreting_opaque_signature(role, kind):
    source = f"https://lustflix.tv/api/playback-media/synthetic-preview-audio-token/daughter-kingdom-night/5/{role}.m3u8"
    assert (
        classify_asset(video(source, pageUrl="https://lustflix.tv/#/watch/C03/5"))["kind"] == kind
    )


@pytest.mark.parametrize(
    "host,path,page",
    [
        (
            "lustflix.tv",
            "synthetic-token/daughter-kingdom-night/6/master.m3u8",
            "https://lustflix.tv/#/watch/C03/5",
        ),
        (
            "other.example",
            "synthetic-token/daughter-kingdom-night/5/master.m3u8",
            "https://lustflix.tv/#/watch/C03/5",
        ),
        (
            "lustflix.tv.evil.example",
            "synthetic-token/daughter-kingdom-night/5/master.m3u8",
            "https://lustflix.tv/#/watch/C03/5",
        ),
        (
            "lustflix.tv",
            "daughter-kingdom-night/5/synthetic-token/master.m3u8",
            "https://lustflix.tv/#/watch/C03/5",
        ),
        (
            "lustflix.tv",
            "synthetic-token/daughter-kingdom-night/5/master.m3u8",
            "https://other.example/#/watch/C03/5",
        ),
    ],
)
def test_paid_media_wrong_episode_or_origin_or_shape_is_not_promoted(host, path, page):
    assert (
        classify_asset(video(f"https://{host}/api/playback-media/{path}", pageUrl=page))["kind"]
        == "unknown"
    )


def test_paid_playback_preserves_real_preview_slug_and_player_label_priority():
    source = "https://lustflix.tv/api/playback-media/synthetic-token/daughter-kingdom-night/5/master.m3u8"
    page = "https://lustflix.tv/#/watch/C03/5"
    assert (
        classify_asset(video(source, pageUrl=page, _mediaLabel="Official trailer"))["kind"]
        == "preview"
    )
    assert (
        classify_asset(video(source.replace("daughter-kingdom-night", "trailer"), pageUrl=page))[
            "kind"
        ]
        == "preview"
    )


@pytest.mark.parametrize("duration", [10, 31.2, 3600, "60:00"])
def test_duration_and_episode_page_alone_cannot_prove_content(duration):
    asset = video(
        "https://cdn.example/random.mp4",
        pageUrl="https://example.com/episodes/1",
        title="Full Episode 1",
        duration=duration,
    )
    assert classify_asset(asset)["kind"] == "unknown"


def test_media_specific_evidence_and_false_token_matches():
    assert classify_asset(video("https://cdn.example/episodes/12.mp4"))["kind"] == "episode"
    assert (
        classify_asset(video("https://cdn.example/random.mp4", _mediaLabel="Official trailer"))[
            "kind"
        ]
        == "preview"
    )
    assert classify_asset(video("https://cdn.example/previewing.mp4"))["kind"] == "unknown"
    assert classify_asset(video("https://cdn.example/random.mp4?preview=true"))["kind"] == "unknown"
    assert (
        classify_asset(video("https://cdn.example/audio.mp4", mime="audio/mp4"))["kind"] == "audio"
    )


def test_scanner_retains_only_media_specific_label():
    record = Scanner._asset_record(
        {"mediaLabel": "Official trailer"},
        "https://cdn.example/opaque.mp4",
        "https://example.com/episode/1",
        "Full episode page",
        "video",
        "http",
        "Fixture",
    )
    assert record["_mediaLabel"] == "Official trailer"
    assert record["classification"]["kind"] == "preview"


def test_manual_classification_survives_and_reset_recomputes(tmp_path):
    app = app_at(tmp_path)
    asset = video("https://example.com/trailer.mp4", id="asset_class", status="discovered")
    app.state.store.mutate(lambda state: state["assets"].append(asset))
    with TestClient(app) as client:
        assert client.get("/api/state").json()["assets"][0]["classification"]["kind"] == "preview"
        path = "/api/assets/asset_class"
        assert client.patch(path, json={"classification": "episode"}).status_code == 200
        value = client.get("/api/state").json()["assets"][0]["classification"]
        assert value["kind"] == "episode" and value["source"] == "manual"
        assert client.patch(path, json={"classification": "complete-verified"}).status_code == 422
    with TestClient(app_at(tmp_path)) as client:
        assert client.get("/api/state").json()["assets"][0]["classification"]["source"] == "manual"
        assert client.patch(path, json={"classification": "automatic"}).status_code == 200
        value = client.get("/api/state").json()["assets"][0]["classification"]
        assert value["kind"] == "preview" and value["source"] == "automatic"


def test_rescan_refreshes_evidence_preserves_manual_and_private_label(tmp_path):
    app = app_at(tmp_path)

    class MediaScanner:
        label = "Official trailer"
        mime = "video/mp4"

        async def scan(self, site, options, on_asset, on_progress, cancelled):
            await on_asset(
                video(
                    "https://cdn.example/opaque.mp4",
                    pageUrl=options["url"],
                    title="Fixture",
                    engine="http",
                    _mediaLabel=self.label,
                    mime=self.mime,
                )
            )
            await on_progress({"status": "completed", "assetsFound": 1})

    scanner = MediaScanner()
    app.state.tasks.scanner = scanner
    with TestClient(app) as client:

        def scan():
            response = client.post(
                "/api/scans",
                json={"siteId": "chunlou", "url": "https://chunlou.tv", "depth": 0, "limit": 1},
            )
            assert response.status_code == 200
            return until(client, lambda state: state["scans"][0]["status"] == "completed")[
                "assets"
            ][0]

        asset = scan()
        assert asset["classification"]["kind"] == "preview"
        assert "_mediaLabel" not in asset
        path = f"/api/assets/{asset['id']}"
        client.patch(path, json={"classification": "episode", "tags": ["保留标签"]})
        scanner.label = "New trailer"
        asset = scan()
        assert asset["classification"]["source"] == "manual"
        assert asset["tags"] == ["保留标签"]
        client.patch(path, json={"classification": "automatic"})
        assert client.get("/api/state").json()["assets"][0]["classification"]["kind"] == "preview"
        scanner.label = ""
        assert scan()["classification"]["kind"] == "unknown"
        scanner.mime = "audio/mp4"
        assert scan()["classification"]["kind"] == "audio"
        scanner.mime = "video/mp4"
        assert scan()["classification"]["kind"] == "unknown"
