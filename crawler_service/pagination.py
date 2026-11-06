"""Exhaust explicit platform cursors, including every reply stream, without a count cap."""
from __future__ import annotations

import asyncio

from crawler_service.store import Store


def number(value) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


def normalize(platform: str, item: dict, root: str | None = None) -> dict | None:
    if platform == "dy":
        cid, content, user = item.get("cid"), item.get("text"), item.get("user") or {}
        nickname, likes, created = user.get("nickname"), item.get("digg_count"), item.get("create_time")
        parent = item.get("reply_id")
    elif platform == "bili":
        cid, content, user = item.get("rpid"), (item.get("content") or {}).get("message"), item.get("member") or {}
        nickname, likes, created = user.get("uname"), item.get("like"), item.get("ctime")
        parent = item.get("parent")
    elif platform == "xhs":
        cid, content, user = item.get("id"), item.get("content"), item.get("user_info") or {}
        nickname, likes, created = user.get("nickname"), item.get("like_count"), item.get("create_time")
        parent = (item.get("target_comment") or {}).get("id")
    else:
        cid, content = item.get("comment_id") or item.get("commentId"), item.get("content")
        nickname = item.get("author_name") or item.get("authorName")
        likes, created = item.get("likedCount") or item.get("likeCount"), item.get("timestamp")
        parent = item.get("replyToCommentId") or item.get("reply_to_comment_id")
    if cid is None or str(cid) in {"", "0"}:
        return None
    parent = str(parent) if parent not in (None, "", 0, "0") else root
    return {"comment_id": str(cid), "parent_comment_id": parent if root else None, "root_comment_id": root, "content": str(content or ""), "author_nickname": str(nickname or "") or None, "like_count": number(likes), "create_time": created}


