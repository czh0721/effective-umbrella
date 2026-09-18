"""全链路回归：真实路由 + 假模型，覆盖 注册→创建→素材→蒸馏→绑定→对话→计费→
分段→长期记忆→危机→主动消息→时间线→告别/激活→念念币/套餐/后台。

独立脚本（不进 unittest discover），避免平台模型环境变量污染其他测试。

运行：python3 scripts/e2e/run_chain.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fake_llm  # noqa: E402

PORT = 8791
TMP = tempfile.mkdtemp(prefix="e2e-")
os.environ["PERSONA_DATA_DIR"] = TMP
os.environ["PERSONA_SECRET_KEY"] = "e2e-secret-key"
os.environ["WECLAW_BIN"] = ""
os.environ["PERSONA_REGISTER_MAX"] = "1000"
os.environ["PERSONA_PLATFORM_API_KEY"] = "fake-key"
os.environ["PERSONA_PLATFORM_BASE_URL"] = f"http://127.0.0.1:{PORT}/v1"
os.environ["PERSONA_PLATFORM_MODEL"] = "fake-model"
os.environ["PERSONA_PLATFORM_DAILY_LIMIT"] = "0"

fake_llm.start(PORT)

from fastapi.testclient import TestClient  # noqa: E402

from ex_persona import crypto, store, workspace  # noqa: E402
from ex_persona import webapp as webapp_module  # noqa: E402

store.init_db()
client = TestClient(webapp_module.app)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def chat(token, message, contact="c1@im.wechat"):
    resp = client.post(
        f"/v1/chat/completions/{token}",
        json={"messages": [{"role": "user", "content": message}], "user": contact},
    )
    if resp.status_code != 200:
        return None, resp.status_code, resp.text
    return resp.json()["choices"][0]["message"]["content"], 200, ""


TRANSCRIPT = """2023-05-01 21:03:00 小鹿
今晚一起吃饭吗
2023-05-01 21:04:12 我
不吃啦 减肥呢
2023-05-01 21:05:00 小鹿
那随你
2023-05-01 21:06:00 小鹿
我今天有点累
2023-05-01 21:07:00 我
那你早点休息
2023-05-01 21:08:00 小鹿
嗯 你也是
"""


def main():
    # 1. 注册
    reg = client.post("/api/auth/register", json={"username": f"e2e-{os.urandom(3).hex()}", "password": "password123"})
    check("注册 200", reg.status_code == 200, reg.text[:120])
    user_id = reg.json()["user"]["id"]

    credits = client.get("/api/credits").json()
    check("注册赠 100 积分", credits["balance"] == 100, f"balance={credits['balance']}")
    check("平台模型就绪", credits["platform_ready"] is True)
    check("每轮扣 1 积分", credits["per_turn_cost"] == 1)

    # 2. 创建人格
    created = client.post("/api/personas", json={"name": "小鹿", "gender": "女"})
    check("创建人格 200", created.status_code == 200, created.text[:120])
    pid = created.json()["persona"]["id"]

    # 2b. 蒸馏券：无券 402 -> 购买 -> 重蒸馏预扣
    no_ticket = client.post(f"/api/personas/{pid}/redistill", json={"use_llm": True})
    check("无券重蒸馏 402", no_ticket.status_code == 402, no_ticket.text[:120])
    store.grant_coins(user_id, 200, reason="e2e 买券准备")
    coins_before_ticket = client.get("/api/credits").json()["coins"]
    buy_ticket = client.post("/api/distill-tickets/purchase", json={"quantity": 2})
    check("购买蒸馏券 200", buy_ticket.status_code == 200, buy_ticket.text[:140])
    after_ticket = client.get("/api/credits").json()
    check("购券扣 120 念念币", coins_before_ticket - after_ticket["coins"] == 120,
          f"{coins_before_ticket}->{after_ticket['coins']}")
    check("蒸馏券到账 2 张", after_ticket["distill_tickets"]["balance"] == 2,
          str(after_ticket.get("distill_tickets")))

    # 3. 素材 + 解析
    paste = client.post("/api/paste", json={"text": TRANSCRIPT})
    check("粘贴素材", paste.status_code == 200, paste.text[:100])
    ingest = client.post("/api/ingest", json={"persona_id": pid})
    check("解析素材", ingest.status_code == 200 and ingest.json()["total"] >= 4, ingest.text[:140])
    check("解析命中目标 小鹿", ingest.status_code == 200 and ingest.json().get("target") == "小鹿", ingest.text[:140])

    # 4. 同步蒸馏
    dis = client.post("/api/distill", json={"persona_id": pid, "use_llm": True})
    check("蒸馏 200", dis.status_code == 200, dis.text[:200])
    persona = store.get_persona(user_id, pid)
    check("蒸馏后状态 ready", persona and persona["status"] == "ready", str(persona and persona["status"]))
    card_path = Path(persona["dir"]) / "persona_card.json"
    card = json.loads(card_path.read_text(encoding="utf-8")) if card_path.exists() else {}
    check("画像写入 summary", bool(card.get("summary")), str(card.get("summary"))[:60])
    check("画像含共同回忆", len(card.get("memories") or []) >= 1, f"{len(card.get('memories') or [])} 条")

    # 5. 异步重蒸馏 + 任务轮询
    tickets_before_re = client.get("/api/credits").json()["distill_tickets"]["balance"]
    re_task = client.post(f"/api/personas/{pid}/redistill", json={"use_llm": True}).json()["task_id"]
    status = "running"
    for _ in range(60):
        task = client.get(f"/api/tasks/{re_task}").json()
        status = task.get("status")
        if status in ("success", "error", "done"):
            break
        time.sleep(0.3)
    check("异步蒸馏任务完成", status in ("success", "done"), f"status={status}")
    tickets_after_re = client.get("/api/credits").json()["distill_tickets"]["balance"]
    check("重蒸馏扣 1 张蒸馏券", tickets_before_re - tickets_after_re == 1,
          f"{tickets_before_re}->{tickets_after_re}")

    # 6. 绑定桥接令牌
    token = crypto.new_bridge_token(user_id)
    store.upsert_wechat_binding(
        user_id, bridge_token=token, home_dir=str(workspace.home_dir(user_id)),
        persona_id=pid, phase="logged-in",
    )
    store.activate_persona(user_id, pid)

    # 7. 正常对话 + 计费
    before = client.get("/api/credits").json()["balance"]
    reply, code, text = chat(token, "在干嘛呀")
    check("对话 200", code == 200, text[:120])
    check("回复非空", bool(reply and reply.strip()), repr((reply or "")[:50]))
    after = client.get("/api/credits").json()["balance"]
    check("成功回复扣 1 积分", before - after == 1, f"{before}->{after}")
    turns = store.list_turns(user_id, pid, limit=10, contact="c1@im.wechat")
    check("轮次已入库(成对)", len(turns) >= 2, f"{len(turns)} turns")

    # 8. 长回复分段入队
    reply2, code2, _ = chat(token, "今天过得怎么样呢")
    check("长回复分段：首段返回", code2 == 200 and reply2 and len(reply2) < 300, f"len={len(reply2 or '')}")
    stats = store.outbox_stats()
    check("后续分段已入队", stats["counts"].get("pending", 0) + stats["counts"].get("sent", 0) >= 1, str(stats["counts"]))

    # 9. 空回复退款
    before_empty = client.get("/api/credits").json()["balance"]
    empty_reply, _, _ = chat(token, "触发空回复测试")
    after_empty = client.get("/api/credits").json()["balance"]
    check("空回复内容为空", not (empty_reply or "").strip(), repr(empty_reply))
    check("空回复退回积分", before_empty - after_empty == 0, f"{before_empty}->{after_empty}")

    # 10. 乱码丢弃 + 退款
    before_garble = client.get("/api/credits").json()["balance"]
    garble_reply, _, _ = chat(token, "触发乱码测试")
    after_garble = client.get("/api/credits").json()["balance"]
    check("乱码回复被丢弃", not (garble_reply or "").strip(), repr((garble_reply or "")[:40]))
    check("乱码回复退回积分", before_garble - after_garble == 0, f"{before_garble}->{after_garble}")

    # 11. 长期记忆抽取
    for i in range(6):
        chat(token, f"我们聊聊第{i}件事吧")
    memories = []
    for _ in range(40):
        memories = client.get(f"/api/personas/{pid}/memories", params={"contact": "c1@im.wechat"}).json().get("memories") or []
        if memories:
            break
        time.sleep(0.3)
    check("长期记忆已抽取", len(memories) >= 1, str([m.get("content") for m in memories])[:90])

    # 12. 情感危机兜底
    crisis_reply, _, _ = chat(token, "我最近压力好大 有点不想活了")
    check("危机命中确定性求助", "热线" in (crisis_reply or ""), repr((crisis_reply or "")[:60]))

    # 13. 主动消息
    proactive = client.post(f"/api/personas/{pid}/proactive/run")
    check("主动消息生成 200", proactive.status_code == 200, proactive.text[:140])
    check("主动消息有内容", bool(proactive.json().get("text")) if proactive.status_code == 200 else False)

    # 13b. 朋友圈：发布 -> 列表 -> 点赞 -> 评论 -> 回应 -> 删除
    persona_settings = client.get(f"/api/personas/{pid}").json().get("settings") or {}
    moments_settings = persona_settings.get("moments") or {}
    moments_settings["enabled"] = True
    saved = client.put(f"/api/personas/{pid}/settings", json={"settings": {"moments": moments_settings}})
    check("开启朋友圈 200", saved.status_code == 200, saved.text[:120])

    before_moment = client.get("/api/credits").json()["balance"]
    published = client.post(f"/api/personas/{pid}/moments")
    check("发布动态 200", published.status_code == 200, published.text[:160])
    moment = published.json().get("moment", {}) if published.status_code == 200 else {}
    check("动态正文非空", bool((moment.get("content") or "").strip()), repr((moment.get("content") or "")[:40]))
    check("发布扣 1 积分", before_moment - client.get("/api/credits").json()["balance"] == 1)

    listed = client.get(f"/api/personas/{pid}/moments").json()
    check("列表含新动态", any(item["id"] == moment.get("id") for item in listed["moments"]), f"n={len(listed['moments'])}")

    like = client.post(f"/api/moments/{moment['id']}/like")
    check("点赞 200 且计数 1", like.status_code == 200 and like.json()["like_count"] == 1, like.text[:120])

    comment = client.post(f"/api/moments/{moment['id']}/comments", json={"content": "在干嘛"})
    check(
        "评论并生成回应",
        comment.status_code == 200 and bool((comment.json()["comment"].get("reply") or "").strip()),
        comment.text[:160],
    )

    removed = client.delete(f"/api/moments/{moment['id']}")
    check("删除动态 200", removed.status_code == 200, removed.text[:120])
    check("删除后列表为空", client.get(f"/api/personas/{pid}/moments").json()["moments"] == [])

    # 14. 时间线
    timeline = client.get(f"/api/personas/{pid}/timeline", params={"contact": "c1@im.wechat"}).json()
    check("时间线有对话", timeline["total"] >= 4, f"total={timeline['total']}")
    check("时间线含联系人", len(timeline["contacts"]) >= 1, str(timeline["contacts"])[:80])

    # 15. 告别 -> 停回；激活 -> 恢复
    bye = client.post(f"/api/personas/{pid}/farewell", json={})
    check("告别 200", bye.status_code == 200, bye.text[:140])
    check("告别后状态 retired", store.get_persona(user_id, pid)["status"] == "retired")
    retired_reply, _, _ = chat(token, "你还在吗")
    check("retired 人格不再回复", not (retired_reply or "").strip(), repr((retired_reply or "")[:40]))
    act = client.post(f"/api/personas/{pid}/activate")
    check("重新激活 200", act.status_code == 200, act.text[:120])
    check("激活后恢复 ready", store.get_persona(user_id, pid)["status"] == "ready")

    # 16. 后台：兑换码 / 套餐 / 双余额
    from ex_persona import accounts

    admin_name = "e2e-admin"
    if store.get_admin_by_username(admin_name) is None:
        accounts.create_admin(admin_name, "password123")
    admin_login = client.post(
        "/api/admin/auth/login", json={"username": admin_name, "password": "password123"}
    )
    check("管理员登录 200", admin_login.status_code == 200, admin_login.text[:120])
    codes = client.post("/api/admin/redemption-codes", json={"count": 1, "coins": 60}).json()["items"]
    check("生成兑换码", len(codes) == 1, str(codes)[:80])
    coins_before_redeem = client.get("/api/credits").json()["coins"]
    redeem = client.post("/api/redeem", json={"code": codes[0]["code"] if isinstance(codes[0], dict) else codes[0]})
    check("兑换念念币 200", redeem.status_code == 200, redeem.text[:140])
    check("念念币到账 60",
          redeem.status_code == 200 and redeem.json()["coins"] == coins_before_redeem + 60,
          f"{coins_before_redeem}->{redeem.json().get('coins')}")

    pkg = client.post("/api/admin/packages", json={
        "name": "体验包", "coins": 30, "credits": 300, "active": True,
        "validity_days": 30, "bonus_credits": 20, "bonus_tickets": 1,
    })
    check("后台创建套餐 200", pkg.status_code == 200, pkg.text[:140])
    if pkg.status_code == 200:
        created = pkg.json()["package"]
        check("套餐返回赠送字段",
              created.get("bonus_credits") == 20 and created.get("bonus_tickets") == 1,
              str({k: created.get(k) for k in ("bonus_credits", "bonus_tickets")}))
        check("套餐返回档位标签", created.get("validity_label") == "月度", str(created.get("validity_label")))
    packages = client.get("/api/credits").json()["packages"]
    target = next((p for p in packages if p.get("name") == "体验包"), None)
    check("套餐出现在列表", target is not None and target.get("bonus_credits") == 20, str(packages)[:120])
    if target:
        tickets_before = client.get("/api/credits").json().get("distill_tickets", {}).get("balance", 0)
        before_buy = client.get("/api/credits").json()
        buy = client.post(f"/api/packages/{target['id']}/purchase")
        after_buy = client.get("/api/credits").json()
        check("购买套餐 200", buy.status_code == 200, buy.text[:140])
        check("扣念念币发积分", after_buy["coins"] == before_buy["coins"] - 30 and after_buy["balance"] == before_buy["balance"] + 320,
              f"coins {before_buy['coins']}->{after_buy['coins']}, credits {before_buy['balance']}->{after_buy['balance']}")
        check("套餐赠送蒸馏券",
              after_buy.get("distill_tickets", {}).get("balance", 0) == tickets_before + 1,
              f"{tickets_before}->{after_buy.get('distill_tickets', {}).get('balance')}")

    prof = client.post("/api/profile", json={"nickname": "e2e小念", "avatar": "data:image/png;base64,iVBORw0KGgo="})
    check("更新资料 200", prof.status_code == 200, prof.text[:140])
    me_prof = client.get("/api/me").json()["user"]
    check("昵称与头像已保存",
          me_prof.get("nickname") == "e2e小念" and (me_prof.get("avatar") or "").startswith("data:image/"),
          str({k: me_prof.get(k) for k in ("nickname", "avatar")}))
    check("返回注册时间", bool(me_prof.get("created_at")), str(me_prof.get("created_at")))
    bad_prof = client.post("/api/profile", json={"nickname": "念" * 21})
    check("超长昵称被拒", bad_prof.status_code == 400, bad_prof.text[:120])

    grant_c = client.post(f"/api/admin/users/{user_id}/credits", json={"delta": 50, "reason": "e2e"})
    check("后台加积分 200", grant_c.status_code == 200, grant_c.text[:120])
    grant_exp = client.post(f"/api/admin/users/{user_id}/credits", json={"delta": 300, "credit_days": 30, "reason": "e2e 到期批次"})
    check("后台带有效期发积分 200", grant_exp.status_code == 200, grant_exp.text[:120])
    exp_credits = client.get("/api/credits").json()
    check("返回积分批次", len(exp_credits.get("batches") or []) >= 1, str(exp_credits.get("batches"))[:120])
    check("返回最近到期时间", bool(exp_credits.get("next_expiry")), str(exp_credits.get("next_expiry")))
    check("返回蒸馏券流水字段", "distill_ticket_ledger" in exp_credits, str(list(exp_credits.keys()))[:120])
    grant_coin = client.post(f"/api/admin/users/{user_id}/coins", json={"delta": 10, "reason": "e2e"})
    check("后台加念念币 200", grant_coin.status_code == 200, grant_coin.text[:120])

    # 17. 通知中心
    notes = client.get("/api/notifications").json()
    check("通知中心有记录", len(notes["items"]) >= 1, f"unread={notes['unread']}")

    # 18. 导出
    export = client.get("/api/export", params={"persona_id": pid})
    check("导出 zip 200", export.status_code == 200 and export.headers.get("content-type", "").startswith("application/zip"),
          f"status={export.status_code} bytes={len(export.content)}")

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n===== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed =====")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
