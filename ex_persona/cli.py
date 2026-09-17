import argparse
import os
from pathlib import Path

from . import agent, distill, ingest


def cmd_ingest(args: argparse.Namespace) -> int:
    source = Path(args.input) if args.input else None
    result = ingest.run(source, Path(args.out), args.target, args.text)
    print(f"解析完成: {len(result.messages)} 条消息，目标对象 = {result.target}")
    print(f"输出: {Path(args.out) / 'messages.jsonl'}")
    return 0


def cmd_distill(args: argparse.Namespace) -> int:
    result = distill.run(Path(args.profile_dir), use_llm=not args.no_llm)
    profile = result["profile"]
    print(f"蒸馏完成: 目标 = {profile['name']}，样本 = {profile['message_count']} 条")
    print(f"调用大模型: {profile['used_llm']}，对话检索对 = {profile['pair_count']} 条")
    print(f"输出: {Path(args.profile_dir) / 'SKILL.md'}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    bot = agent.PersonaAgent(args.profile_dir)
    print(f"已加载人格: {bot.persona.name}，模型: {bot.config.model}")
    print("直接输入消息开始聊天，输入 exit 退出。")
    history: list[dict] = []
    while True:
        try:
            text = input("我: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit", ":q"}:
            break
        reply = bot.reply(text, history=history)
        print(f"{bot.persona.name}: {reply}")
        history.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ]
        )
        if len(history) > 20:
            history = history[-20:]
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    os.environ["PERSONA_PROFILE_DIR"] = args.profile_dir
    import uvicorn

    from .server import app

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    os.environ["PERSONA_PORT"] = str(args.port)
    import uvicorn

    from .webapp import app

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    from . import accounts, store, workspace

    store.init_db()
    try:
        user = accounts.register(args.username, args.password)
    except accounts.AccountError as error:
        print(f"创建失败：{error.message}")
        return 1
    workspace.ensure_user(user["id"])
    print(f"已创建用户 id={user['id']} username={user['username']}")
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    from . import store

    store.init_db()
    user = store.get_user_by_username(args.username)
    if user is None:
        print(f"用户不存在：{args.username}")
        return 1
    store.set_user_role(user["id"], args.role)
    store.add_audit(0, "cli", "user.set_role", str(user["id"]), f"role={args.role}")
    print(f"已设置 id={user['id']} username={user['username']} role={args.role}")
    return 0


def cmd_set_password(args: argparse.Namespace) -> int:
    from . import accounts, store

    store.init_db()
    user = store.get_user_by_username(args.username)
    if user is None:
        print(f"用户不存在：{args.username}")
        return 1
    try:
        accounts.set_username_password(user["id"], user.get("username") or args.username, args.password)
    except accounts.AccountError as error:
        print(f"设置失败：{error.message}")
        return 1
    store.delete_user_sessions(user["id"])
    store.add_audit(0, "cli", "user.reset_password", str(user["id"]), f"username={args.username}")
    print(f"已重置 id={user['id']} username={user['username']} 的密码，旧会话已全部失效")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ex_persona", description="前任人格蒸馏与微信机器人")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="解析多平台聊天记录")
    p_ingest.add_argument("--input", default=None, help="聊天记录文件或目录")
    p_ingest.add_argument("--out", default="data/profile")
    p_ingest.add_argument("--target", default=None)
    p_ingest.add_argument("--text", default=None, help="直接追加的文本（描述或粘贴的记录）")
    p_ingest.set_defaults(func=cmd_ingest)

    p_distill = sub.add_parser("distill", help="蒸馏人格")
    p_distill.add_argument("--profile-dir", default="data/profile")
    p_distill.add_argument("--no-llm", action="store_true")
    p_distill.set_defaults(func=cmd_distill)

    p_chat = sub.add_parser("chat", help="本地命令行对话")
    p_chat.add_argument("--profile-dir", default="data/profile")
    p_chat.set_defaults(func=cmd_chat)

    p_serve = sub.add_parser("serve", help="启动 OpenAI 兼容服务，供 weclaw 调用")
    p_serve.add_argument("--profile-dir", default="data/profile")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_web = sub.add_parser("web", help="启动多用户平台（官网 + 登录 + 工作台）")
    p_web.add_argument("--host", default="0.0.0.0")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.set_defaults(func=cmd_web)

    p_user = sub.add_parser("create-user", help="运维：直接创建一个用户名密码账号")
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--password", required=True)
    p_user.set_defaults(func=cmd_create_user)

    p_role = sub.add_parser("set-role", help="运维：设置用户角色（user / admin）")
    p_role.add_argument("--username", required=True)
    p_role.add_argument("--role", choices=["user", "admin"], default="admin")
    p_role.set_defaults(func=cmd_set_role)

    p_password = sub.add_parser("set-password", help="运维：重置指定用户的密码并注销其全部会话")
    p_password.add_argument("--username", required=True)
    p_password.add_argument("--password", required=True)
    p_password.set_defaults(func=cmd_set_password)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
