"""kytdl: thin command line for the local workbench (127.0.0.1 only). Shares kytdl_client with the MCP server.
Read-only or local-only commands: nothing here spends points, publishes, adopts, rejects or changes 科研通 state.
Exit codes: 0 ok, 1 business error, 2 service not running, 3 permission denied."""
import argparse
import base64
import json
import sys
from pathlib import Path

from kytdl_client import ApiError, Client, ServiceDown, WAIT_MAX_SECONDS, wait_until

OK, BUSINESS, DOWN, DENIED = 0, 1, 2, 3
BINARY_IMPORTS = (".docx", ".doc")


def clip(text, size):
    text = str(text or "")
    return text if len(text) <= size else text[:size - 1] + "…"


def render(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                print("%s%s:" % (pad, key))
                render(item, indent + 1)
            else:
                print("%s%s: %s" % (pad, key, "" if item is None else item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print("%s- %s" % (pad, "  ".join("%s=%s" % (k, clip(v, 60)) for k, v in item.items() if v not in (None, "", [], {}))))
            else:
                print("%s- %s" % (pad, item))
    else:
        print(pad + str(value))


def read_source(source):
    """(kind, payload): ('text', str) or ('file', (name, bytes))."""
    if source == "-":
        return "text", sys.stdin.read()
    path = Path(source)
    if not path.is_file():
        raise ValueError("找不到文件：" + source)
    binary = path.read_bytes()
    if path.suffix.lower() in BINARY_IMPORTS:
        return "file", (path.name, binary)
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return "text", binary.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文本编码，请使用 UTF-8")


def cmd_status(client, args):
    st = client.state()
    m = st.get("monitor") or {}
    return dict(running=True, version=(st.get("about") or {}).get("version"), papers=len(st["items"]), busy=bool(st.get("busy") or st.get("waiting")),
                active_account_id=st.get("active_account_id"), mcp_website_actions=bool((st.get("preferences") or {}).get("mcp_website_actions")),
                monitor=dict(enabled=m.get("enabled"), next_sync_at=m.get("next_sync_at"), last_sync_at=m.get("last_sync_at"), login_required=m.get("login_required")),
                batches=[dict(id=b["id"], phase=b["phase"], total=b["total"]) for b in st.get("batches", [])[:5]], panel_url=client.panel_url()), OK


def cmd_import(client, args):
    kind, payload = read_source(args.source)
    if kind == "file":
        name, binary = payload
        parsed = client.post("/api/parse-file", {"filename": name, "base64": base64.b64encode(binary).decode()})
        reply = client.post("/api/import", {"records": parsed["records"]}) if parsed["records"] else {}
    else:
        reply = client.post("/api/import", {"text": payload}) if payload.strip() else {}
    if not reply.get("batch_id"):
        return dict(ok=False, error="没有可导入的新文献（空内容、无法识别或全部重复）"), BUSINESS
    return dict(ok=True, batch_id=reply["batch_id"], count=reply["count"], skipped_duplicates=reply["skipped_duplicates"],
                next="kytdl wait %s --until awaiting_confirmation,done" % reply["batch_id"]), OK


def cmd_search(client, args):
    reply = client.post("/api/literature-search", dict(query=args.query, cursor=args.cursor, year_from=args.year_from,
                         year_to=args.year_to, open_access=args.open_access, page_size=args.page_size, mode=args.mode))
    return dict(ok=True, **reply), OK


def cmd_oa(client, args):
    if args.download:
        reply = client.post("/api/mcp/oa-fetch", {"doi": args.doi})
        if not reply.get("started"):
            return dict(ok=False, error="未开始：" + reply.get("reason", ""), id=reply.get("id")), BUSINESS
        return dict(ok=True, started=True, id=reply["id"], next="kytdl wait %s --until oa_finished; kytdl result %s" % (reply["id"], reply["id"])), OK
    reply = client.post("/api/mcp/oa-lookup", {"doi": args.doi}, timeout=130)
    found = reply["status"] == "found"
    return dict(reply, ok=found), OK if found else BUSINESS


def cmd_batch(client, args):
    st = client.state()
    if not args.id:
        return dict(batches=[dict(id=b["id"], created=b["created"], phase=b["phase"], total=b["total"], counts=b["counts"]) for b in st.get("batches", [])]), OK
    batch = next((b for b in st.get("batches", []) if b["id"] == args.id), None)
    if not batch:
        return dict(ok=False, error="未找到批次 " + args.id), BUSINESS
    rows = [dict(id=r["id"], title=clip(r["title"], 70), doi=r["doi"], code=r["code"], advice=r["advice"], checked=r["checked"], reward=r.get("reward"),
                 version=r["version"], retracted=r["retracted"]) for r in batch["items"]]
    return dict(id=batch["id"], phase=batch["phase"], counts=batch["counts"], steps=[dict(label=s["label"], status=s["status"]) for s in batch["steps"]], rows=rows), OK


def cmd_events(client, args):
    rows = client.state().get("events", [])[-max(1, min(args.n, 150)):]
    return dict(events=[dict(time=e["time"], message=e["message"]) for e in rows]), OK


def cmd_monitor(client, args):
    if args.mode in ("on", "off"):
        client.post("/api/monitor", {"enabled": args.mode == "on"})
    m = client.state().get("monitor") or {}
    return {k: m.get(k) for k in ("enabled", "label", "login_required", "pending", "next_sync_at", "last_sync_at", "interval_seconds", "reason", "failures")}, OK


def cmd_wait(client, args):
    result = wait_until(client, args.id, args.until, min(args.timeout, WAIT_MAX_SECONDS))
    if not result["ok"]:
        return dict(result, error="未找到 " + args.id), BUSINESS
    return result, OK if result["reached"] else BUSINESS


def cmd_result(client, args):
    reply = client.post("/api/mcp/result", {"id": args.id})
    return dict(reply, ok=reply["has_pdf"]), OK if reply["has_pdf"] else BUSINESS


def cmd_open(client, args):
    if args.id and not any(i["id"] == args.id for i in client.state()["items"]):
        return dict(ok=False, error="未找到条目 " + args.id), BUSINESS
    reply = client.post("/api/open-panel", {"id": args.id})
    return dict(ok=True, opened=True, panel_url=reply.get("url") or client.panel_url(args.id)), OK


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    p = argparse.ArgumentParser(prog="kytdl", description="Local 科研通 literature helper CLI. Never spends points or changes 科研通 state.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common]).set_defaults(run=cmd_status)
    s = sub.add_parser("import", parents=[common], help="import references (file or - for stdin); starts free-source screening only")
    s.add_argument("source")
    s.set_defaults(run=cmd_import)
    s = sub.add_parser("search", parents=[common], help="search OpenAlex; no download or import")
    s.add_argument("query")
    s.add_argument("--cursor", default="*")
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)
    s.add_argument("--open-access", action="store_true")
    s.add_argument("--page-size", type=int, default=50)
    s.add_argument("--mode", choices=["keyword", "exact", "semantic"], default="keyword")
    s.set_defaults(run=cmd_search)
    s = sub.add_parser("oa", parents=[common], help="look up (or with --download fetch) the open-access PDF of a DOI")
    s.add_argument("doi")
    s.add_argument("--download", action="store_true")
    s.set_defaults(run=cmd_oa)
    s = sub.add_parser("batch", parents=[common])
    s.add_argument("id", nargs="?", default="")
    s.set_defaults(run=cmd_batch)
    s = sub.add_parser("events", parents=[common])
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(run=cmd_events)
    s = sub.add_parser("monitor", parents=[common])
    s.add_argument("mode", nargs="?", choices=["on", "off", "status"], default="status")
    s.set_defaults(run=cmd_monitor)
    s = sub.add_parser("wait", parents=[common], help="long poll until a batch/paper reaches a state")
    s.add_argument("id")
    s.add_argument("--until", required=True, help="comma list, e.g. awaiting_confirmation,done or downloaded")
    s.add_argument("--timeout", type=int, default=60, help="seconds, max 600")
    s.set_defaults(run=cmd_wait)
    s = sub.add_parser("open", parents=[common], help="open the workbench window (optionally on one paper) to watch progress")
    s.add_argument("id", nargs="?", default="")
    s.set_defaults(run=cmd_open)
    s = sub.add_parser("result", parents=[common])
    s.add_argument("id")
    s.set_defaults(run=cmd_result)
    return p


def main(argv=None, client=None):
    args = parser().parse_args(argv)
    try:
        data, code = args.run(client or Client(), args)
    except ServiceDown as exc:
        data, code = dict(ok=False, error=str(exc), reason="service_not_running"), DOWN
    except ApiError as exc:
        data, code = dict(ok=False, error=str(exc), reason=exc.reason or "error"), DENIED if exc.permission else BUSINESS
    except (ValueError, KeyError, OSError) as exc:
        data, code = dict(ok=False, error=str(exc) or type(exc).__name__), BUSINESS
    if args.json:
        data.setdefault("ok", code == OK)
        print(json.dumps(dict(data, exit=code), ensure_ascii=False))
    elif code != OK and data.get("error"):
        print(data["error"], file=sys.stderr)
        extra = {k: v for k, v in data.items() if k not in ("error", "ok")}
        if extra:
            render(extra)
    else:
        render({k: v for k, v in data.items() if k != "ok"})
    return code


if __name__ == "__main__":
    sys.exit(main())
