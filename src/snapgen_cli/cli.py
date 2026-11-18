"""`snapgen` command line.

Models, options and request validation come from the snapgen_mcp package (the SnapGen MCP server), so the CLI
accepts exactly what the MCP tools accept and is checked locally before any credit is spent.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from snapgen_mcp import registry
from snapgen_mcp.auth import CREATE_KEY_URL
from snapgen_mcp.client import SnapGenAuthError, SnapGenClient, SnapGenError

from . import __version__, config, jobs, media

EXIT_ERROR, EXIT_USAGE, EXIT_JOB_FAILED, EXIT_TIMEOUT, EXIT_INTERRUPTED = 1, 2, 3, 4, 130
NOT_SIGNED_IN = f"Not signed in. Run `snapgen auth login` or set SNAPGEN_API_KEY (create a key at {CREATE_KEY_URL})."
KEY_REFUSED = "Your API key was refused (deleted, or wrong). Run `snapgen auth login` with a valid key."


class UsageError(SnapGenError):
    pass


class CliAuthError(SnapGenAuthError):
    """An auth problem the CLI words itself (any other SnapGenAuthError means uapi refused the key)."""


# ---------------------------------------------------------------- output
class Out:
    def __init__(self, as_json: bool, color: bool):
        self.json = as_json
        self.color = color and not as_json

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def green(self, t: str) -> str:
        return self._c("32", t)

    def red(self, t: str) -> str:
        return self._c("31", t)

    def emit(self, data: Any) -> None:
        print(json.dumps(data, indent=2, ensure_ascii=False))

    def info(self, text: str) -> None:
        """Progress chatter. Goes to stderr so stdout stays clean for pipes."""
        if not self.json:
            print(self.dim(text), file=sys.stderr)

    def fields(self, pairs: list[tuple[str, Any]]) -> None:
        pairs = [(k, v) for k, v in pairs if v not in (None, "", [])]
        width = max((len(k) for k, _ in pairs), default=0)
        for k, v in pairs:
            print(f"{self.dim(k.ljust(width))}  {v}")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
        term = shutil.get_terminal_size((120, 20)).columns
        # The last column (usually a summary) gets whatever room is left.
        widths[-1] = max(20, min(widths[-1], term - sum(widths[:-1]) - 2 * (len(widths) - 1)))

        def line(cells: list[str]) -> str:
            cells = [c if len(c) <= w else c[: w - 3] + "..." for c, w in zip(cells, widths)]
            return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

        print(self.bold(line(headers)))
        for r in rows:
            print(line(r))


def _enable_windows_ansi() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - colors are cosmetic
        return False


def _out(args: argparse.Namespace) -> Out:
    color = (sys.stdout.isatty() and not getattr(args, "no_color", False) and "NO_COLOR" not in os.environ
             and _enable_windows_ansi())
    return Out(getattr(args, "json", False), color)


def _cli_message(msg: str) -> str:
    """Error text from snapgen_mcp names MCP tools; point at the matching CLI commands instead."""
    msg = msg.replace("Call list_models to see valid ids.", "Run `snapgen model list` to see valid ids.")
    msg = re.sub(r"Call describe_model\('([^']+)'\) for details\.", r"Run `snapgen model get \1` for details.", msg)
    for tool, cmd in (("generate_image", "snapgen generate create"), ("generate_video", "snapgen generate create"),
                      ("extend_video", "snapgen generate extend"), ("generate_storyboard", "snapgen generate storyboard")):
        msg = msg.replace(f"Use {tool} instead.", f"Use `{cmd}` instead.")
    return msg


# ---------------------------------------------------------------- helpers
def _base_url(args: argparse.Namespace) -> str | None:
    """--base-url, else the config file, else SNAPGEN_BASE_URL. The API key travels in a header, so plain http is
    refused unless the host is this machine (a local dev server)."""
    url = (getattr(args, "base_url", None) or config.load().get("base_url")
           or os.environ.get("SNAPGEN_BASE_URL") or None)
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise UsageError(f"Invalid base URL {url!r}: use https://host.")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise UsageError(f"Refusing to send your API key over plain http to {parsed.hostname}. Use https.")
    return url


def _client(args: argparse.Namespace) -> SnapGenClient:
    key, _ = config.resolve_api_key()
    if not key:
        raise CliAuthError(NOT_SIGNED_IN)
    return SnapGenClient(api_key=key, base_url=_base_url(args))


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (use e.g. 90s, 10m, 1h)")
    value, unit = float(m.group(1)), m.group(2) or "s"
    return value * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def parse_model_options(tokens: list[str], spec: registry.ModelSpec) -> dict[str, Any]:
    """Turn leftover `--name value` / `--name=value` / bare `--flag` tokens into model options.

    `--aspect-ratio` and `--aspect_ratio` are the same option. Values are checked by registry.build_request.
    """
    options: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--") or tok == "--":
            raise UsageError(f"Unexpected argument {tok!r}. Model options look like --name value.")
        name, eq, value = tok[2:].partition("=")
        name = name.replace("-", "_")
        if not eq:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                value = tokens[i + 1]
                i += 1
            else:
                param = spec.params.get(name)
                if param is None or param.type != "bool":
                    allowed = ", ".join("--" + p for p in spec.params) or "none"
                    hint = "needs a value" if param else f"is not an option of {spec.id} (options: {allowed})"
                    raise UsageError(f"--{name} {hint}.")
                value = "true"
        if name in options:
            raise UsageError(f"--{name} given twice.")
        options[name] = value
        i += 1
    unknown = [n for n in options if n not in spec.params]
    if unknown:
        allowed = ", ".join("--" + p for p in spec.params) or "none"
        raise UsageError(f"{spec.id} has no option {', '.join('--' + n for n in unknown)}. Options: {allowed}. "
                         f"See `snapgen model get {spec.id}`.")
    return options


def _print_job(out: Out, job: dict) -> None:
    if out.json:
        out.emit(job)
        return
    status = job["status"]
    colored = out.green(status) if status == "completed" else out.red(status) if status == "failed" else status
    out.fields([("uuid", job.get("uuid")), ("status", colored), ("model", job.get("model")),
                ("progress", f"{job['progress_percent']}%" if job.get("progress_percent") else None),
                ("credits", job.get("credits_used")), ("error", job.get("error")),
                ("expires", job.get("expires_at"))])
    for url in jobs.result_urls(job):
        print(url)


async def _finish(args: argparse.Namespace, out: Out, client: SnapGenClient, sub: dict) -> int:
    """After a submit: print the job, or wait for it (and download) when asked."""
    want_wait = args.wait or args.download is not None
    if not want_wait:
        if out.json:
            out.emit(sub)
        else:
            out.fields([("uuid", sub["uuid"]), ("status", sub["status"]), ("model", sub.get("model")),
                        ("estimated", f"{sub.get('estimated_credit')} credits")])
            out.info(f"Track it with: snapgen generate wait {sub['uuid']}")
        return 0
    out.info(f"Submitted {sub['uuid']} ({sub.get('estimated_credit')} credits estimated). Waiting...")
    return await _wait_and_report(args, out, client, sub["uuid"])


async def _wait_and_report(args: argparse.Namespace, out: Out, client: SnapGenClient, uuid: str) -> int:
    try:
        job = await jobs.wait(client, uuid, args.wait_timeout, args.wait_interval,
                              show_progress=not out.json and sys.stderr.isatty())
    except KeyboardInterrupt:
        out.info(f"Stopped waiting. The job keeps running: snapgen generate wait {uuid}")
        raise
    if args.download is not None:
        job["files"] = [str(p) for p in await jobs.download(job, Path(args.download))]
    _print_job(out, job)
    if not out.json:
        for f in job.get("files") or []:
            out.info(f"saved {f}")
    return 0


# ---------------------------------------------------------------- commands
async def cmd_auth_login(args: argparse.Namespace, out: Out) -> int:
    if args.with_token or not sys.stdin.isatty():
        key = sys.stdin.readline().strip()
    else:
        print(f"Paste a SnapGen API key (create one at {CREATE_KEY_URL}).", file=sys.stderr)
        key = getpass.getpass("API key: ").strip()
    if not key:
        raise UsageError("No API key given.")
    base_url = _base_url(args)
    try:
        acct = await SnapGenClient(api_key=key, base_url=base_url).account()
    except SnapGenAuthError:
        raise CliAuthError(f"That API key was refused. Check it at {CREATE_KEY_URL}") from None
    data = config.load()
    data["api_key"] = key
    path = config.save(data)
    if out.json:
        out.emit({"signed_in": True, "name": acct.get("full_name"), "config": str(path)})
    else:
        print(f"Signed in as {out.bold(acct.get('full_name') or 'unknown')}. Key saved to {path}")
    if os.environ.get("SNAPGEN_API_KEY"):
        out.info("Note: SNAPGEN_API_KEY is set in your environment and takes precedence over the saved key.")
    return 0


async def cmd_auth_logout(args: argparse.Namespace, out: Out) -> int:
    data = config.load()
    removed = data.pop("api_key", None) is not None
    if removed:
        config.save(data)
    if out.json:
        out.emit({"signed_out": removed})
    else:
        print("Signed out." if removed else "No saved key.")
        if os.environ.get("SNAPGEN_API_KEY"):
            out.info("SNAPGEN_API_KEY is still set in your environment.")
    return 0


async def cmd_auth_status(args: argparse.Namespace, out: Out) -> int:
    key, source = config.resolve_api_key()
    if not key:
        raise CliAuthError(NOT_SIGNED_IN)
    acct = await _client(args).account()
    where = "SNAPGEN_API_KEY" if source == "env" else str(config.config_path())
    if out.json:
        out.emit({"signed_in": True, "name": acct.get("full_name"), "key": config.mask(key), "source": where})
    else:
        out.fields([("account", acct.get("full_name")), ("key", config.mask(key)), ("source", where)])
    return 0


async def cmd_account(args: argparse.Namespace, out: Out) -> int:
    acct = await _client(args).account()
    credit = acct.get("user_credit") or {}
    data = {"name": acct.get("full_name"), "plan_id": acct.get("plan_id"),
            "available_credit": credit.get("available_credit"), "locked_credit": credit.get("locked_credit")}
    if out.json:
        out.emit(data)
    else:
        out.fields([("account", data["name"]), ("plan", data["plan_id"]),
                    ("credits", out.bold(str(data["available_credit"]))), ("locked", data["locked_credit"])])
    return 0


async def cmd_model_list(args: argparse.Namespace, out: Out) -> int:
    models = [m for m in registry.all_models()
              if (args.kind is None or m.kind == args.kind) and (args.family is None or m.family == args.family)]
    if out.json:
        out.emit([{"id": m.id, "family": m.family, "kind": m.kind, "premium": m.premium, "summary": m.summary}
                  for m in models])
    else:
        out.table(["MODEL", "KIND", "FAMILY", "PREMIUM", "SUMMARY"],
                  [[m.id, m.kind, m.family, "yes" if m.premium else "", m.summary] for m in models])
    return 0


async def cmd_model_get(args: argparse.Namespace, out: Out) -> int:
    spec = registry.get_model(args.model)
    d = spec.describe()
    if out.json:
        out.emit(d)
        return 0
    print(out.bold(spec.id) + out.dim(f"  ({spec.kind}, {spec.family})"))
    print(spec.summary)
    print()
    out.fields([("premium", "required" if spec.premium else "no"), ("pricing", d["pricing"]),
                ("prompt", f"at least {spec.min_prompt_len} characters" if spec.min_prompt_len > 1 else None)])
    if spec.params:
        print()
        rows = []
        for name, p in spec.params.items():
            values = (", ".join(map(str, p.choices)) if p.choices else
                      f"{p.min}..{p.max}" if p.min is not None or p.max is not None else p.type)
            rows.append([f"--{name}", values, "" if p.default is None else str(p.default), p.description])
        out.table(["OPTION", "VALUES", "DEFAULT", "NOTES"], rows)
    refs = [(flag, r) for flag, r in (("--image (URL / file)", spec.image_urls),
                                      ("--image / --history (uuid)", spec.history),
                                      ("--video", spec.video_urls), ("--audio", spec.audio_urls)) if r]
    if refs:
        print()
        out.table(["REFERENCE", "MIN", "MAX"], [[f, str(r.min), "any" if r.max is None else str(r.max)] for f, r in refs])
    if spec.max_total_refs:
        print(out.dim(f"At most {spec.max_total_refs} reference files in total."))
    if spec.notes:
        print()
        print(spec.notes)
    return 0


async def _submit(args: argparse.Namespace, out: Out, spec: registry.ModelSpec, prompt: str,
                  options: dict[str, Any], refs: media.Refs) -> int:
    # Validate first (local files as placeholders) so nothing is uploaded or charged for a bad request.
    registry.build_request(spec, prompt, options, **refs.as_kwargs())
    client = _client(args)
    if refs.files:
        await media.upload_placeholders(client, refs, on_upload=lambda p: out.info(f"Uploading {p.name}..."))
    endpoint, fields = registry.build_request(spec, prompt, options, **refs.as_kwargs())
    sub = jobs.submitted(await client.submit(endpoint, fields))
    return await _finish(args, out, client, sub)


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt == "-":
        return sys.stdin.read()
    if args.prompt and args.prompt.startswith("@") and Path(args.prompt[1:]).is_file():
        return Path(args.prompt[1:]).read_text(encoding="utf-8")
    return args.prompt or ""


async def cmd_generate_create(args: argparse.Namespace, out: Out) -> int:
    spec = registry.get_model(args.model)
    if spec.kind == "extend":
        raise UsageError(f"{spec.id} extends an existing video. Use `snapgen generate extend {spec.id} <video_uuid>`.")
    if spec.kind == "storyboard":
        raise UsageError(f"{spec.id} is a storyboard model. Use `snapgen generate storyboard`.")
    options = parse_model_options(args.model_options, spec)
    refs = media.collect(args.image, args.video, args.audio, args.history)
    return await _submit(args, out, spec, _prompt(args), options, refs)


async def cmd_generate_extend(args: argparse.Namespace, out: Out) -> int:
    spec = registry.get_model(args.model, "extend")
    options = parse_model_options(args.model_options, spec)
    return await _submit(args, out, spec, _prompt(args), options, media.Refs(history_uuids=[args.video_uuid]))


def _load_scenes(args: argparse.Namespace) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    if args.scenes_file:
        raw = sys.stdin.read() if args.scenes_file == "-" else Path(args.scenes_file).read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise UsageError("--scenes-file must hold a JSON list of {\"prompt\": ..., \"duration\": 6|10}.")
        scenes.extend(loaded)
    for seconds, prompt in args.scene or []:
        try:
            scenes.append({"prompt": prompt, "duration": int(seconds)})
        except ValueError:
            raise UsageError(f"--scene {seconds!r} {prompt!r}: the first value is the duration (6 or 10).") from None
    return scenes


async def cmd_generate_storyboard(args: argparse.Namespace, out: Out) -> int:
    scenes = _load_scenes(args)
    # Same rules as the MCP generate_storyboard tool.
    if not 2 <= len(scenes) <= 10:
        raise UsageError("A storyboard needs between 2 and 10 scenes.")
    for i, sc in enumerate(scenes, 1):
        if not str(sc.get("prompt", "")).strip():
            raise UsageError(f"Scene {i} needs a prompt.")
        if sc.get("duration") not in (6, 10):
            raise UsageError(f"Scene {i}: duration must be 6 or 10 seconds.")
    if sum(sc["duration"] for sc in scenes) > 45:
        raise UsageError("Total storyboard duration cannot exceed 45 seconds.")
    spec = registry.get_model("grok-storyboard", "storyboard")
    fields = {"scenes": json.dumps([{"prompt": s["prompt"], "duration": s["duration"]} for s in scenes]),
              "model": spec.send_model, "aspect_ratio": args.aspect_ratio, "resolution": args.resolution}
    client = _client(args)
    sub = jobs.submitted(await client.submit(spec.endpoint, fields))
    return await _finish(args, out, client, sub)


async def cmd_generate_get(args: argparse.Namespace, out: Out) -> int:
    job = jobs.summarize(await _client(args).get_history(args.uuid))
    _print_job(out, job)
    return EXIT_JOB_FAILED if job["status"] == "failed" else 0


async def cmd_generate_wait(args: argparse.Namespace, out: Out) -> int:
    return await _wait_and_report(args, out, _client(args), args.uuid)


async def cmd_generate_list(args: argparse.Namespace, out: Out) -> int:
    resp = await _client(args).list_histories(filter_by=args.type, page=args.page, items_per_page=args.per_page)
    items = [jobs.summarize(h) for h in resp.get("result") or []]
    if out.json:
        out.emit({"total": resp.get("total"), "page": args.page, "items": items})
        return 0
    out.table(["UUID", "TYPE", "MODEL", "STATUS", "CREDITS", "CREATED", "PROMPT"],
              [[j["uuid"] or "", j["type"] or "", j["model"] or "", j["status"], str(j["credits_used"] or ""),
                str(j["created_at"] or "")[:19], (j["prompt"] or "").replace("\n", " ")] for j in items])
    out.info(f"page {args.page} - {len(items)} of {resp.get('total')} total")
    return 0


async def cmd_upload(args: argparse.Namespace, out: Out) -> int:
    client = _client(args)
    results = []
    for f in args.files:
        path = Path(f).expanduser()
        if not path.is_file():
            raise UsageError(f"File not found: {path}")
        out.info(f"Uploading {path.name}...")
        r = await media.upload_file(client, path)
        results.append({"file": str(path), **{k: r.get(k) for k in ("url", "media_type", "content_type", "size",
                                                                     "expires_at")}})
    if out.json:
        out.emit(results)
    else:
        for r in results:
            print(r["url"])
    return 0


async def cmd_version(args: argparse.Namespace, out: Out) -> int:
    from importlib.metadata import PackageNotFoundError, version

    try:
        mcp_version = version("snapgen-mcp")
    except PackageNotFoundError:
        mcp_version = "unknown"
    data = {"version": __version__, "snapgen_mcp": mcp_version, "python": sys.version.split()[0],
            "models": len(registry.all_models())}
    if out.json:
        out.emit(data)
    else:
        print(f"snapgen {__version__} (snapgen-mcp {mcp_version}, python {data['python']}, {data['models']} models)")
    return 0


# ---------------------------------------------------------------- parser
EXAMPLES = """\
examples:
  snapgen auth login
  snapgen model list --kind video
  snapgen model get kling-video-4-0
  snapgen generate create nano-banana-pro --prompt "a quiet beach at sunrise" --aspect_ratio 16:9 --wait
  snapgen generate create veo-3.1-fast --prompt "drone shot over a valley" --image ./first.png --duration 8 --wait
  snapgen generate extend veo-extend <video_uuid> --prompt "the camera keeps rising" --wait
  snapgen generate list --json | jq -r '.items[] | select(.status=="completed") | .uuid'
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    # SUPPRESS so a flag given before the subcommand is not reset by the subcommand's own default.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="disable color output")
    common.add_argument("--base-url", default=argparse.SUPPRESS,
                        help="API base URL (default: SNAPGEN_BASE_URL or https://api.snapgen.ai)")

    waiting = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    waiting.add_argument("--wait-timeout", type=parse_duration, default=parse_duration("10m"), metavar="DURATION",
                         help="max time to wait, e.g. 90s, 10m, 1h (default 10m)")
    waiting.add_argument("--wait-interval", type=parse_duration, default=parse_duration("3s"), metavar="DURATION",
                         help="poll interval (default 3s)")
    waiting.add_argument("--download", "-o", metavar="DIR", help="save the results into DIR (implies --wait)")

    submit = argparse.ArgumentParser(add_help=False, allow_abbrev=False, parents=[waiting])
    submit.add_argument("--wait", action="store_true", help="block until the job finishes and print the result URLs")

    def sub(parent_subs, name: str, help_: str, parents=(), **kw) -> argparse.ArgumentParser:
        return parent_subs.add_parser(name, help=help_, description=help_, parents=[common, *parents],
                                      allow_abbrev=False, **kw)

    p = argparse.ArgumentParser(prog="snapgen", parents=[common], allow_abbrev=False,
                                description="Generate images and videos on snapgen.ai from the terminal.",
                                epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"snapgen {__version__}")
    top = p.add_subparsers(dest="command", metavar="<command>")

    auth = sub(top, "auth", "sign in with an API key, sign out, show the active key")
    auth_s = auth.add_subparsers(dest="sub", metavar="<subcommand>")
    a = sub(auth_s, "login", "save an API key (verified against your account first)")
    a.add_argument("--with-token", action="store_true", help="read the key from stdin instead of prompting")
    a.set_defaults(fn=cmd_auth_login)
    sub(auth_s, "logout", "forget the saved API key").set_defaults(fn=cmd_auth_logout)
    sub(auth_s, "status", "show which key is active and the account it belongs to").set_defaults(fn=cmd_auth_status)

    sub(top, "account", "plan and credit balance").set_defaults(fn=cmd_account)

    model = sub(top, "model", "list models and inspect their options")
    model_s = model.add_subparsers(dest="sub", metavar="<subcommand>")
    m = sub(model_s, "list", "list models")
    m.add_argument("--kind", choices=registry.KINDS)
    m.add_argument("--family", help="e.g. veo, kling, seedance, grok, nano-banana, gpt-image, minimax, flux, vela, muse")
    m.set_defaults(fn=cmd_model_list)
    m = sub(model_s, "get", "show a model's options, allowed values, reference limits and pricing")
    m.add_argument("model")
    m.set_defaults(fn=cmd_model_get)

    gen = sub(top, "generate", "create, extend, track and list generations")
    gen_s = gen.add_subparsers(dest="sub", metavar="<subcommand>")
    g = sub(gen_s, "create", "start an image or video generation (costs credits)", parents=[submit],
            epilog="Model options are passed as flags, e.g. --aspect_ratio 16:9 --duration 8 "
                   "(see `snapgen model get <model>`).")
    g.add_argument("model", help="model id, see `snapgen model list`")
    g.add_argument("--prompt", "-p", required=True, help="text prompt; '-' reads stdin, '@file.txt' reads a file")
    g.add_argument("--image", action="append", default=[], metavar="REF",
                   help="reference image: URL, local file (uploaded) or generation uuid; repeatable")
    g.add_argument("--video", action="append", default=[], metavar="REF", help="reference video: URL or local file")
    g.add_argument("--audio", action="append", default=[], metavar="REF", help="reference audio: URL or local file")
    g.add_argument("--history", action="append", default=[], metavar="UUID",
                   help="a previous generation to use as reference; repeatable")
    g.set_defaults(fn=cmd_generate_create, accepts_model_options=True)

    g = sub(gen_s, "extend", "continue a finished Veo, Grok or Seedance video from its last frame (costs credits)",
            parents=[submit])
    g.add_argument("model", choices=[s.id for s in registry.all_models() if s.kind == "extend"])
    g.add_argument("video_uuid")
    g.add_argument("--prompt", "-p", required=True, help="what happens next")
    g.set_defaults(fn=cmd_generate_extend, accepts_model_options=True)

    g = sub(gen_s, "storyboard", "multi-scene Grok video, 2-10 scenes merged into one (costs credits per scene)",
            parents=[submit])
    g.add_argument("--scene", nargs=2, action="append", metavar=("SECONDS", "PROMPT"),
                   help="one scene: duration (6 or 10) and prompt; repeat in order")
    g.add_argument("--scenes-file", metavar="FILE", help='JSON list of {"prompt", "duration"}; "-" reads stdin')
    g.add_argument("--aspect-ratio", "--aspect_ratio", dest="aspect_ratio", default="landscape",
                   choices=("landscape", "portrait", "square"))
    g.add_argument("--resolution", default="480p", choices=("480p", "720p"))
    g.set_defaults(fn=cmd_generate_storyboard)

    g = sub(gen_s, "get", "show a generation's status and result URLs")
    g.add_argument("uuid")
    g.set_defaults(fn=cmd_generate_get)
    g = sub(gen_s, "wait", "wait for a generation to finish", parents=[waiting])
    g.add_argument("uuid")
    g.set_defaults(fn=cmd_generate_wait)
    g = sub(gen_s, "list", "recent generations, newest first")
    g.add_argument("--type", default="all", choices=("all", "image", "video"))
    g.add_argument("--page", type=int, default=1)
    g.add_argument("--per-page", type=int, default=10)
    g.set_defaults(fn=cmd_generate_list)

    u = sub(top, "upload", "upload image / video / audio files and print their URLs (free)")
    u.add_argument("files", nargs="+", metavar="FILE")
    u.set_defaults(fn=cmd_upload)

    sub(top, "version", "print version info").set_defaults(fn=cmd_version)

    for parser in (p, auth, model, gen):
        parser.set_defaults(help_parser=parser)
    return p


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))


def run(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # e.g. a cp1252 Windows pipe
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if not hasattr(args, "fn"):
        (getattr(args, "help_parser", None) or parser).print_help()
        return EXIT_USAGE
    if extra and not getattr(args, "accepts_model_options", False):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.model_options = extra
    if not hasattr(args, "wait"):
        args.wait = False
    if not hasattr(args, "download"):
        args.download = None
    out = _out(args)
    try:
        return asyncio.run(args.fn(args, out))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - every failure becomes one readable line
        if isinstance(exc, SnapGenAuthError) and not isinstance(exc, CliAuthError):
            message = KEY_REFUSED
        elif isinstance(exc, (SnapGenError, json.JSONDecodeError, OSError)):
            message = _cli_message(str(exc))
        else:
            raise
        code = (EXIT_JOB_FAILED if isinstance(exc, jobs.JobFailed) else EXIT_TIMEOUT if isinstance(exc, jobs.WaitTimeout)
                else EXIT_USAGE if isinstance(exc, UsageError) else EXIT_ERROR)
        if out.json:
            print(json.dumps({"error": message, "exit_code": code}), file=sys.stderr)
        else:
            print(out.red("error: ") + message if out.color else "error: " + message, file=sys.stderr)
        return code
