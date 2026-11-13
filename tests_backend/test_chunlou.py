from urllib.parse import urlsplit

import httpx
import pytest

from backend import chunlou
from backend.chunlou import drama_route, episode_route, inspect_playback, read_drama, read_episode

ORIGIN = "https://chunlou.tv"
TARGET = "a1111111-aaaa-4222-a333-000000000001"
OTHER = "b1111111-aaaa-4222-a333-000000000002"
BRANCH = "c1111111-aaaa-4222-a333-000000000003"
SHOW = "d1111111-aaaa-4222-a333-000000000004"
PAGE = f"{ORIGIN}/feed?ep={TARGET}"
DETAIL = f"{ORIGIN}/drama/field-notes"
PROJECT = "https://abcdefghijklmnop.supabase.co"
SHOW_ROW = {
    "id": SHOW,
    "title_i18n": {"zh": "自然观察"},
    "slug": "field-notes",
    "completed": True,
    "status": "published",
    "free_episodes": 1,
    "vip_only": False,
    "min_level": 0,
    "cover_path": f"{SHOW}/art.webp",
    "poster_path": "../unsafe.webp",
    "banner_path": f"{SHOW}/art.webp",
}
FIRST = {
    "id": TARGET,
    "drama_id": SHOW,
    "seq": 1,
    "kind": "main",
    "duration_sec": 32,
    "coin_price": 0,
    "preview_sec": 60,
    "preview_locked": False,
    "status": "published",
}
SECOND = {**FIRST, "id": OTHER, "seq": 2, "coin_price": 100, "preview_sec": 10}


class Policy:
    async def validate(self, url):
        assert urlsplit(url).hostname in {"chunlou.tv", "abcdefghijklmnop.supabase.co"}
        return url


