"""Synthetic fixtures for previews and UI checks; never mixed with live data."""

import time


class DemoReader:
    def __init__(self):
        self.cancelled = False

    def reset_cancel(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def close(self):
        pass

    def start(self, progress=None):
        if progress:
            progress("正在加载演示联系人…")
        return {"version": "演示数据", "account": "demo", "contacts": [
            {"username": "demo_chen", "nick_name": "陈晓", "remark": "陈晓 · 产品设计", "alias": "chen_demo", "description": "下次沟通确认设计稿反馈。", "local_type": 1, "delete_flag": 0},
            {"username": "demo_lin", "nick_name": "林远", "remark": "林远 · 研发", "alias": "lin_demo", "description": "", "local_type": 1, "delete_flag": 0},
            {"username": "demo_team@chatroom", "nick_name": "周末徒步小队", "remark": "", "alias": "", "description": "", "local_type": 2, "delete_flag": 0},
            {"username": "demo_xu", "nick_name": "许然", "remark": "", "alias": "xu_demo", "description": "", "local_type": 1, "delete_flag": 0},
        ], "coverage": {"snapshot": True}}

    def messages(self, username, limit=100, keyword="", before=None, progress=None):
        if progress:
            progress("正在读取演示聊天…")
        now = int(time.time())
        texts = [("陈晓", "早上好，整理后的方案我已经看过了。"),
                 ("我", "收到。我们把讨论要点整理一下，下午再确认。"),
                 ("陈晓", "可以，主要看联系人分组和备注的使用流程。"),
                 ("我", "备注修改之后，我会再刷新通讯录核对一下。"),
                 ("陈晓", "好，按这个方案推进。")]
        rows = [{"local_id": n, "local_type": 1, "create_time": now-1800+n*180,
                 "sender_name": sender, "sender_username": "demo_chen", "text": body,
                 "source_shard": "demo", "decode_error": False}
                for n, (sender, body) in enumerate(texts) if keyword.casefold() in body.casefold()]
        return {"messages": rows, "total": len(rows), "scanned": len(texts), "has_more": False,
                "next_before": None, "coverage": {"complete": True, "demo": True}}
