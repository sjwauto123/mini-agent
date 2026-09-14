from mini_agent.contracts import ExecutionContext
from mini_agent.tools import calculator


async def test_calculator_is_safe():
    ok = await calculator({"expression": "-(2 + 3) * 4"}, ExecutionContext("r", "s"))
    unsafe = await calculator({"expression": "__import__('os').getcwd()"}, ExecutionContext("r", "s"))
    zero = await calculator({"expression": "1/0"}, ExecutionContext("r", "s"))
    assert ok.ok and ok.data["value"] == -20
    assert unsafe.error["code"] == "invalid_expression"
    assert zero.error["code"] == "division_by_zero"


async def test_registry_schema_and_duplicate(services):
    _, _, registry = services
    names = {item["function"]["name"] for item in registry.definitions()}
    assert names == {"calculator", "todo", "search", "weather", "resource_read", "resource_search"}
    result = await registry.execute("calculator", {"bad": "x"}, ExecutionContext("r", "s"))
    assert not result.ok and result.error["code"] == "invalid_arguments"
    # detail 只随工具结果进入模型上下文（前端不渲染 tool 消息），模型据此才能改对参数。
    typed = await registry.execute("calculator", {"expression": 5}, ExecutionContext("r", "s"))
    assert typed.error["detail"].startswith("expression")
    spec = registry.get("calculator")
    try:
        registry.register(spec)
        assert False, "duplicate registration should fail"
    except ValueError:
        pass


async def test_todos_are_isolated_by_session(services):
    store, _, registry = services
    first, second = await store.create_session(), await store.create_session()
    add = await registry.execute("todo", {"action": "add", "text": "window one"}, ExecutionContext("r1", first))
    one = await registry.execute("todo", {"action": "list"}, ExecutionContext("r2", first))
    two = await registry.execute("todo", {"action": "list"}, ExecutionContext("r3", second))
    assert add.ok and len(one.data["items"]) == 1
    assert two.data["items"] == []


async def test_resource_read_search_and_isolation(services):
    store, resources, registry = services
    first, second = await store.create_session(), await store.create_session()
    resource_id = await resources.save(first, "alpha beta gamma beta")
    page = await registry.execute("resource_read", {"resource_id": resource_id, "cursor": 0, "limit": 5}, ExecutionContext("r1", first))
    found = await registry.execute("resource_search", {"resource_id": resource_id, "query": "beta"}, ExecutionContext("r1", first))
    denied = await registry.execute("resource_read", {"resource_id": resource_id}, ExecutionContext("r2", second))
    assert page.data["content"] == "alpha" and page.data["next_cursor"] == 5
    assert found.data["matches"][0]["position"] == 6
    assert denied.error["code"] == "resource_not_found"
