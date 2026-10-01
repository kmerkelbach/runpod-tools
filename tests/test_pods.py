import pytest

from runpod_tools.pods import (
    SelectionError,
    SshEndpoint,
    cost_so_far,
    format_uptime,
    gpu_label,
    select_pods,
    ssh_endpoint,
    uptime_seconds,
)
from tests.fakes import pod


def test_ssh_endpoint_from_tcp_port_22():
    assert ssh_endpoint(pod()) == SshEndpoint("1.2.3.4", 40022)


def test_ssh_endpoint_none_when_no_tcp_22():
    p = pod(runtime={"uptimeInSeconds": 1, "ports": [{"ip": "1.2.3.4", "privatePort": 8888, "publicPort": 1, "type": "http"}]})
    assert ssh_endpoint(p) is None


def test_ssh_endpoint_none_when_host_has_no_public_ip():
    # Secure-cloud hosts without a public IP report no port entry at all.
    assert ssh_endpoint(pod(runtime={"uptimeInSeconds": 1, "ports": None})) is None
    assert ssh_endpoint(pod(runtime=None)) is None


def test_gpu_label_single_and_multi():
    assert gpu_label(pod()) == "H200 SXM"
    assert gpu_label(pod(gpuCount=2)) == "2x H200 SXM"
    assert gpu_label(pod(machine=None, gpuCount=1)) == "1x GPU"


def test_uptime_and_formatting():
    assert uptime_seconds(pod()) == 5400
    assert uptime_seconds(pod(runtime=None)) is None
    assert format_uptime(5400) == "1h 30m"
    assert format_uptime(120) == "2m"
    assert format_uptime(None) == "-"


def test_cost_so_far():
    assert cost_so_far(pod()) == pytest.approx(3.0)
    assert cost_so_far(pod(runtime=None)) is None
    assert cost_so_far(pod(costPerHr=None)) is None


# --- selection ---------------------------------------------------------------

RUNNING_A = pod(id="aaa111", name="alice_eval_1")
RUNNING_B = pod(id="bbb222", name="alice_eval_2")
STOPPED_C = pod(id="ccc333", name="alice_train", desiredStatus="EXITED")
ALL = [RUNNING_A, RUNNING_B, STOPPED_C]


def select(**kw):
    kw.setdefault("ids", None)
    kw.setdefault("select_all", False)
    kw.setdefault("statuses", ["RUNNING"])
    kw.setdefault("interactive", False)
    return select_pods(ALL, **kw)


def test_select_all_returns_every_pod_in_status():
    assert select(select_all=True) == [RUNNING_A, RUNNING_B]


def test_select_by_exact_ids():
    assert select(ids="bbb222,aaa111") == [RUNNING_B, RUNNING_A]


def test_select_by_unique_name_prefix():
    assert select(ids="alice_eval_2") == [RUNNING_B]


def test_select_ambiguous_prefix_lists_candidates():
    with pytest.raises(SelectionError) as exc:
        select(ids="alice_eval")
    assert exc.value.exit_code == 2
    assert "alice_eval_1" in str(exc.value) and "alice_eval_2" in str(exc.value)


def test_select_unknown_id_is_an_error():
    with pytest.raises(SelectionError, match="zzz"):
        select(ids="zzz")


def test_select_id_in_wrong_status_is_an_error():
    with pytest.raises(SelectionError, match="EXITED"):
        select(ids="ccc333", statuses=["RUNNING"])


def test_select_single_candidate_auto_selected():
    assert select(statuses=["EXITED"]) == [STOPPED_C]


def test_select_no_candidates_is_an_error():
    with pytest.raises(SelectionError, match="No pods"):
        select_pods([], ids=None, select_all=False, statuses=["RUNNING"], interactive=False)


def test_select_ambiguous_non_interactive_exits_2_with_list():
    with pytest.raises(SelectionError) as exc:
        select()
    assert exc.value.exit_code == 2
    assert "--pod" in str(exc.value) and "aaa111" in str(exc.value) and "bbb222" in str(exc.value)


def test_select_interactive_prompt_picks_by_number():
    answers = iter(["9", "2"])
    chosen = select(interactive=True, prompt=lambda _msg: next(answers))
    assert chosen == [RUNNING_B]


def test_select_interactive_multi_accepts_all():
    chosen = select(interactive=True, multi=True, prompt=lambda _msg: "all")
    assert chosen == [RUNNING_A, RUNNING_B]


def test_select_interactive_eof_is_an_error():
    def prompt(_msg):
        raise EOFError

    with pytest.raises(SelectionError):
        select(interactive=True, prompt=prompt)


# --- name_prefix scoping (review fix) ------------------------------------------

OTHER = pod(id="ddd444", name="bob_job")
SHARED = [RUNNING_A, RUNNING_B, STOPPED_C, OTHER]


def test_select_all_respects_name_prefix():
    got = select_pods(SHARED, ids=None, select_all=True, statuses=["RUNNING"], interactive=False, name_prefix="alice_")
    assert got == [RUNNING_A, RUNNING_B]


def test_auto_select_respects_name_prefix():
    pods = [OTHER, STOPPED_C]
    got = select_pods(pods, ids=None, select_all=False, statuses=["EXITED"], interactive=False, name_prefix="alice_")
    assert got == [STOPPED_C]
    with pytest.raises(SelectionError, match="alice_"):
        select_pods([OTHER], ids=None, select_all=False, statuses=["RUNNING"], interactive=False, name_prefix="alice_")


def test_explicit_id_can_reach_pods_outside_prefix():
    got = select_pods(SHARED, ids="ddd444", select_all=False, statuses=["RUNNING"], interactive=False, name_prefix="alice_")
    assert got == [OTHER]


def test_ssh_endpoint_ignores_private_ip_mapping():
    p = pod(runtime={"uptimeInSeconds": 1, "ports": [
        {"ip": "10.0.0.5", "isIpPublic": False, "privatePort": 22, "publicPort": 22, "type": "tcp"}]})
    assert ssh_endpoint(p) is None


def test_ssh_endpoint_accepts_missing_is_ip_public():
    p = pod(runtime={"uptimeInSeconds": 1, "ports": [
        {"ip": "1.2.3.4", "privatePort": 22, "publicPort": 40022, "type": "tcp"}]})
    assert ssh_endpoint(p) == SshEndpoint("1.2.3.4", 40022)
