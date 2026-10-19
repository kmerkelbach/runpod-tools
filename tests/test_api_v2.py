"""REST v2 behind the v1 interface: the same commands, fed equivalent v1/GraphQL
and v2 responses, print the same thing."""


import pytest

from runpod_tools.api import RunpodClient, RunpodError
from runpod_tools.api_v2 import (
    V2_BASE,
    RestV2Client,
    client_v2_from_env,
    create_bodies,
    legacy_pod,
    legacy_template,
)
from runpod_tools.cli import main
from runpod_tools.config import Config
from runpod_tools.podcache import PodCache
from tests.fakes import FakeTransport, graphql_ok, pod
from tests.test_remote import Recorder, cfg

GPU_ID = "NVIDIA H200"

CATALOG = {"gpus": [
    {"id": GPU_ID, "name": "H200 SXM", "memory": 141, "secure": True, "community": False,
     "price": {"secure": 3.99, "community": 0.0}, "maxCount": {"secure": 8, "community": 0}},
    {"id": "NVIDIA RTX A4000", "name": "RTX A4000", "memory": 16, "secure": False, "community": True,
     "price": {"secure": 0.0, "community": 0.17}, "maxCount": {"secure": 0, "community": 4}},
]}


def v2_pod(**over) -> dict:
    """The v2 pod equivalent to tests.fakes.pod()."""
    base = {
        "id": "abc123", "name": "my_pod", "status": "RUNNING", "image": "example/image:tag",
        "cost": 2.0, "gpu": {"id": GPU_ID, "count": 1, "vcpuCount": 16, "memory": 120},
        "cloud": "SECURE", "dataCenterId": "EU-SE-1", "ports": ["22/tcp", "8000/http"],
        "ssh": {"direct": {"host": "1.2.3.4", "port": 40022, "username": "root", "command": "ssh ..."},
                "proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc123-1234", "command": "ssh ..."}},
        "runtime": {"uptime": 5400, "gpus": [{"util": 98, "memoryUtil": 80}],
                    "ports": [{"private": 22, "public": 40022, "type": "tcp", "ip": "1.2.3.4"}]},
        "startedAt": "2026-10-19T20:00:00Z", "createdAt": "2026-10-19T19:59:00Z",
    }
    base.update(over)
    return base


def v2_listing(*pods):
    return 200, {"pods": list(pods), "pagination": {"nextCursor": None, "hasNextPage": False}}


def run_with(client, argv, *, config=None, runner=None):
    def forbidden(*a, **k):
        raise AssertionError(f"unexpected subprocess: {a} {k}")

    return main(argv, client_factory=lambda **_: client, config_loader=lambda: config or Config(),
                stdin_isatty=lambda: False, env={"RUNPOD_API_KEY": "k"}, runner=runner or forbidden,
                execvp=forbidden)


def v1(transport):
    return RunpodClient("k", transport=transport, rate_limit_wait=0, log=lambda _m: None)


def v2(transport, catalog: PodCache | None = None):
    return RestV2Client("k", transport=transport, rate_limit_wait=0, log=lambda _m: None, catalog_cache=catalog)


def both_outputs(capsys, argv, v1_responses, v2_responses, **kw):
    t1 = FakeTransport()
    for r in v1_responses:
        t1.queue(*r)
    code1 = run_with(v1(t1), argv, **kw)
    out1 = capsys.readouterr()
    t2 = FakeTransport()
    for r in v2_responses:
        t2.queue(*r)
    code2 = run_with(v2(t2), argv, **kw)
    out2 = capsys.readouterr()
    return (code1, out1.out), (code2, out2.out), t2


# -- parity ---------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["pods"], ["pods", "--json"], ["pods", "--status", "RUNNING"]])
def test_pods_output_is_identical(capsys, argv):
    one, two, _ = both_outputs(
        capsys, argv,
        [graphql_ok({"myself": {"pods": [pod(), pod(id="st1", name="stopped_pod", desiredStatus="EXITED",
                                                       costPerHr=0.0, runtime=None)]}})],
        [(200, CATALOG), v2_listing(v2_pod(), v2_pod(id="st1", name="stopped_pod", status="EXITED", cost=0.0,
                                                       runtime=None, ssh={"direct": None, "proxy": {"username": "abc123-1234"}}))],
    )
    assert one == two


