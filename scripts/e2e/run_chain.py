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
    reg = client.post("/api/auth/register", json={"username": f"e2e-{os.urandom(3).hex()}", "password": "Password123!"})
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

    # 2b. 蒸馏券已取消：蒸馏直接扣积分，券购买接口下线
    credits0 = client.get("/api/credits").json()
    check("蒸馏积分单价 100", credits0.get("distill_credit_cost") == 100,
          str(credits0.get("distill_credit_cost")))
    check("credits 不再返回蒸馏券余额字段", "distill_tickets" not in credits0,
          str(sorted(credits0.keys()))[:140])
    buy_ticket = client.post("/api/distill-tickets/purchase", json={"quantity": 2})
    check("购买蒸馏券已下线 410", buy_ticket.status_code == 410, buy_ticket.text[:140])
    store.grant_coins(user_id, 200, reason="e2e 念念币准备")
    store.grant_credits(user_id, 1000, reason="e2e 蒸馏积分准备", source="gift")

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
    distill_cost = client.get("/api/credits").json()["distill_credit_cost"]
    balance_before_re = client.get("/api/credits").json()["balance"]
    re_task = client.post(f"/api/personas/{pid}/redistill", json={"use_llm": True}).json()["task_id"]
    status = "running"
    for _ in range(60):
        task = client.get(f"/api/tasks/{re_task}").json()
        status = task.get("status")
        if status in ("success", "error", "done"):
            break
        time.sleep(0.3)
    check("异步蒸馏任务完成", status in ("success", "done"), f"status={status}")
    balance_after_re = client.get("/api/credits").json()["balance"]
    check("重蒸馏扣 100 积分", balance_before_re - balance_after_re == distill_cost,
          f"{balance_before_re}->{balance_after_re}")

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
        accounts.create_admin(admin_name, "Password123!")
    admin_login = client.post(
        "/api/admin/auth/login", json={"username": admin_name, "password": "Password123!"}
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
        "validity_days": 30, "bonus_credits": 20,
    })
    check("后台创建套餐 200", pkg.status_code == 200, pkg.text[:140])
    if pkg.status_code == 200:
        created = pkg.json()["package"]
        check("套餐返回赠送字段",
              created.get("bonus_credits") == 20 and created.get("bonus_tickets") == 0,
              str({k: created.get(k) for k in ("bonus_credits", "bonus_tickets")}))
        check("套餐返回档位标签", created.get("validity_label") == "月度", str(created.get("validity_label")))
    packages = client.get("/api/credits").json()["packages"]
    target = next((p for p in packages if p.get("name") == "体验包"), None)
    check("套餐出现在列表", target is not None and target.get("bonus_credits") == 20, str(packages)[:120])
    if target:
        before_buy = client.get("/api/credits").json()
        buy = client.post(f"/api/packages/{target['id']}/purchase")
        after_buy = client.get("/api/credits").json()
        check("购买套餐 200", buy.status_code == 200, buy.text[:140])
        check("扣念念币发积分", after_buy["coins"] == before_buy["coins"] - 30 and after_buy["balance"] == before_buy["balance"] + 320,
              f"coins {before_buy['coins']}->{after_buy['coins']}, credits {before_buy['balance']}->{after_buy['balance']}")
        check("套餐不再发放蒸馏券", "distill_tickets" not in after_buy,
              str(sorted(after_buy.keys()))[:140])

    tier_names = {
        "轻享月卡", "标准月卡", "尊享月卡",
        "轻享季卡", "标准季卡", "尊享季卡",
        "轻享年卡", "标准年卡", "尊享年卡",
    }
    board = client.get("/api/credits").json()["packages"]
    board_names = {p.get("name") for p in board}
    check("套餐板含 9 档新档位", tier_names <= board_names, str(sorted(board_names)))
    check("旧跨时长套餐已下架", "标准包" not in board_names and "尊享包" not in board_names,
          str(sorted(board_names)))
    tier_margins = []
    tier_margin_ok = True
    for item in board:
        if item.get("name") not in tier_names:
            continue
        revenue = float(item["coins"]) / 10
        cost = (float(item["credits"]) / 20) * 0.008
        margin = 1 - cost / revenue
        tier_margins.append(f"{item['name']}={margin:.3f}")
        if abs(margin - 0.6) > 0.02:
            tier_margin_ok = False
    check("新档位毛利约 60%", tier_margin_ok and len(tier_margins) == 9, ", ".join(tier_margins))

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

    # 16.5 后台：看板 / 订单 / 用户详情
    dash = client.get("/api/admin/dashboard?days=7")
    check("后台看板 200", dash.status_code == 200, dash.text[:120])
    if dash.status_code == 200:
        dash_data = dash.json()
        check("看板趋势 7 天", len(dash_data.get("trend") or []) == 7,
              str(len(dash_data.get("trend") or [])))
        check("看板含转化漏斗", "paying" in (dash_data.get("funnel") or {}),
              str(dash_data.get("funnel"))[:80])
    orders = client.get("/api/admin/orders")
    check("后台订单列表 200", orders.status_code == 200, orders.text[:120])
    if orders.status_code == 200:
        order_items = orders.json().get("items") or []
        check("订单含本次购买", any(o.get("user_id") == user_id for o in order_items),
              f"n={len(order_items)}")
        check("营收汇总非负", (orders.json().get("revenue") or {}).get("coins", -1) >= 0)
    detail = client.get(f"/api/admin/users/{user_id}")
    check("后台用户详情 200",
          detail.status_code == 200 and detail.json().get("user", {}).get("id") == user_id,
          detail.text[:120])
    note = client.post(f"/api/admin/users/{user_id}/note", json={"note": "e2e 备注", "tags": "e2e"})
    check("后台写用户备注 200", note.status_code == 200, note.text[:120])

    # 16b. 后台扩展 P1：内容 / 运营 / 系统 / 管理员
    content = client.get("/api/admin/content/personas?limit=200")
    check("内容人格列表 200", content.status_code == 200, content.text[:120])
    persona_stop = client.post(f"/api/admin/content/personas/{pid}/status", json={"status": "disabled"})
    check("后台停用人格 200", persona_stop.status_code == 200, persona_stop.text[:120])
    check("人格已停用", store.get_persona(user_id, pid)["status"] == "disabled")
    persona_restore = client.post(f"/api/admin/content/personas/{pid}/status", json={"status": "ready"})
    check("后台恢复人格 200", persona_restore.status_code == 200, persona_restore.text[:120])
    check("人格已恢复", store.get_persona(user_id, pid)["status"] == "ready")
    moments = client.get("/api/admin/content/moments?limit=200")
    check("内容朋友圈列表 200", moments.status_code == 200, moments.text[:120])
    announce = client.post("/api/admin/announcements", json={"title": "e2e 公告", "body": "正文"})
    check("发布公告 200", announce.status_code == 200, announce.text[:120])
    notice = client.post("/api/admin/notices", json={"message": "e2e 通知", "user_ids": [user_id]})
    check("发送站内通知 200", notice.status_code == 200 and notice.json().get("sent") == 1, notice.text[:120])
    seg_count = client.post("/api/admin/audience/count", json={"audience": "manual", "audience_value": str(user_id)})
    check("受众命中人数 200",
          seg_count.status_code == 200 and seg_count.json().get("count") == 1,
          seg_count.text[:120])
    tpl = client.post("/api/admin/notice-templates", json={"name": "e2e 模板", "body": "你好 {username}"})
    check("创建通知模板 200", tpl.status_code == 200, tpl.text[:120])
    tpl_id = tpl.json()["template"]["id"]
    tpl_del = client.delete(f"/api/admin/notice-templates/{tpl_id}")
    check("删除通知模板 200", tpl_del.status_code == 200, tpl_del.text[:120])
    targeted = client.post(
        "/api/admin/announcements",
        json={"title": "e2e 定向公告", "body": "定向", "audience": "manual",
              "audience_value": str(user_id), "pinned": True},
    )
    check("发布定向公告 200", targeted.status_code == 200, targeted.text[:120])
    ann_id = targeted.json()["announcement"]["id"]
    user_ann = client.get("/api/announcements")
    check("用户可见定向公告",
          any(i["id"] == ann_id for i in (user_ann.json().get("items") or [])),
          user_ann.text[:120])
    reach = client.get(f"/api/admin/announcements/{ann_id}/reach")
    check("公告触达统计 200",
          reach.status_code == 200 and reach.json().get("read", 0) >= 1,
          reach.text[:120])
    history = client.get("/api/admin/notices")
    check("通知历史 200",
          history.status_code == 200 and len(history.json().get("items") or []) >= 1,
          history.text[:120])
    flags = client.get("/api/admin/system/flags")
    check("功能开关列表 200",
          flags.status_code == 200 and "moments_auto" in (flags.json().get("flags") or {}),
          flags.text[:120])
    flag_set = client.put("/api/admin/system/flags/moments_auto", json={"value": True})
    check("切换功能开关 200", flag_set.status_code == 200, flag_set.text[:120])
    backups = client.get("/api/admin/system/backups")
    check("备份列表只读 200",
          backups.status_code == 200 and isinstance(backups.json().get("items"), list),
          backups.text[:120])
    system_health = client.get("/api/admin/system/health")
    check("系统健康 200",
          system_health.status_code == 200 and system_health.json().get("status") == "ok",
          system_health.text[:120])
    admins = client.get("/api/admin/admins")
    check("管理员列表 200", admins.status_code == 200, admins.text[:120])
    new_admin = client.post("/api/admin/admins", json={"username": "e2e-helper", "password": "Password123!"})
    check("创建管理员 200或已存在", new_admin.status_code in (200, 409), new_admin.text[:120])
    audit = client.get("/api/admin/audit")
    check("审计含新动作",
          any(str(i.get("action", "")).startswith(("persona.", "announcement.", "flag."))
              for i in (audit.json().get("items") or [])),
          audit.text[:120])

    users_csv = client.get("/api/admin/users/export")
    check("用户导出 CSV 200",
          users_csv.status_code == 200 and users_csv.content.startswith(b"\xef\xbb\xbf"),
          f"status={users_csv.status_code}")
    orders_csv = client.get("/api/admin/orders/export")
    check("订单导出 CSV 200",
          orders_csv.status_code == 200 and orders_csv.content.startswith(b"\xef\xbb\xbf"),
          f"status={orders_csv.status_code}")
    audit_filtered = client.get("/api/admin/audit", params={"action": "user.set_note"})
    check("审计按动作筛选",
          audit_filtered.status_code == 200
          and all("user.set_note" in str(i.get("action", ""))
                  for i in (audit_filtered.json().get("items") or [])),
          audit_filtered.text[:120])
    dash_forced = client.get("/api/admin/dashboard", params={"days": 7, "refresh": 1})
    check("看板强制刷新跳过缓存",
          dash_forced.status_code == 200 and not dash_forced.json().get("cached"),
          dash_forced.text[:120])

    # 账号安全：弱密码拒绝 + 登录失败锁定与管理员解锁
    weak = client.post("/api/auth/register", json={
        "username": f"e2e-weak-{os.urandom(3).hex()}", "password": "password123",
    })
    check("弱密码注册被拒绝", weak.status_code == 400, weak.text[:120])

    lock_user = f"e2e-lock-{os.urandom(3).hex()}"
    with TestClient(webapp_module.app) as lock_client:
        lock_reg = lock_client.post("/api/auth/register", json={
            "username": lock_user, "password": "Password123!",
        })
        check("锁定用例注册 200", lock_reg.status_code == 200, lock_reg.text[:120])
        lock_id = lock_reg.json()["user"]["id"]
        lock_client.post("/api/auth/logout")
        last = None
        for _ in range(5):
            last = lock_client.post("/api/auth/login", json={
                "username": lock_user, "password": "WrongPass1!",
            })
        check("连续失败触发锁定 429",
              last.status_code == 429 and int(last.headers.get("Retry-After", "0")) > 0,
              last.text[:120])
        blocked = lock_client.post("/api/auth/login", json={
            "username": lock_user, "password": "Password123!",
        })
        check("锁定期内正确密码仍被拒", blocked.status_code == 429, blocked.text[:120])

    locked = client.get("/api/admin/security/locked")
    check("锁定列表包含该账户",
          locked.status_code == 200
          and any(i.get("id") == lock_id and i.get("kind") == "user"
                  for i in locked.json().get("items", [])),
          locked.text[:120])
    unlock = client.post("/api/admin/security/unlock", json={"kind": "user", "id": lock_id})
    check("管理员解锁 200", unlock.status_code == 200, unlock.text[:120])
    with TestClient(webapp_module.app) as relock_client:
        re_ok = relock_client.post("/api/auth/login", json={
            "username": lock_user, "password": "Password123!",
        })
        check("解锁后可正常登录", re_ok.status_code == 200, re_ok.text[:120])

    # 17. 通知中心
    notes = client.get("/api/notifications").json()
    check("通知中心有记录", len(notes["items"]) >= 1, f"unread={notes['unread']}")

    # 18. 导出
    export = client.get("/api/export", params={"persona_id": pid})
    check("导出 zip 200", export.status_code == 200 and export.headers.get("content-type", "").startswith("application/zip"),
          f"status={export.status_code} bytes={len(export.content)}")

    # 19. 后台运维增强：会话 / 日志 / 告警 / 兑换码导出与批量作废
    sessions = client.get(f"/api/admin/users/{user_id}/sessions")
    check("用户会话列表 200",
          sessions.status_code == 200 and all("token_prefix" in i for i in sessions.json().get("items", [])),
          sessions.text[:120])
    revoke_all = client.post(f"/api/admin/users/{user_id}/sessions/revoke-all")
    check("全部下线 200", revoke_all.status_code == 200, revoke_all.text[:120])
    after_revoke = client.get(f"/api/admin/users/{user_id}/sessions")
    check("下线后会话清空", after_revoke.json().get("items") == [], after_revoke.text[:120])

    store.set_user_totp(user_id, "E2ESECRETE2ESECRET", True)
    reset_totp = client.post(f"/api/admin/users/{user_id}/totp/reset")
    check("重置双因素 200",
          reset_totp.status_code == 200 and not store.get_user(user_id).get("totp_enabled"),
          reset_totp.text[:120])

    logs = client.get("/api/admin/system/logs", params={"limit": 50})
    check("应用日志 200", logs.status_code == 200 and isinstance(logs.json().get("items"), list),
          logs.text[:120])
    logs_error = client.get("/api/admin/system/logs", params={"level": "ERROR"})
    check("日志级别筛选 200", logs_error.status_code == 200, logs_error.text[:120])

    alerts = client.get("/api/admin/alerts", params={"limit": 20})
    check("告警中心 200", alerts.status_code == 200 and "items" in alerts.json(), alerts.text[:120])
    alert_items = alerts.json().get("items") or []
    if alert_items:
        mark = client.post(f"/api/admin/alerts/{alert_items[0]['id']}/read")
        check("告警逐条已读 200", mark.status_code in (200, 404), mark.text[:120])
    else:
        check("告警逐条已读 200", True, "无告警，跳过")

    codes_export = client.get("/api/admin/redemption-codes/export")
    check("兑换码导出 CSV 200",
          codes_export.status_code == 200 and codes_export.content.startswith(b"\xef\xbb\xbf"),
          f"status={codes_export.status_code}")
    void_targets = client.post("/api/admin/redemption-codes",
                               json={"count": 2, "coins": 5, "batch": "e2e-void"}).json()["items"]
    batch_void = client.post("/api/admin/redemption-codes/batch-void",
                             json={"ids": [i["id"] for i in void_targets]})
    check("批量作废 200 且命中",
          batch_void.status_code == 200 and batch_void.json().get("voided") == 2,
          batch_void.text[:120])
    empty_void = client.post("/api/admin/redemption-codes/batch-void", json={})
    check("空选择批量作废 400", empty_void.status_code == 400, empty_void.text[:120])

    # 20. 管理员强制双因素：开启后未绑定管理员只能走绑定流程
    force_on = client.put("/api/admin/system/flags/admin_force_totp", json={"value": True})
    check("开启强制双因素", force_on.status_code == 200, force_on.text[:120])
    with TestClient(webapp_module.app) as f_client:
        f_login = f_client.post("/api/admin/auth/login", json={
            "username": admin_name, "password": "Password123!",
        })
        check("未绑定管理员返回绑定标记",
              f_login.status_code == 200 and f_login.json().get("totp_setup_required") is True,
              f_login.text[:120])
        f_blocked = f_client.get("/api/admin/summary")
        check("未绑定访问业务接口 403", f_blocked.status_code == 403, f_blocked.text[:120])
        f_setup = f_client.post("/api/admin/account/2fa/setup")
        secret = (f_setup.json() or {}).get("secret", "")
        f_enable = (
            f_client.post("/api/admin/account/2fa/enable",
                          json={"code": accounts.totp_code_now(secret)})
            if secret else None
        )
        check("绑定双因素后业务接口 200",
              f_enable is not None and f_enable.status_code == 200
              and f_client.get("/api/admin/summary").status_code == 200,
              f_enable.text[:120] if f_enable is not None else "no secret")
    client.put("/api/admin/system/flags/admin_force_totp", json={"value": False})

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n===== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed =====")
    if failed:
        print("FAILED:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
