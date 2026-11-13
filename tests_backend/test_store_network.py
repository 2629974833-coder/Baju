import pytest

from backend.network import NetworkPolicy
from backend.store import Store


def test_real_state_has_targets_but_no_demo_assets(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    state = store.read()
    assert state["mode"] == "real"
    assert state["assets"] == [] and state["jobs"] == []
    assert {s["domain"] for s in state["sites"]} == {
        "lustflix.tv",
        "candy.ai",
        "chunlou.tv",
        "moolight.tv",
    }
    assert all(s["auth"] == "disconnected" for s in state["sites"])


def test_store_mutation_is_atomic_and_survives_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.mutate(lambda state: state["settings"].update(concurrency=2))
    assert Store(path).read()["settings"]["concurrency"] == 2
    state = store.read()
    state["settings"]["concurrency"] = 99
    assert store.read()["settings"]["concurrency"] == 2

    def fail(state):
        state["settings"]["concurrency"] = 3
        raise ValueError("rollback")

    with pytest.raises(ValueError):
        store.mutate(fail)
    assert store.read()["settings"]["concurrency"] == 2


def test_public_state_hides_internal_download_details(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.mutate(
        lambda s: s["assets"].append(
            {
                "id": "a1",
                "_headers": {"Authorization": "secret"},
                "_path": "assets/a1/a.jpg",
                "title": "a",
            }
        )
    )
    public = store.public_state()
    assert "_headers" not in public["assets"][0]
    assert "_path" not in public["assets"][0]
    assert public["assets"][0]["title"] == "a"


@pytest.mark.asyncio
async def test_policy_blocks_dangerous_schemes_and_private_hosts():
    policy = NetworkPolicy()
    for url in [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://127.0.0.1/x",
        "http://[::1]/",
        "http://169.254.169.254/",
        "https://user:pass@example.com/",
    ]:
        with pytest.raises(ValueError):
            await policy.validate(url)


@pytest.mark.asyncio
async def test_policy_allows_only_explicit_fixture_host():
    policy = NetworkPolicy(allow_private_hosts={"127.0.0.1"})
    assert (
        await policy.validate("http://127.0.0.1:8910/image?a=1")
        == "http://127.0.0.1:8910/image?a=1"
    )
    with pytest.raises(ValueError):
        await policy.validate("http://[::1]:8910/")
