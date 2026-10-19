"""The shared pod listing: endpoint-only commands read it, everything else lists fresh."""

import threading
from pathlib import Path

import pytest

from runpod_tools.api import RunpodClient, RunpodError
from runpod_tools.podcache import DEFAULT_TTL, PodCache, cache_from_env
from tests.fakes import RATE_LIMITED, FakeClock, FakeTransport, graphql_ok, pod
from tests.test_cli import run
from tests.test_remote import Recorder, cfg, listing

READY = pod(id="p1", name="a_pod")
OTHER = pod(id="p2", name="b_pod")
STARTING = pod(id="p1", name="a_pod", runtime=None)


class WallClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return WallClock()


@pytest.fixture
def cache(tmp_path: Path, clock: WallClock) -> PodCache:
    return PodCache(tmp_path / "pods-acct.json", ttl=30, stale_limit=600, clock=clock)


def client(transport: FakeTransport, cache: PodCache, wait: float = 0) -> RunpodClient:
    fake = FakeClock()
    return RunpodClient("k", transport=transport, pod_cache=cache, rate_limit_wait=wait,
                        sleep=fake.sleep, clock=fake, log=lambda _m: None)


def test_fresh_listing_writes_the_cache(cache):
    t = FakeTransport().queue(*listing(READY))
    assert client(t, cache).list_pods() == [READY]
    assert cache.read(30) == [READY]


def test_cached_pods_reuses_a_current_listing(cache):
    cache.write([READY])
    t = FakeTransport()
    assert client(t, cache).cached_pods() == ([READY], True)
    assert t.calls == []


def test_cached_pods_refreshes_an_expired_listing(cache, clock):
    cache.write([READY])
    clock.now += 31
    t = FakeTransport().queue(*listing(READY, OTHER))
    assert client(t, cache).cached_pods() == ([READY, OTHER], False)
    assert cache.read(30) == [READY, OTHER]


def test_rate_limited_refresh_falls_back_to_a_stale_listing(cache, clock):
    cache.write([READY])
    clock.now += 300
    t = FakeTransport().queue(*RATE_LIMITED)
    assert client(t, cache).cached_pods() == ([READY], True)


def test_rate_limited_refresh_without_a_recent_listing_fails(cache, clock):
    cache.write([READY])
    clock.now += 601
    t = FakeTransport().queue(*RATE_LIMITED)
    with pytest.raises(RunpodError) as exc:
        client(t, cache).cached_pods()
    assert exc.value.status == 429


def test_other_errors_never_use_a_stale_listing(cache, clock):
    cache.write([READY])
    clock.now += 60
    t = FakeTransport().queue(500, {"error": "boom"})
    with pytest.raises(RunpodError):
        client(t, cache).cached_pods()


def test_plain_list_pods_never_reads_the_cache(cache):
    cache.write([READY])
    t = FakeTransport().queue(*RATE_LIMITED)
    with pytest.raises(RunpodError):
        client(t, cache).list_pods()


@pytest.mark.parametrize("action", ["create", "stop", "resume", "terminate"])
def test_changes_to_pods_drop_the_cache(cache, action):
    cache.write([READY])
    t = FakeTransport()
    c = client(t, cache)
    if action == "create":
        t.queue(200, {"id": "new"})
        c.create_pod({"name": "x"})
    elif action == "stop":
        t.queue(204, b"")
        c.stop_pod("p1")
    elif action == "resume":
        t.queue(*graphql_ok({"podResume": {"id": "p1"}}))
        c.resume_pod("p1")
    else:
        t.queue(*graphql_ok({"podTerminate": None}))
        c.terminate_pod("p1")
    assert cache.read(30) is None


def test_corrupt_cache_counts_as_missing(cache):
    cache.path.write_text("{not json")
    assert cache.read(30) is None


def test_parallel_callers_share_one_refresh(cache):
    """Threads stand in for parallel rpt processes: the lock is an flock on a file."""
    t = FakeTransport().queue(*listing(READY))
    lock = threading.Lock()

    def guarded(*args):
        with lock:
            return t(*args)

    results = []

    def worker():
        c = RunpodClient("k", transport=guarded, pod_cache=cache, log=lambda _m: None)
        results.append(c.cached_pods()[0])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [[READY]] * 8
    assert len(t.calls) == 1


