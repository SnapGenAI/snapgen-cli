"""CLI tests against a mocked uapi (respx). No network, no credits."""
import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from snapgen_cli import cli, config

API = "https://api.snapgen.ai/uapi/v1"
UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SNAPGEN_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SNAPGEN_API_KEY", "test-key")
    monkeypatch.delenv("SNAPGEN_BASE_URL", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def form(request: httpx.Request) -> dict:
    return parse_qs(request.content.decode())


def run(capsys, *argv):
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def history(status=2, **extra):
    base = {"uuid": UUID, "type": "video", "model_name": "veo-3.1-fast", "input_text": "p", "status": status,
            "status_percentage": 100 if status == 2 else 40, "used_credit": 4,
            "generated_video": [{"video_url": "https://cdn.example/v.mp4"}] if status == 2 else []}
    return {**base, **extra}


# ---------------------------------------------------------------- submit
def test_create_sends_validated_fields_and_key(api, capsys):
    route = api.post(API + "/video-gen/veo").respond(json={"uuid": UUID, "model_name": "veo-3.1-fast", "estimated_credit": 4})
    code, out, _ = run(capsys, "generate", "create", "veo-3.1-fast", "--prompt", "a valley",
                       "--aspect-ratio", "9:16", "--duration=8", "--image", "https://x/a.png", "--json")
    assert code == 0
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "test-key"
    assert form(req) == {"prompt": ["a valley"], "model": ["veo-3.1-fast"], "aspect_ratio": ["9:16"],
                         "duration": ["8"], "ref_images": ["https://x/a.png"]}
    assert json.loads(out) == {"uuid": UUID, "status": "submitted", "model": "veo-3.1-fast", "estimated_credit": 4}


def test_bad_option_value_is_refused_before_any_request(api, capsys):
    code, _, err = run(capsys, "generate", "create", "veo-3.1-fast", "-p", "x", "--duration", "7")
    assert code == cli.EXIT_ERROR
    assert "duration" in err and "[4, 6, 8, 10]" in err
    assert not api.calls


def test_unknown_option_lists_the_allowed_ones(api, capsys):
    code, _, err = run(capsys, "generate", "create", "nano-banana-2", "-p", "x", "--fps", "30")
    assert code == cli.EXIT_USAGE
    assert "--fps" in err and "--aspect_ratio" in err
    assert not api.calls


def test_bool_flag_without_value(api, capsys):
    route = api.post(API + "/video-gen/grok").respond(json={"uuid": UUID})
    assert run(capsys, "generate", "create", "grok-3", "-p", "x", "--skip-audio")[0] == 0
    assert form(route.calls.last.request)["skip_audio"] == ["true"]


def test_uuid_image_becomes_history_reference(api, capsys):
    route = api.post(API + "/generate_image").respond(json={"uuid": UUID})
    assert run(capsys, "generate", "create", "nano-banana-2", "-p", "x", "--image", UUID)[0] == 0
    assert form(route.calls.last.request)["ref_history"] == [UUID]


def test_local_file_is_uploaded_then_referenced(api, capsys, tmp_path):
    img = tmp_path / "first.png"
    img.write_bytes(b"\x89PNG fake")
    upload = api.post(API + "/uploads").respond(json={"url": "https://cdn.example/up.png"})
    gen = api.post(API + "/video-gen/veo").respond(json={"uuid": UUID})
    assert run(capsys, "generate", "create", "veo-3.1-fast", "-p", "x", "--image", str(img))[0] == 0
    assert b'filename="first.png"' in upload.calls.last.request.content
    assert form(gen.calls.last.request)["ref_images"] == ["https://cdn.example/up.png"]


def test_invalid_request_uploads_nothing(api, capsys, tmp_path):
    img = tmp_path / "char.png"
    img.write_bytes(b"x")
    # Motion control needs a reference video: refused locally, so the image is never uploaded.
    code, _, err = run(capsys, "generate", "create", "kling-video-motion-4", "-p", "make it dance now",
                       "--image", str(img))
    assert code == cli.EXIT_ERROR and "reference videos" in err
    assert not api.calls


def test_missing_reference_file(api, capsys):
    code, _, err = run(capsys, "generate", "create", "veo-3.1-fast", "-p", "x", "--video", "./nope.mp4")
    assert code == cli.EXIT_ERROR and "not found" in err


def test_extend_model_via_create_points_to_extend(capsys):
    code, _, err = run(capsys, "generate", "create", "veo-extend", "-p", "x")
    assert code == cli.EXIT_USAGE and "snapgen generate extend" in err


def test_unknown_model_message_uses_cli_wording(capsys):
    code, _, err = run(capsys, "generate", "create", "nope", "-p", "x")
    assert code == cli.EXIT_ERROR and "snapgen model list" in err and "list_models" not in err


def test_extend_sends_video_uuid(api, capsys):
    route = api.post(API + "/video-extend/veo").respond(json={"uuid": UUID})
    assert run(capsys, "generate", "extend", "veo-extend", UUID, "-p", "keeps rising")[0] == 0
    assert form(route.calls.last.request)["ref_history"] == [UUID]


def test_storyboard(api, capsys):
    route = api.post(API + "/video-storyboard/grok").respond(json={"uuid": UUID})
    assert run(capsys, "generate", "storyboard", "--scene", "6", "one", "--scene", "10", "two",
               "--aspect-ratio", "portrait")[0] == 0
    f = form(route.calls.last.request)
    assert json.loads(f["scenes"][0]) == [{"prompt": "one", "duration": 6}, {"prompt": "two", "duration": 10}]
    assert f["model"] == ["grok-video"] and f["aspect_ratio"] == ["portrait"]


def test_storyboard_rules(api, capsys):
    assert run(capsys, "generate", "storyboard", "--scene", "6", "only one")[0] == cli.EXIT_USAGE
    assert run(capsys, "generate", "storyboard", "--scene", "7", "a", "--scene", "6", "b")[0] == cli.EXIT_USAGE
    assert not api.calls


# ---------------------------------------------------------------- wait / get / download
def test_wait_polls_until_done(api, capsys):
    api.post(API + "/video-gen/veo").respond(json={"uuid": UUID, "estimated_credit": 4})
    api.get(API + f"/history/{UUID}").mock(side_effect=[httpx.Response(200, json=history(1)),
                                                  httpx.Response(200, json=history(2))])
    code, out, _ = run(capsys, "generate", "create", "veo-3.1-fast", "-p", "x", "--wait", "--wait-interval", "0",
                       "--json")
    assert code == 0
    assert json.loads(out)["video_urls"] == ["https://cdn.example/v.mp4"]


def test_failed_job_exit_code(api, capsys):
    api.get(API + f"/history/{UUID}").respond(json=history(3, error_message="content policy"))
    code, _, err = run(capsys, "generate", "wait", UUID, "--wait-interval", "0")
    assert code == cli.EXIT_JOB_FAILED and "content policy" in err


def test_wait_timeout(api, capsys):
    api.get(API + f"/history/{UUID}").respond(json=history(1))
    code, _, err = run(capsys, "generate", "wait", UUID, "--wait-timeout", "0s", "--wait-interval", "1s")
    assert code == cli.EXIT_TIMEOUT and f"snapgen generate wait {UUID}" in err


def test_get_prints_urls(api, capsys):
    api.get(API + f"/history/{UUID}").respond(json=history(2))
    code, out, _ = run(capsys, "generate", "get", UUID)
    assert code == 0 and "https://cdn.example/v.mp4" in out


def test_download(api, capsys, tmp_path):
    api.get(API + f"/history/{UUID}").respond(json=history(2))
    api.get("https://cdn.example/v.mp4").respond(content=b"video-bytes", headers={"content-type": "video/mp4"})
    code, out, _ = run(capsys, "generate", "wait", UUID, "-o", str(tmp_path / "out"), "--json")
    assert code == 0
    saved = tmp_path / "out" / f"{UUID}.mp4"
    assert saved.read_bytes() == b"video-bytes"
    assert json.loads(out)["files"] == [str(saved)]


def test_list(api, capsys):
    route = api.get(API + "/histories").respond(json={"total": 1, "result": [history(2)]})
    code, out, _ = run(capsys, "generate", "list", "--type", "video", "--per-page", "5", "--json")
    assert code == 0
    assert route.calls.last.request.url.params["filter_by"] == "video"
    assert json.loads(out)["items"][0]["status"] == "completed"


# ---------------------------------------------------------------- auth / account
def test_login_verifies_and_saves(api, capsys, monkeypatch):
    monkeypatch.delenv("SNAPGEN_API_KEY")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("new-key\n"))
    route = api.get(API + "/account").respond(json={"full_name": "Ada"})
    code, out, _ = run(capsys, "auth", "login", "--with-token")
    assert code == 0 and "Ada" in out
    assert route.calls.last.request.headers["x-api-key"] == "new-key"
    assert config.resolve_api_key() == ("new-key", "config")
    assert run(capsys, "auth", "logout")[0] == 0
    assert config.resolve_api_key() == ("", "")


