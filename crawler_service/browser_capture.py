"""Portable Bilibili capture schema, completeness checks and offline exports."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def video_url(value: str) -> str:
    parsed = urlsplit(value)
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+)/?", parsed.path)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in
            {"www.bilibili.com", "bilibili.com", "m.bilibili.com"} or
            parsed.username or parsed.password or parsed.port not in (None, 80, 443) or not match):
        raise ValueError("请输入 B站 /video/BV… 视频链接；短链接请先在浏览器展开。")
    return f"https://www.bilibili.com/video/{match[1]}/"


def ident(value) -> str:
    return "0" if value in (None, "", 0, "0") else str(value)


def integer(value) -> int:
    if isinstance(value, bool) or value is None or not str(value).isdigit():
        raise ValueError("评论数量或分页数字无效。")
    return int(value)


def normalize(raw: dict) -> dict:
    cid = ident(raw.get("rpid_str") or raw.get("rpid"))
    if not cid.isdigit() or cid == "0":
        raise ValueError("评论缺少有效 rpid。")
    content = raw.get("content") or {}
    return {
        "rpid": cid, "root": ident(raw.get("root_str") or raw.get("root")),
        "parent": ident(raw.get("parent_str") or raw.get("parent")),
        "username": (raw.get("member") or {}).get("uname", ""),
        "message": content.get("message", ""), "ctime": raw.get("ctime"),
        "like": raw.get("like", 0), "rcount": raw.get("rcount"),
        "pictures": content.get("pictures") or [],
    }


def new_capture(url: str) -> dict:
    return {"schema_version": 1, "video_url": video_url(url), "title": "",
            "started_at": now(), "finished_at": None, "displayed_totals": [],
            "main_runs": [], "threads": {}, "comments": {}, "reply_runs": {},
            "errors": [], "status": "running"}


def load_capture(payload: dict) -> dict:
    """Accept this collector's snapshots and the historical MCP snapshot schema."""
    if not isinstance(payload, dict):
        raise ValueError("抓取文件必须是 JSON 对象。")
    state = new_capture(payload.get("video_url", ""))
    for key in state:
        if key in payload:
            state[key] = payload[key]
    state["video_url"] = video_url(state["video_url"])
    for name in ("threads", "comments", "reply_runs"):
        if not isinstance(state[name], dict):
            raise ValueError(f"{name} 必须是以评论 ID 为键的对象，请选择 snapshot.json。")
    for name in ("main_runs", "displayed_totals", "errors"):
        if not isinstance(state[name], list):
            raise ValueError(f"{name} 格式无效。")
    for cid, c in state["comments"].items():
        if not isinstance(c, dict) or str(c.get("rpid")) != cid or not cid.isdigit() or cid == "0":
            raise ValueError("评论 ID 与字典键不一致。")
        if not isinstance(c.get("message", ""), str) or not isinstance(c.get("pictures", []), list):
            raise ValueError("评论正文或图片格式无效。")
        for field in ("root", "parent"):
            if not ident(c.get(field)).isdigit():
                raise ValueError("评论回复关系 ID 无效。")
    for cid, c in state["threads"].items():
        if not isinstance(c, dict) or cid not in state["comments"] or str(c.get("rpid")) != cid or ident(c.get("root")) != "0":
            raise ValueError("主评论列表与评论数据不一致。")
    derived = {cid for cid, c in state["comments"].items() if ident(c.get("root")) == "0"}
    if derived != set(state["threads"]):
        raise ValueError("主评论列表缺失或包含非主评论。")
    return state


def put(state: dict, raw: dict, main: bool = False):
    c = normalize(raw)
    state["comments"][c["rpid"]] = c
    if main and c["root"] == "0":
        state["threads"][c["rpid"]] = c
    for sub in raw.get("replies") or []:
        put(state, sub)


def pinned(value):
    if isinstance(value, dict):
        if value.get("rpid") or value.get("rpid_str"):
            yield value
        else:
            for child in value.values():
                yield from pinned(child)
    elif isinstance(value, list):
        for child in value:
            yield from pinned(child)