class Pager:
    def __init__(self, store: Store, job_id: str, platform: str, interval: float = 2):
        self.store, self.job_id, self.platform, self.interval = store, job_id, platform, interval
        self.issues: list[str] = []
        self.started = False
        self.exhausted = False

    async def call(self, method, *args, **kwargs):
        for attempt in range(3):
            try:
                result = await method(*args, **kwargs)
                if not isinstance(result, dict):
                    raise ValueError("平台返回了无效的评论数据，未判定为抓取完成。")
                await asyncio.sleep(self.interval)
                return result
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))

    def save(self, items: list, root: str | None = None):
        rows = []
        for item in items:
            row = normalize(self.platform, item, root) if isinstance(item, dict) else None
            if not row:
                raise ValueError("平台返回的评论缺少有效 ID，未判定为抓取完成。")
            rows.append(row)
        self.store.save(self.job_id, rows)

    async def run(self, client, video_id: str, token: str = ""):
        self.started = True
        root_state = self.store.checkpoint(self.job_id, "roots")
        if root_state["done"]:
            self.exhausted = True
            return []
        cursor = root_state["cursor"]
        if cursor is None:
            cursor = 0 if self.platform in {"dy", "bili"} else ""
        seen = set()
        while True:
            if str(cursor) in seen:
                raise ValueError("平台重复返回相同翻页位置，已停止；已保存的数据可以继续抓取。")
            seen.add(str(cursor))
            data, items, next_cursor, done = await self.root_page(client, video_id, token, cursor)
            self.save(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                row = normalize(self.platform, item)
                if not row:
                    continue
                try:
                    await self.replies(client, video_id, token, item, row["comment_id"])
                except Exception as exc:
                    self.issues.append(f"评论 {row['comment_id']} 的回复未取完（{type(exc).__name__}），可点击继续重试。")
            # Do not advance past a root page while any of its reply streams failed.
            if self.issues:
                return []
            if not done and (not items or next_cursor == cursor):
                raise ValueError("平台仍标记有下一页，但没有返回新评论或新游标。")
            self.store.save_checkpoint(self.job_id, "roots", next_cursor, done)
            if done:
                self.exhausted = True
                return []
            cursor = next_cursor

    async def root_page(self, client, video_id, token, cursor):
        if self.platform == "dy":
            data = await self.call(client.get_aweme_comments, video_id, cursor)
            required = ("comments", "has_more", "cursor")
            items, next_cursor, done = data.get("comments") or [], data.get("cursor"), not data.get("has_more")
        elif self.platform == "ks":
            data = await self.call(client.get_video_comments, video_id, cursor)
            required = ("rootCommentsV2", "pcursorV2")
            items, next_cursor = data.get("rootCommentsV2") or [], data.get("pcursorV2")
            done = next_cursor == "no_more"
        elif self.platform == "xhs":
            data = await self.call(client.get_note_comments, video_id, token, cursor)
            required = ("comments", "has_more", "cursor")
            items, next_cursor, done = data.get("comments") or [], data.get("cursor"), not data.get("has_more")
        else:
            from media_platform.bilibili.field import CommentOrderType
            data = await self.call(client.get_video_comments, video_id, order_mode=CommentOrderType.TIME, next=cursor)
            required = ("cursor",)
            info = data.get("cursor") or {}
            if info.get("is_end") not in (True, False) or "next" not in info:
                raise ValueError("B站未返回完整分页状态。")
            items = list(data.get("replies") or [])
            if cursor == 0:
                top = data.get("top") or {}
                def pinned(value):
                    if isinstance(value, dict):
                        return [value] if value.get("rpid") else [row for child in value.values() for row in pinned(child)]
                    if isinstance(value, list):
                        return [row for child in value for row in pinned(child)]
                    return []
                items.extend(pinned(top))
                items.extend(data.get("top_replies") or [])
            next_cursor, done = info["next"], bool(info["is_end"])
        if any(key not in data for key in required) or not isinstance(items, list):
            raise ValueError("平台未返回完整分页状态，可能需要重新登录或完成验证。")
        if self.platform in {"dy", "xhs"} and data["has_more"] not in (0, 1, True, False):
            raise ValueError("平台未返回有效的分页结束状态。")
        return data, items, next_cursor, done

    async def replies(self, client, video_id, token, item, root):
        state = self.store.checkpoint(self.job_id, root)
        if state["done"]:
            return
        count_key = {"dy": "reply_comment_total", "bili": "rcount", "xhs": "sub_comment_count"}.get(self.platform)
        if count_key and (count_key not in item or item[count_key] is None):
            raise ValueError("平台未提供该条评论的回复数量，不能判定回复已取完。")
        if self.platform == "ks" and not any(key in item for key in ("hasSubComments", "commentCount", "subCommentCount")):
            raise ValueError("快手未提供回复状态。")
        expected = number(item.get("reply_comment_total") if self.platform == "dy" else item.get("rcount") if self.platform == "bili" else item.get("sub_comment_count") if self.platform == "xhs" else item.get("commentCount") or item.get("subCommentCount"))
        preview = item.get("sub_comments") if self.platform == "xhs" else item.get("replies") if self.platform == "bili" else []
        if isinstance(preview, list):
            self.save(preview, root)
        if self.platform == "ks":
            needed = bool(item.get("hasSubComments")) or expected > 0
        elif self.platform == "xhs":
            needed = bool(item.get("sub_comment_has_more"))
            if not needed and expected > len(preview or []):
                raise ValueError("小红书回复数量大于返回数量，但未返回下一页。")
        else:
            needed = expected > 0
        if not needed:
            self.store.save_checkpoint(self.job_id, root, None, True)
            return
        cursor = state["cursor"]
        if cursor is None:
            cursor = 1 if self.platform == "bili" else 0 if self.platform == "dy" else item.get("sub_comment_cursor", "") if self.platform == "xhs" else ""
        seen = set()
        while True:
            if str(cursor) in seen:
                raise ValueError("子评论翻页游标重复。")
            seen.add(str(cursor))
            if self.platform == "dy":
                data = await self.call(client.get_sub_comments, video_id, root, cursor)
            elif self.platform == "ks":
                data = await self.call(client.get_video_sub_comments, video_id, int(root), cursor)
            elif self.platform == "xhs":
                data = await self.call(client.get_note_sub_comments, video_id, root, token, cursor=cursor)
            else:
                from media_platform.bilibili.field import CommentOrderType
                data = await self.call(client.get_video_level_two_comments, video_id, int(root), cursor, 20, CommentOrderType.DEFAULT)
            if self.platform == "bili":
                if not isinstance(data.get("page"), dict) or "count" not in data["page"]:
                    raise ValueError("B站未返回子评论分页数量。")
                items, next_cursor = data.get("replies") or [], cursor + 1
                done = number(data["page"]["count"]) <= cursor * 20
            elif self.platform == "ks":
                if "subCommentsV2" not in data or "pcursorV2" not in data:
                    raise ValueError("快手未返回子评论分页状态。")
                items, next_cursor = data["subCommentsV2"] or [], data["pcursorV2"]
                done = next_cursor == "no_more"
            else:
                if any(k not in data for k in ("comments", "has_more", "cursor")):
                    raise ValueError("平台未返回子评论分页状态。")
                if data["has_more"] not in (0, 1, True, False):
                    raise ValueError("平台未返回有效的子评论结束状态。")
                items, next_cursor, done = data["comments"] or [], data["cursor"], not data["has_more"]
            if not isinstance(items, list) or (not done and (not items or next_cursor == cursor)):
                raise ValueError("子评论分页停滞。")
            self.save(items, root)
            self.store.save_checkpoint(self.job_id, root, next_cursor, done)
            if done:
                return
            cursor = next_cursor