def test_login_refused_key_is_not_saved(api, capsys, monkeypatch):
    monkeypatch.delenv("SNAPGEN_API_KEY")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("bad\n"))
    api.get(API + "/account").respond(401, json={"detail": {"error_code": "API_KEY_NOT_FOUND"}})
    code, _, err = run(capsys, "auth", "login", "--with-token")
    assert code == cli.EXIT_ERROR and "refused" in err
    assert config.resolve_api_key() == ("", "")


def test_not_signed_in(capsys, monkeypatch):
    monkeypatch.delenv("SNAPGEN_API_KEY")
    code, _, err = run(capsys, "account")
    assert code == cli.EXIT_ERROR and "snapgen auth login" in err


def test_refused_key_message(api, capsys):
    api.get(API + "/account").respond(401, json={"detail": {"error_code": "API_KEY_NOT_FOUND"}})
    code, _, err = run(capsys, "account")
    assert code == cli.EXIT_ERROR and "snapgen auth login" in err and "x-api-key header" not in err


def test_account(api, capsys):
    api.get(API + "/account").respond(json={"full_name": "Ada", "plan_id": "pro",
                                      "user_credit": {"available_credit": 120, "locked_credit": 4}})
    code, out, _ = run(capsys, "account", "--json")
    assert code == 0 and json.loads(out)["available_credit"] == 120