def test_pods_table_columns_peers_parse_are_unchanged(capsys):
    """Peers' scripts read running rows and take $/hr as the second-to-last field."""
    _, (code, out), _ = both_outputs(capsys, ["pods"], [graphql_ok({"myself": {"pods": [pod()]}})],
                                     [(200, CATALOG), v2_listing(v2_pod())])
    row = next(line for line in out.splitlines() if " running " in line)
    assert row.split()[-2] == "2.000"


def test_ssh_print_is_identical(capsys):
    one, two, _ = both_outputs(capsys, ["ssh", "--pod", "abc123", "--print"],
                               [graphql_ok({"myself": {"pods": [pod()]}})],
                               [(200, CATALOG), v2_listing(v2_pod())], config=cfg())
    assert one == two
    assert two[1].startswith("ssh root@1.2.3.4 -p 40022 -i /k/id")


def test_run_uses_the_same_ssh_argv(capsys):
    rec1, rec2 = Recorder(), Recorder()
    run_with(v1(FakeTransport().queue(*graphql_ok({"myself": {"pods": [pod()]}}))),
             ["run", "--pod", "abc123", "--", "true"], config=cfg(), runner=rec1)
    run_with(v2(FakeTransport().queue(200, CATALOG).queue(*v2_listing(v2_pod()))),
             ["run", "--pod", "abc123", "--", "true"], config=cfg(), runner=rec2)
    assert rec1.calls[0][0] == rec2.calls[0][0]


def test_a_pod_without_direct_ssh_has_no_endpoint(capsys):
    no_direct = v2_pod(ssh={"direct": None, "proxy": {"username": "abc123-1234"}},
                       runtime={"uptime": 3, "ports": [{"private": 22, "public": None, "type": "tcp", "ip": None}]})
    one, two, _ = both_outputs(capsys, ["pods"],
                               [graphql_ok({"myself": {"pods": [pod(runtime={"uptimeInSeconds": 3, "ports": None})]}})],
                               [(200, CATALOG), v2_listing(no_direct)])
    assert "no TCP endpoint" in two[1]
    assert "proxy (terminal only" in two[1] and "abc123-1234@ssh.runpod.io" in two[1]


def test_gpus_output_matches_for_on_demand_prices(capsys):
    gql = [{"id": GPU_ID, "displayName": "H200 SXM", "memoryInGb": 141, "secureCloud": True,
            "communityCloud": False, "lowestPrice": {"minimumBidPrice": None, "uninterruptablePrice": 3.99}},
           {"id": "NVIDIA RTX A4000", "displayName": "RTX A4000", "memoryInGb": 16, "secureCloud": False,
            "communityCloud": True, "lowestPrice": {"minimumBidPrice": None, "uninterruptablePrice": 0.17}}]
    one, two, _ = both_outputs(capsys, ["gpus"], [graphql_ok({"gpuTypes": gql})], [(200, CATALOG)])
    assert one == two


def test_volumes_output_is_identical(capsys):
    one, two, t2 = both_outputs(
        capsys, ["volumes"],
        [(200, [{"id": "vol1", "name": "shared", "size": 4000, "dataCenterId": "US-NC-1"}])],
        [(200, {"networkVolumes": [{"id": "vol1", "name": "shared", "size": 4000, "dataCenter": "US-NC-1",
                                    "type": "STANDARD"}], "pagination": {"hasNextPage": False}})],
    )
    assert one == two
    assert t2.calls[0].url.startswith(V2_BASE + "/network-volumes")


V2_TEMPLATE = {"id": "tpl1", "name": "llm-research", "image": "repo/img:tag", "args": "",
               "disk": 50, "ports": ["22/tcp", "8888/http"], "env": {"HF_TOKEN": "{{ RUNPOD_SECRET_HF_TOKEN_WRITE }}"},
               "mounts": {"persistent": {"size": 100, "path": "/workspace"}}, "registry": None,
               "serverless": False, "public": False, "category": "NVIDIA", "startSsh": True,
               "startJupyter": False, "allowedCudaVersions": []}
