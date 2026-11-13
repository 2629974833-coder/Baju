"""Per-extraction EJS processes must stop before their scan slot is released."""

import importlib
import os
import threading
import time
from types import SimpleNamespace

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP


def runtime_module():
    module = importlib.import_module("backend.youtube_media")
    assert hasattr(module, "YouTubeRuntime"), "EJS subprocess lifetime is not bounded"
    return module


def provider(module):
    path = module.youtube_ydl_params()["js_runtimes"]["deno"].get("path")
    if not path:
        pytest.skip("Deno dependency is needed for real runtime verification")
    return SimpleNamespace(
        runtime_info=SimpleNamespace(path=path), _get_env_options=lambda: os.environ.copy()
    )


def test_deno_normal_output_still_runs_under_restricted_official_options():
    module = runtime_module()
    control = module.YouTubeRuntime(deadline=time.monotonic() + 5)
    result = control.run_deno(
        provider(module), "console.log(JSON.stringify({ok: true}))", DenoJCP._DENO_BASE_OPTIONS
    )
    assert result.strip() == '{"ok":true}'


@pytest.mark.parametrize("reason", ["cancelled", "deadline", "output-budget"])
def test_long_running_or_noisy_process_is_killed_and_reaped(reason, monkeypatch):
    module = runtime_module()
    created = []
    real_popen = module.subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    stopped = threading.Event()

    def check():
        if stopped.is_set():
            raise RuntimeError("fixture cancelled")

    timer = threading.Timer(0.15, stopped.set)
    deadline = time.monotonic() + (0.15 if reason == "deadline" else 5)
    control = module.YouTubeRuntime(check=check, deadline=deadline)
    script = "while (true) {}"
    if reason == "output-budget":
        script = 'while (true) console.log("x".repeat(65536));'
    if reason == "cancelled":
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises((RuntimeError, module.YouTubeRuntimeStopped)):
            control.run_deno(provider(module), script, DenoJCP._DENO_BASE_OPTIONS)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2
    assert created and all(p.poll() is not None for p in created)


def test_provider_replacement_is_scoped_to_one_extractor_instance():
    module = runtime_module()
    original = DenoJCP._run_deno
    with (
        YoutubeDL({"quiet": True, **module.youtube_ydl_params()}, auto_init=False) as one,
        YoutubeDL({"quiet": True, **module.youtube_ydl_params()}, auto_init=False) as two,
    ):
        first = module.YouTubeIE(one, runtime_deadline=time.monotonic() + 2)
        second = module.YouTubeIE(two, runtime_deadline=time.monotonic() + 3)
        first.initialize()
        second.initialize()
        first_deno = next(
            p for p in first._jsc_director.providers.values() if isinstance(p, DenoJCP)
        )
        second_deno = next(
            p for p in second._jsc_director.providers.values() if isinstance(p, DenoJCP)
        )
        assert first_deno is not second_deno
        assert first_deno._run_deno is not second_deno._run_deno
        assert first_deno._run_deno.func.__self__ is first._runtime
        assert second_deno._run_deno.func.__self__ is second._runtime
        assert DenoJCP._run_deno is original