# ---------------------------------------------------------------- misc
def test_model_list_and_get_need_no_key(capsys, monkeypatch):
    monkeypatch.delenv("SNAPGEN_API_KEY")
    code, out, _ = run(capsys, "model", "list", "--kind", "extend", "--json")
    assert code == 0 and {m["id"] for m in json.loads(out)} >= {"veo-extend", "grok-extend", "seedance-extend"}
    code, out, _ = run(capsys, "model", "get", "seedance-2-omni", "--json")
    assert code == 0 and "duration" in json.loads(out)["options"]


def test_stray_args_rejected_for_non_generate_commands(capsys):
    with pytest.raises(SystemExit):
        cli.run(["account", "--bogus"])


@pytest.mark.parametrize("text,seconds", [("90s", 90), ("10m", 600), ("1h", 3600), ("2", 2), ("500ms", 0.5)])
def test_parse_duration(text, seconds):
    assert cli.parse_duration(text) == seconds


# ---------------------------------------------------------------- hardening
def test_download_name_from_server_cannot_escape_folder(api, capsys, tmp_path):
    evil = "../../evil"
    api.get(API + f"/history/{UUID}").respond(json=history(2, uuid=evil, generated_video=[
        {"video_url": "https://cdn.example/v.sh%2F..%2Fx"}]))
    api.get(url__startswith="https://cdn.example/").respond(content=b"x")
    out_dir = tmp_path / "out"
    assert run(capsys, "generate", "wait", UUID, "-o", str(out_dir))[0] == 0
    files = list(out_dir.iterdir())
    assert [f.name for f in files] == ["______evil.bin"]
    assert not (tmp_path / "evil.bin").exists()


@pytest.mark.parametrize("url", ["http://api.example.com", "ftp://api.example.com", "api.example.com"])
def test_insecure_base_url_refused_before_sending_key(api, capsys, url):
    code, _, err = run(capsys, "account", "--base-url", url)
    assert code == cli.EXIT_USAGE
    assert not api.calls


def test_http_localhost_allowed(api, capsys):
    route = api.get("http://localhost:8080/uapi/v1/account").respond(json={"full_name": "Ada"})
    assert run(capsys, "account", "--base-url", "http://localhost:8080")[0] == 0
    assert route.called


def test_insecure_base_url_from_env_refused(api, capsys, monkeypatch):
    monkeypatch.setenv("SNAPGEN_BASE_URL", "http://evil.example")
    assert run(capsys, "account")[0] == cli.EXIT_USAGE
    assert not api.calls