GQL_TEMPLATE = {"id": "tpl1", "name": "llm-research", "imageName": "repo/img:tag", "ports": "22/tcp,8888/http",
                "containerDiskInGb": 50, "volumeInGb": 100, "volumeMountPath": "/workspace", "dockerArgs": "",
                "containerRegistryAuthId": None, "readme": "", "isPublic": False, "isServerless": False,
                "startSsh": True, "startJupyter": False,
                "env": [{"key": "HF_TOKEN", "value": "{{ RUNPOD_SECRET_HF_TOKEN_WRITE }}"}]}


def test_template_show_is_identical(capsys):
    one, two, _ = both_outputs(capsys, ["template", "show", "--template-id", "tpl1"],
                               [graphql_ok({"myself": {"podTemplates": [GQL_TEMPLATE]}})], [(200, V2_TEMPLATE)])
    assert one == two


def test_template_ports_patches_only_ports(capsys):
    t = FakeTransport().queue(200, V2_TEMPLATE).queue(200, {**V2_TEMPLATE, "ports": ["22/tcp", "8888/http", "8000/http"]})
    assert run_with(v2(t), ["template", "ports", "--template-id", "tpl1", "--add", "8000/http", "-y"]) == 0
    assert t.last.method == "PATCH" and t.last.url == V2_BASE + "/templates/tpl1"
    assert t.last.body == {"ports": ["22/tcp", "8888/http", "8000/http"]}
    assert "8000/http" in capsys.readouterr().out


def test_template_volume_patches_only_the_mount(capsys):
    t = FakeTransport().queue(200, V2_TEMPLATE).queue(200, {**V2_TEMPLATE, "mounts": {"persistent": {"size": 200, "path": "/workspace"}}})
    assert run_with(v2(t), ["template", "volume", "--template-id", "tpl1", "--gb", "200", "-y"]) == 0
    assert t.last.body == {"mounts": {"persistent": {"size": 200, "path": "/workspace"}}}
    assert "200 GB" in capsys.readouterr().out


def test_template_env_patch_sends_the_merged_env(capsys):
    t = FakeTransport().queue(200, V2_TEMPLATE).queue(200, V2_TEMPLATE)
    assert run_with(v2(t), ["template", "env", "--template-id", "tpl1", "--env", "FOO=bar", "-y"]) == 0
    assert t.last.body == {"env": {"HF_TOKEN": "{{ RUNPOD_SECRET_HF_TOKEN_WRITE }}", "FOO": "bar"}}


# -- lifecycle --------------------------------------------------------------------


@pytest.mark.parametrize("action,status,method,path,body", [
    ("stop", "RUNNING", "POST", "/pods/abc123/action", {"action": "stop"}),
    ("resume", "EXITED", "POST", "/pods/abc123/action", {"action": "start"}),
    ("terminate", "RUNNING", "DELETE", "/pods/abc123", None),
])
def test_lifecycle_actions(capsys, action, status, method, path, body):
    listing = v2_pod(status=status, runtime=None if status == "EXITED" else v2_pod()["runtime"])
    t = FakeTransport().queue(200, CATALOG).queue(*v2_listing(listing)).queue(204 if method == "DELETE" else 200,
                                                                          b"" if method == "DELETE" else listing)
    assert run_with(v2(t), [action, "--pod", "abc123", "-y"]) == 0
    assert (t.last.method, t.last.url, t.last.body) == (method, V2_BASE + path, body)