def ingest_main(state: dict, data: dict):
    cursor = data.get("cursor") or {}
    if type(cursor.get("is_end")) is not bool:
        raise ValueError("主评论缺少明确的分页结束状态。")
    rows = data.get("replies") or []
    if not isinstance(rows, list):
        raise ValueError("主评论列表格式无效。")
    for raw in [*rows, *pinned(data.get("top")), *pinned(data.get("top_replies")), *pinned((data.get("upper") or {}).get("top"))]:
        put(state, raw, main=True)
    state["main_runs"].append({"at": now(), "mode": cursor.get("mode"),
        "is_begin": cursor.get("is_begin"), "is_end": cursor["is_end"],
        "count": len(rows), "ids": [normalize(r)["rpid"] for r in rows],
        "next_offset": (cursor.get("pagination_reply") or {}).get("next_offset"),
        "total": cursor.get("all_count")})
    if cursor.get("all_count") is not None:
        state["displayed_totals"].append(integer(cursor["all_count"]))


def audit(state: dict) -> dict:
    counts = Counter(ident(c.get("root")) for c in state["comments"].values())
    orphan = sorted(root for root in counts if root != "0" and root not in state["threads"])
    mismatches = []
    for root, c in state["threads"].items():
        run = state["reply_runs"].get(root) or {}
        try:
            total, expected, actual = integer(run.get("total")), integer(c.get("rcount")), counts[root]
            pages = run.get("pages") or []
            if total:
                nums = [integer(p.get("pn")) for p in pages]
                size = integer(pages[-1].get("size", 10))
                pages_ok = size > 0 and nums == list(range(1, len(nums)+1)) and len(nums)*size >= total
            else:
                pages_ok = True
            valid = (run.get("complete") is True and not run.get("error") and pages_ok and
                     total == expected == actual == integer(run.get("count")))
        except (ValueError, TypeError, AttributeError, IndexError):
            valid = False
        if not valid:
            mismatches.append({"root": root, "actual": counts[root], "declared": c.get("rcount"),
                               "page_total": run.get("total")})
    latest = [r for r in state["main_runs"] if r.get("mode") == 2]
    hot = [r for r in state["main_runs"] if r.get("mode") == 3]
    latest_end = bool(latest and latest[0].get("is_begin") is True and latest[-1].get("is_end") is True)
    total = len(state["comments"])
    displayed = state["displayed_totals"][-1] if state["displayed_totals"] else None
    return {"source": "local-browser", "total": total, "roots": len(state["threads"]),
            "replies": total-counts["0"], "picture_comments": sum(bool(c.get("pictures")) for c in state["comments"].values()),
            "latest_end": latest_end, "hot_end": bool(hot and hot[-1].get("is_end") is True),
            "main_pages": {"latest": len(latest), "hot": len(hot)},
            "displayed_total": displayed, "displayed_gap": displayed-total if displayed is not None else None,
            "orphan_roots": orphan, "reply_mismatches": mismatches, "errors": state["errors"],
            "accessible_scope_verified": latest_end and not orphan and not mismatches and not state["errors"],
            "scope": "抓取时段内该浏览器账号可访问、接口实际返回的评论；非冻结快照。"}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def export_capture(state: dict, directory: Path) -> dict:
    report = audit(state)
    directory = Path(directory)
    write_json(directory / "snapshot.json", state)
    write_json(directory / "report.json", report)
    with (directory / "comments.jsonl").open("w", encoding="utf-8") as stream:
        for c in state["comments"].values():
            stream.write(json.dumps(c, ensure_ascii=False)+"\n")
    fields = ["rpid", "root", "parent", "username", "message", "ctime", "like", "rcount", "pictures"]
    with (directory / "comments.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for c in state["comments"].values():
            row = {k: c.get(k, "") for k in fields}
            row["pictures"] = json.dumps(row["pictures"], ensure_ascii=False)
            for key in ("rpid", "root", "parent", "username", "message"):
                value = str(row[key])
                row[key] = "'"+value if key in {"rpid", "root", "parent"} or value.lstrip().startswith(("=", "+", "-", "@")) else value
            writer.writerow(row)
    return report
