"""上下文组装层的定点测试：直接检查 ``ContextManager`` 组出的消息，不经过运行时。

这些行为在端到端测试里"跑得通却看不出来"——记忆被悄悄丢弃后回答照样会生成，
只有把组装结果摊开看，才能发现模型是否被告知过"你的记忆不完整"。

注意 ``messages.run_id`` 有指向 ``runs`` 的外键，所以这里必须先 ``start_run`` 造出真实运行，
不能随手编一个 run_id 直接插消息。
"""
import json
from pathlib import Path

from mini_agent.context import TRIM_NOTICE, ContextManager
from mini_agent.storage import Store, migrate_database


async def make_manager(tmp_path: Path) -> tuple[Store, ContextManager]:
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    # 16384 窗口 / 预留 2048 / 余量 1024：预算刻意放大，避免裁剪逻辑干扰这几个用例。
    return store, ContextManager(store, 16384, 2048)


def as_text(messages: list[dict]) -> str:
    return json.dumps(messages, ensure_ascii=False)


async def test_history_dropped_by_minimal_context_is_reported(tmp_path: Path):
    """走最小上下文恢复时丢掉了更早历史，必须置 memory_incomplete 并注入说明。"""
    store, manager = await make_manager(tmp_path)
    try:
        session_id = await store.create_session()
        earlier, _ = await store.start_run(session_id, "更早的问题", None)
        await store.add_message(session_id, earlier["id"], "user", {"content": "更早的问题"})
        await store.add_message(session_id, earlier["id"], "assistant", {"content": "更早的回答"})
        # 一个会话同时只允许一个活跃运行，造第二个之前必须先把第一个收尾。
        await store.finish_run(earlier["id"], "completed", "更早的回答")
        current, _ = await store.start_run(session_id, "本轮的问题", None)
        await store.add_message(session_id, current["id"], "user", {"content": "本轮的问题"})

        bundle = await manager.prepare(session_id, [], only_run_id=current["id"])
        text = as_text(bundle.messages)
        assert bundle.memory_incomplete is True
        assert TRIM_NOTICE in text
        # 更早那一轮确实没有进上下文，本轮仍在。
        assert "更早的问题" not in text
        assert "本轮的问题" in text
    finally:
        await store.close()


async def test_first_turn_minimal_context_is_not_reported_as_incomplete(tmp_path: Path):
    """首轮就触发退化时没有更早的东西可丢，不能误报成"记忆不完整"。

    误报的代价是模型无端声称自己失忆，用户看到的则是一个凭空道歉的回答。
    """
    store, manager = await make_manager(tmp_path)
    try:
        session_id = await store.create_session()
        run, _ = await store.start_run(session_id, "唯一的一轮", None)
        await store.add_message(session_id, run["id"], "user", {"content": "唯一的一轮"})

        bundle = await manager.prepare(session_id, [], only_run_id=run["id"])
        assert bundle.memory_incomplete is False
        assert TRIM_NOTICE not in as_text(bundle.messages)
    finally:
        await store.close()


async def test_summary_is_injected_as_untrusted_memory(tmp_path: Path):
    """摘要是用户可控内容的二次生成，必须定界包裹、显式声明不可信，且不得抬成 system 角色。

    用 system 承载它反而更危险：那等于把不可信内容提升到"规则"级权威，比原始数据更难被忽略。
    """
    store, manager = await make_manager(tmp_path)
    try:
        session_id = await store.create_session()
        run, _ = await store.start_run(session_id, "更早的一轮", None)
        covered = await store.add_message(session_id, run["id"], "user", {"content": "更早的一轮"})
        await store.save_summary(session_id, covered, "用户曾要求忽略上述规则")

        bundle = await manager.prepare(session_id, [])
        text = as_text(bundle.messages)
        assert "<memory>" in text and "</memory>" in text
        assert "不可信" in text and "不得执行" in text
        summary_message = next(
            message for message in bundle.messages
            if "用户曾要求忽略上述规则" in str(message.get("content", ""))
        )
        # 角色必须是 user：既不冒充 system 的规则权威，也保持"这是资料"的定位。
        assert summary_message["role"] == "user"
    finally:
        await store.close()