def test_cache_from_env_is_per_account_and_can_be_disabled(tmp_path):
    env = {"RPT_CACHE_DIR": str(tmp_path)}
    a, b = cache_from_env("key-a", env), cache_from_env("key-b", env)
    assert a.path != b.path and a.path.parent == tmp_path
    assert "key-a" not in str(a.path)
    assert a.ttl == DEFAULT_TTL
    assert cache_from_env("key-a", {**env, "RPT_POD_CACHE_TTL": "0"}) is None
    assert cache_from_env("key-a", {**env, "RPT_POD_CACHE_TTL": "5"}).ttl == 5


# -- commands ------------------------------------------------------------------


def test_run_uses_the_cached_listing(cache):
    cache.write([READY])
    rec = Recorder()
    assert run(["run", "--pod", "p1", "--", "true"], FakeTransport(), config=cfg(), runner=rec, pod_cache=cache) == 0
    assert len(rec.calls) == 1


def test_run_lists_fresh_when_the_pod_is_not_cached(cache):
    cache.write([OTHER])
    t = FakeTransport().queue(*listing(READY, OTHER))
    rec = Recorder()
    assert run(["run", "--pod", "p1", "--", "true"], t, config=cfg(), runner=rec, pod_cache=cache) == 0
    assert len(t.calls) == 1 and len(rec.calls) == 1


def test_run_lists_fresh_when_the_cached_pod_has_no_endpoint(cache):
    cache.write([STARTING])
    t = FakeTransport().queue(*listing(READY))
    rec = Recorder()
    assert run(["run", "--pod", "p1", "--", "true"], t, config=cfg(), runner=rec, pod_cache=cache) == 0
    assert "40022" in rec.calls[0][0]


def test_ssh_print_format_is_unchanged_with_the_cache(cache, capsys):
    cache.write([READY])
    assert run(["ssh", "--pod", "p1", "--print"], FakeTransport(), config=cfg(), pod_cache=cache) == 0
    assert capsys.readouterr().out.strip().startswith("ssh root@1.2.3.4 -p 40022 -i /k/id")


def test_pods_command_always_lists_fresh(cache, capsys):
    cache.write([READY])
    t = FakeTransport().queue(*RATE_LIMITED)
    assert run(["pods"], t, pod_cache=cache) == 1
    assert len(t.calls) == 1


def test_run_all_lists_fresh_even_with_a_current_cache(cache):
    cache.write([READY])
    t = FakeTransport().queue(*listing(READY, OTHER))
    rec = Recorder()
    assert run(["run", "--all", "--", "true"], t, config=cfg(), runner=rec, pod_cache=cache) == 0
    assert len(t.calls) == 1 and len(rec.calls) == 2


def test_waiters_take_the_stale_listing_after_a_failed_refresh(cache, clock):
    cache.write([READY])
    clock.now += 120
    cache.mark_refresh_failed()
    t = FakeTransport()
    assert client(t, cache).cached_pods() == ([READY], True)
    assert t.calls == []


def test_an_old_failed_refresh_is_tried_again(cache, clock):
    cache.write([READY])
    cache.mark_refresh_failed()
    clock.now += 120
    t = FakeTransport().queue(*listing(READY, OTHER))
    assert client(t, cache).cached_pods() == ([READY, OTHER], False)


def test_a_stuck_lock_holder_does_not_block_forever(cache):
    import fcntl

    cache.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache.lock_path, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = []

        def waiter():
            with cache.refreshing(timeout=0.5, poll=0.05):
                result.append("ran")

        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=5)
        assert result == ["ran"]


def test_a_change_drops_a_listing_written_during_it(cache):
    class Racing(FakeTransport):
        def __call__(self, *args):
            cache.write([READY])  # a refresh in flight lands mid-change
            return super().__call__(*args)

    t = Racing().queue(204, b"")
    client(t, cache).stop_pod("p1")
    assert cache.read(30) is None