def test_start_translates_the_payload_and_prints_the_id(capsys):
    config = Config()
    config.pod.template_id = "tpl1"
    config.pod.gpu_type = GPU_ID
    config.pod.allowed_cuda_versions = ["13.0"]
    t = FakeTransport().queue(201, v2_pod(id="newpod", status="PROVISIONING", runtime=None))
    assert run_with(v2(t), ["start", "--volume-gb", "100", "--env", "A=b"], config=config) == 0
    body = t.last.body
    assert body["templateId"] == "tpl1" and body["cloud"] == "SECURE"
    assert body["gpu"] == {"id": GPU_ID, "count": 1, "minCudaVersion": "13.0"}
    assert body["mounts"] == {"persistent": {"size": 100, "path": "/workspace"}}
    assert body["env"] == {"A": "b"}
    assert "supportPublicIp" not in body and "gpuTypeIds" not in body
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "pod_id=newpod"


def test_start_capacity_refusal_is_retryable_like_v1(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", lambda s: None)
    config = Config()
    config.pod.template_id = "tpl1"
    no_capacity = (400, {"title": "Bad Request", "status": 400,
                         "detail": "There are no instances currently available"})
    t = FakeTransport().queue(*no_capacity).queue(201, v2_pod(id="newpod"))
    assert run_with(v2(t), ["start", "--retry", "1"], config=config) == 0
    assert "no instances currently available" in capsys.readouterr().err


def test_start_capacity_refusal_without_retry_exits_1(capsys):
    config = Config()
    config.pod.template_id = "tpl1"
    t = FakeTransport().queue(400, {"title": "Bad Request", "detail": "There are no instances currently available"})
    assert run_with(v2(t), ["start"], config=config) == 1
    assert "no instances currently available" in capsys.readouterr().err


def test_cloud_all_tries_secure_then_community():
    bodies = create_bodies({"name": "x", "gpuTypeIds": [GPU_ID], "cloudType": "ALL"})
    assert [b["cloud"] for b in bodies] == ["SECURE", "COMMUNITY"]
    t = FakeTransport().queue(400, {"detail": "no instances available"}).queue(201, v2_pod(id="c1"))
    assert v2(t).create_pod({"name": "x", "gpuTypeIds": [GPU_ID], "cloudType": "ALL"})["id"] == "c1"
    assert [c.body["cloud"] for c in t.calls] == ["SECURE", "COMMUNITY"]


def test_network_volume_and_start_command_translate():
    body = create_bodies({"name": "x", "gpuTypeIds": [GPU_ID], "networkVolumeId": "vol1", "volumeInGb": 50,
                          "dockerStartCmd": ["bash", "-c", "sleep 1"], "imageName": "img", "containerDiskInGb": 80})[0]
    assert body["mounts"] == {"network": [{"volumeId": "vol1", "path": "/workspace"}]}
    assert body["cmd"] == ["bash", "-c", "sleep 1"] and body["image"] == "img" and body["disk"] == 80


# -- translation details ---------------------------------------------------------------


@pytest.mark.parametrize("v2_status,legacy", [("PROVISIONING", "CREATED"), ("STARTING", "CREATED"),
                                              ("RUNNING", "RUNNING"), ("EXITED", "EXITED"), ("ERROR", "ERROR")])
def test_status_mapping(v2_status, legacy):
    assert legacy_pod(v2_pod(status=v2_status), {})["desiredStatus"] == legacy


def test_unknown_gpu_id_falls_back_to_the_id():
    assert legacy_pod(v2_pod(), {})["machine"]["gpuDisplayName"] == GPU_ID


def test_listing_pages_through_cursors():
    t = (FakeTransport().queue(200, CATALOG)
         .queue(200, {"pods": [v2_pod(id="a")], "pagination": {"nextCursor": "c2", "hasNextPage": True}})
         .queue(200, {"pods": [v2_pod(id="b")], "pagination": {"nextCursor": None, "hasNextPage": False}}))
    assert [p["id"] for p in v2(t).list_pods()] == ["a", "b"]
    assert t.calls[2].url.endswith("cursor=c2")


def test_catalog_is_cached_between_listings(tmp_path):
    catalog = PodCache(tmp_path / "gpus.json", ttl=86400)
    t = FakeTransport().queue(200, CATALOG).queue(*v2_listing(v2_pod())).queue(*v2_listing(v2_pod()))
    client = v2(t, catalog)
    client.list_pods()
    client.list_pods()
    assert sum("/catalog/gpus" in c.url for c in t.calls) == 1


def test_unreadable_catalog_does_not_fail_the_listing():
    t = FakeTransport().queue(500, {"title": "boom"}).queue(*v2_listing(v2_pod()))
    assert v2(t).list_pods()[0]["machine"]["gpuDisplayName"] == GPU_ID


def test_problem_details_become_the_error_message():
    t = FakeTransport().queue(404, {"type": "about:blank", "title": "Not Found", "status": 404,
                                    "detail": "pod zzz not found"})
    with pytest.raises(RunpodError, match="Not Found: pod zzz not found") as exc:
        v2(t).terminate_pod("zzz")
    assert exc.value.status == 404


def test_template_translation_keeps_placeholders():
    assert legacy_template(V2_TEMPLATE)["env"] == {"HF_TOKEN": "{{ RUNPOD_SECRET_HF_TOKEN_WRITE }}"}
    assert legacy_template(V2_TEMPLATE)["ports"] == "22/tcp,8888/http"


def test_rpt_uses_v2_by_default(monkeypatch, tmp_path):
    seen = []

    def fake_v2(env=None, transport=None):
        seen.append("v2")
        return v2(FakeTransport().queue(200, CATALOG).queue(*v2_listing()))

    monkeypatch.setattr("runpod_tools.api_v2.client_v2_from_env", fake_v2)
    assert main(["pods"], config_loader=Config, stdin_isatty=lambda: False, env={"RUNPOD_API_KEY": "k"}) == 0
    assert seen == ["v2"]


def test_rpt_api_v1_goes_back_to_graphql(monkeypatch):
    seen = []

    def fake_v1(env=None, transport=None):
        seen.append("v1")
        return v1(FakeTransport().queue(*graphql_ok({"myself": {"pods": []}})))

    monkeypatch.setattr("runpod_tools.cli.client_from_env", fake_v1)
    from runpod_tools import cli

    assert main(["pods"], client_factory=cli.client_from_env, config_loader=Config, stdin_isatty=lambda: False,
                env={"RUNPOD_API_KEY": "k", "RPT_API": "v1"}) == 0
    assert seen == ["v1"]


def test_v2_caches_are_separate_from_v1(tmp_path):
    from runpod_tools.api import client_from_env

    env = {"RUNPOD_API_KEY": "k", "RPT_CACHE_DIR": str(tmp_path)}
    assert client_v2_from_env(env).pod_cache.path != client_from_env(env).pod_cache.path


def test_other_ports_are_public_unless_the_ip_is_private():
    p = v2_pod(runtime={"uptime": 1, "ports": [
        {"private": 22, "public": 40022, "type": "tcp", "ip": "1.2.3.4"},
        {"private": 8000, "public": 40100, "type": "tcp", "ip": "1.2.3.4"},
        {"private": 8888, "public": None, "type": "http", "ip": "10.0.0.5"},
    ]})
    ports = {e["privatePort"]: e for e in legacy_pod(p, {})["runtime"]["ports"]}
    assert ports[8000]["isIpPublic"] is True and ports[8000]["publicPort"] == 40100
    assert ports[8888]["isIpPublic"] is False
    assert ports[22] == {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 22, "publicPort": 40022, "type": "tcp"}


def test_selected_client_follows_rpt_api(tmp_path):
    from runpod_tools.api_v2 import selected_client_from_env

    env = {"RUNPOD_API_KEY": "k", "RPT_CACHE_DIR": str(tmp_path)}
    assert isinstance(selected_client_from_env(env), RestV2Client)
    assert not isinstance(selected_client_from_env({**env, "RPT_API": "v1"}), RestV2Client)


def test_cuda_floor_is_the_lowest_allowed_version():
    body = create_bodies({"name": "x", "gpuTypeIds": [GPU_ID], "allowedCudaVersions": ["12.10", "12.8", "13.0"]})[0]
    assert body["gpu"]["minCudaVersion"] == "12.8"