@pytest.fixture
def source(monkeypatch):
    requests = []
    cases = {
        "rows": [FIRST, SECOND],
        "found": [{**FIRST, "drama": SHOW_ROW}],
        "show": [SHOW_ROW],
        "status": 200,
        "project": PROJECT,
        "offset": 0,
    }
    real = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        assert (
            request.method == "GET"
            and "authorization" not in request.headers
            and "cookie" not in request.headers
        )
        if request.url.host == "chunlou.tv":
            assert "apikey" not in request.headers
            content = {
                "/": '<script type="module" src="/assets/index-v1.js"></script>',
                "/assets/index-v1.js": 'import{a}from"./config-v1.js";',
                "/assets/config-v1.js": f"const config={{supabaseUrl:`{cases['project']}`,supabaseAnonKey:`sb_publishable_123456789012`}};",
            }[request.url.path]
            return httpx.Response(
                200, text=content, headers={"set-cookie": "sourceid=discard_me; Path=/"}
            )
        assert (
            request.url.host == urlsplit(PROJECT).hostname
            and request.headers["apikey"] == "sb_publishable_123456789012"
        )
        if request.url.path.endswith("/dramas"):
            assert request.url.params["slug"] == "eq.field-notes"
            records = cases["show"]
        elif "id" in request.url.params:
            assert request.url.params["id"] == "eq." + TARGET
            records = cases["found"]
        else:
            assert request.url.params["drama_id"] == "eq." + SHOW
            records = cases["rows"]
        return httpx.Response(
            cases["status"],
            json=records,
            headers={"content-range": f"0-{len(records) - 1}/{len(records) + cases['offset']}"},
        )

    monkeypatch.setattr(
        chunlou.httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return cases, requests


@pytest.mark.parametrize(
    "url",
    [
        PAGE.replace("chunlou.tv", "chunlou.example"),
        PAGE.replace("https:", "http:"),
        PAGE.replace("/feed?", "/foo/feed?"),
        PAGE + "&ep=" + OTHER,
        PAGE[:-1],
        PAGE.replace("//", "//user@"),
        PAGE.replace("tv/", "tv:444/"),
    ],
)
def test_wrong_source_or_ambiguous_episode_is_not_claimed(url):
    assert episode_route(url) is None


async def test_exact_published_identity_catalog_and_images(source):
    record = await read_episode(PAGE, Policy())
    assert record["id"] == TARGET and record["dramaId"] == SHOW and record["title"] == "自然观察"
    catalog = record["catalog"]
    assert (
        catalog["seriesKey"] == SHOW
        and catalog["catalogUrl"] == DETAIL
        and catalog["expectedCount"] == 2
    )
    assert [entry["pageUrl"] for entry in catalog["entries"]] == [PAGE, PAGE.replace(TARGET, OTHER)]
    assert [entry["access"] for entry in catalog["entries"]] == ["unknown", "needs-user"]
    assert record["images"] == [
        {"url": f"https://hls.chunlou.tv/cover/{SHOW}/art.webp", "title": "自然观察 · 封面"}
    ]
    assert (
        drama_route(DETAIL) == "field-notes"
        and drama_route(ORIGIN + "/feed?d=field-notes") == "field-notes"
    )
    assert (await read_drama(DETAIL, Policy()))["catalog"] == catalog


async def test_catalog_rejects_cross_drama_and_duplicate_sequences(source):
    cases, _ = source
    for rows in [[{**FIRST, "drama_id": OTHER}], [FIRST, {**SECOND, "seq": 1}], [SECOND]]:
        cases["rows"] = rows
        with pytest.raises(ValueError):
            await read_episode(PAGE, Policy())
    cases["rows"] = [FIRST, {**SECOND, "kind": "branch", "id": BRANCH}]
    assert len((await read_episode(PAGE, Policy()))["catalog"]["entries"]) == 1
    cases["status"] = 403
    with pytest.raises(chunlou.DirectoryAccessRequired):
        await read_episode(PAGE, Policy())


def playlist(ep=TARGET, duration=32.9, key="", ended=True):
    base = f"https://hls.chunlou.tv/hls/{ep}"
    variant = base + "/r1/v720/index.m3u8?sig=fixture"
    master = base + "/master.m3u8?token=&lang=zh"
    return {
        master: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1024,RESOLUTION=1280x720\n" + variant,
        variant: "#EXTM3U\n"
        + key
        + f"#EXTINF:{duration},\nseg_0000.ts\n"
        + ("#EXT-X-ENDLIST" if ended else ""),
    }


def test_actual_variant_exact_identity_full_duration_and_scheme_gate():
    record = chunlou._record(SHOW_ROW, [FIRST], TARGET)
    assert inspect_playback(record, playlist())["status"] == "ready"
    assert inspect_playback(record, playlist(OTHER))["status"] == "unobserved"
    assert inspect_playback(record, playlist(duration=10))["status"] == "preview"
    assert inspect_playback(record, playlist(ended=False))["status"] == "unobserved"
    manifests = playlist(
        key=f'#EXT-X-KEY:METHOD=AES-128,URI="chunlou://hls.chunlou.tv/key/{TARGET}/1"\n'
    )
    result = inspect_playback(record, manifests)
    assert result["status"] == "ready" and result["chunlouAes128"] is True
    assert (
        inspect_playback(record, playlist(key='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://test"\n'))[
            "status"
        ]
        == "unsupported"
    )
    incomplete = playlist()
    for url in list(incomplete):
        if "/r1/" in url:
            incomplete[url.replace("fixture", "other")] = incomplete.pop(url)
    assert inspect_playback(record, incomplete)["status"] == "unobserved"


def test_published_main_number_and_final_series_total_follow_publisher_evidence():
    ongoing = {**SHOW_ROW, "completed": False}
    first = {**FIRST, "seq": 4, "display_no": 1}
    record = chunlou._record(ongoing, [first, SECOND], TARGET)
    assert record["episode"]["ordinal"] == 1
    assert record["catalog"]["ongoing"] and record["catalog"]["publishedCount"] == 2
    assert record["catalog"]["expectedCount"] is None
    partial = chunlou._record(SHOW_ROW, [FIRST], TARGET, complete=False)
    assert partial["catalog"]["truncated"] and partial["catalog"]["expectedCount"] is None


@pytest.mark.parametrize(
    "tag",
    [
        "https://other.example/unjoined-segment.ts",
        "#EXT-X-BYTERANGE:32@0",
        '#EXT-X-MAP:URI="https://hls.chunlou.tv/hls/other/initial.mp4"',
        '#EXT-X-KEY:METHOD=NONE,URI="chunlou://hls.chunlou.tv/key/secret"',
        '#EXT-X-KEY:METHOD=AES-128,URI="https://hls.chunlou.tv/key/b1111111-aaaa-4222-a333-000000000002/1"',
    ],
)
def test_every_resource_in_a_rendition_must_be_supported_and_for_this_episode(tag):
    record = chunlou._record(SHOW_ROW, [FIRST], TARGET)
    result = inspect_playback(record, playlist(key=tag + "\n"))
    assert result["status"] != "ready" and "url" not in result


def test_separate_audio_does_not_publish_a_video_only_file_as_full_episode():
    data = playlist()
    master = next(iter(data))
    data[master] = data[master].replace("BANDWIDTH=1024,", 'BANDWIDTH=1024,AUDIO="commentary",')
    data[master] += (
        '\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="commentary",URI="https://audio.example/track.m3u8"'
    )
    result = inspect_playback(chunlou._record(SHOW_ROW, [FIRST], TARGET), data)
    assert result["status"] == "unsupported" and "音轨" in result["message"]


def test_standard_https_episode_key_and_scoped_initialization_segment_are_supported():
    key_uri = f"https://hls.chunlou.tv/key/{TARGET}/1"
    tags = f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri}",IV=0x01\n#EXT-X-MAP:URI="init.mp4"\n'
    result = inspect_playback(chunlou._record(SHOW_ROW, [FIRST], TARGET), playlist(key=tags))
    assert result["status"] == "ready"


@pytest.mark.parametrize("error_code", [302, 404, 429, 502])
async def test_directory_errors_do_not_masquerade_as_an_empty_complete_catalog(source, error_code):
    cases, _ = source
    cases["status"] = error_code
    with pytest.raises(ValueError):
        await read_episode(PAGE, Policy())


async def test_directory_config_cannot_forward_credentials_to_an_unrelated_project_host(source):
    cases, requests = source
    cases["project"] = PROJECT + ".unrelated.example"
    with pytest.raises(ValueError, match="公开目录配置"):
        await read_episode(PAGE, Policy())
    assert all(request.url.host == "chunlou.tv" for request in requests)


async def test_incomplete_public_directory_is_kept_but_not_marked_complete(source):
    cases, _ = source
    cases["offset"] = 3
    record = await read_episode(PAGE, Policy())
    assert record["catalog"]["publishedCount"] == 2
    assert record["catalog"]["expectedCount"] is None and record["catalog"]["truncated"]


def test_priced_episode_in_public_free_range_can_be_scanned():
    free_show = {**SHOW_ROW, "free_episodes": 2}
    record = chunlou._record(free_show, [FIRST, SECOND], OTHER)
    assert not record["episode"]["restricted"]
    assert record["catalog"]["entries"][1]["access"] == "unknown"
    vip = chunlou._record({**free_show, "vip_only": True}, [FIRST, SECOND], OTHER)
    assert vip["catalog"]["entries"][1]["accessEvidence"] == "public-directory"


@pytest.mark.parametrize("session_key", [False, True])
def test_protected_vod_matching_declared_short_preview_is_needs_access(session_key):
    record = chunlou._record(SHOW_ROW, [FIRST, SECOND], OTHER)
    key = f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="chunlou://hls.chunlou.tv/key/{OTHER}/1"\n'
    clips = playlist(OTHER, duration=10.3, key="" if session_key else key)
    if session_key:
        master = next(iter(clips))
        clips[master] = clips[master].replace(
            "#EXTM3U\n", "#EXTM3U\n" + key.replace("EXT-X-KEY", "EXT-X-SESSION-KEY")
        )
    result = inspect_playback(record, clips)
    assert result["status"] == "preview" and "url" not in result
    variant = next(url for url in clips if "v720" in url)
    clips[variant] = clips[variant].replace("#EXT-X-ENDLIST", "")
    assert inspect_playback(record, clips)["status"] == "unsupported"
    assert (
        inspect_playback(record, playlist(OTHER, duration=32, key=key))["status"] == "unsupported"
    )
    assert (
        inspect_playback(record, playlist(OTHER, duration=22, key=key))["status"] == "unsupported"
    )

def test_chunlou_key_alias_preserves_query_bytes_and_episode_scope():
    uri = f"chunlou://hls.chunlou.tv/key/{TARGET}/2?e=123&n=a%2Fb&s=c%2Bq"
    assert chunlou.chunlou_key_url(uri, TARGET) == uri.replace("chunlou://", "https://", 1)
    invalid = [
        uri.replace(TARGET, OTHER), uri.replace("/key/", "/hls/"), uri + "#frag",
        uri.replace("hls.", "user@hls."), uri.replace(".tv/", ".tv:443/"),
        uri.replace(".tv/", ".tv.example/"), uri.replace("/2?", "/../2?"),
        uri.replace("/2?", "/%32?"), uri.replace("?e=", "?e=\r\n"),
        uri.replace("/2?", "/2/extra?"), uri.replace("chunlou://", "http://"),
    ]
    for item in invalid:
        assert chunlou.chunlou_key_url(item, TARGET) is None
