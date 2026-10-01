"""Offline regression tests for exact-name tool routing and LLM hot updates."""

import asyncio
import json
import os
import sys
import traceback
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mohobot.llm_service import LLMService
from mohobot.models.config import GlobalConfig
from mohobot.services.llm_tools import LLMTool, LLMToolRegistry, tool_schema


_KEY_ENV = {
    "MOHOBOT_LLM_API_KEY": "",
    "MOHOBOT_VISION_API_KEY": "",
    "MOHOBOT_EMOTION_API_KEY": "",
}


def _config():
    config = GlobalConfig()
    config.llm.chat_api_key = "test-chat"
    config.llm.chat_base_url = "https://chat.invalid/v1"
    config.llm.vision_base_url = ""
    return config


def _response(content="mock reply", tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=None,
    )


class _MockClient:
    def __init__(self, *, api_key, base_url):
        self.api_key = api_key
        self.base_url = base_url
        self.close = AsyncMock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=_response())))


@contextmanager
def _runtime(config=None, tools=None):
    """Replace every OpenAI client and recorder; never load config or user data."""
    clients = []

    def create(**kwargs):
        client = _MockClient(**kwargs)
        clients.append(client)
        return client

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _KEY_ENV))
        factory = stack.enter_context(patch("mohobot.llm_service.AsyncOpenAI", side_effect=create))
        tools = tools if tools is not None else LLMToolRegistry()
        stack.enter_context(patch("mohobot.services.llm_tools.registry", tools))
        recorder = SimpleNamespace(record=AsyncMock(), close=AsyncMock())
        svc = LLMService(config or _config(), usage_recorder=recorder)
        yield svc, clients, factory, tools


def _roles(svc):
    return [svc._chat_client, svc._vision_client, svc._emotion_client]


def _schema_names(svc):
    return [tool["function"]["name"] for tool in svc._current_tools_schemas()]


async def test_registry_contains_and_handler_compatibility():
    tools = LLMToolRegistry()
    sync_handler = Mock(return_value={"value": "中文"})
    async_handler = AsyncMock(return_value="plain text")
    tools.register(LLMTool(tool_schema("custom_lookup", "lookup", {}, []), sync_handler))
    tools.register(LLMTool(tool_schema("async_lookup", "lookup", {}, []), async_handler))
    assert tools.contains("custom_lookup")
    assert not tools.contains("song_missing")
    assert json.loads(await tools.execute("custom_lookup", '{"query":"x"}')) == {"value": "中文"}
    sync_handler.assert_called_once_with(query="x")
    assert await tools.execute("async_lookup", {}) == "plain text"
    async_handler.assert_awaited_once_with()
    try:
        tools.register(LLMTool(tool_schema("custom_lookup", "duplicate", {}, []), sync_handler))
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate tool name must be rejected")


async def test_non_song_plugin_schema_and_execution_stay_dynamic():
    with _runtime() as (svc, _, _, tools):
        handler = AsyncMock(return_value={"answer": 42})
        schema = tool_schema("weather_lookup", "non-song plugin", {"city": {"type": "string"}}, ["city"])
        tools.register(LLMTool(schema, handler))
        assert schema in svc._current_tools_schemas()
        assert _schema_names(svc).count("weather_lookup") == 1
        assert json.loads(await svc._execute_tool("weather_lookup", '{"city":"上海"}')) == {"answer": 42}
        handler.assert_awaited_once_with(city="上海")
        await svc.close()


async def test_invalid_arguments_and_unknown_tools_are_controlled_errors():
    with _runtime() as (svc, _, _, tools):
        handler = Mock(return_value="should not run")
        tools.register(LLMTool(tool_schema("custom_lookup", "lookup", {}, []), handler))
        invalid = ["", " ", "not JSON", "{broken", "[]", "null", '"text"', "0", "false", "true"]
        for arguments in invalid:
            for name in ("custom_lookup", "get_current_time", "get_group_member_info", "anysearch_search"):
                assert "error" in json.loads(await svc._execute_tool(name, arguments)), (name, arguments)
            assert "error" in json.loads(await tools.execute("custom_lookup", arguments))
        for arguments in (None, [], 0, False, True):
            assert "error" in json.loads(await tools.execute("custom_lookup", arguments))
            assert "error" in json.loads(await svc._execute_tool("custom_lookup", arguments))
        handler.assert_not_called()
        for name in ("unknown", "song_missing"):
            assert json.loads(await svc._execute_tool(name, "{}"))["error"] == f"未知工具: {name}"
            assert json.loads(await tools.execute(name, "{}"))["error"] == f"未知工具: {name}"
        await svc.close()


async def test_builtins_win_collisions_and_member_stub_is_not_advertised():
    with _runtime() as (svc, _, _, tools):
        handler = Mock(return_value="plugin hijacked builtin")
        for name in ("get_current_time", "get_group_member_info", "anysearch_search"):
            tools.register(LLMTool(tool_schema(name, "conflicting plugin", {}, []), handler))
        assert _schema_names(svc) == ["get_current_time"]
        assert "get_group_member_info" not in [t["function"]["name"] for t in svc._tools_schemas]
        with patch("mohobot.utils.time_utils.format_utc8", return_value="mock time"):
            assert await svc._execute_tool("get_current_time", "{}") == "mock time"
        assert "error" in json.loads(await svc._execute_tool("get_group_member_info", "{}"))
        assert "Anysearch" in json.loads(await svc._execute_tool("anysearch_search", '{"query":"test"}'))["error"]
        handler.assert_not_called()
        await svc.close()


async def test_anysearch_availability_and_config_remain_unchanged_by_llm_sync():
    config = _config()
    config.anysearch.api_key = "mock-search"
    with _runtime(config) as (svc, _, _, tools):
        handler = Mock()
        tools.register(LLMTool(tool_schema("anysearch_search", "conflicting plugin", {}, []), handler))
        search = svc._anysearch_client
        search.safe_search = AsyncMock(return_value="mock search result")
        schema = next(t for t in svc._current_tools_schemas() if t["function"]["name"] == "anysearch_search")
        assert schema["function"]["description"] != "conflicting plugin"
        assert await svc._execute_tool("anysearch_search", '{"query":"test"}') == "mock search result"
        search.safe_search.assert_awaited_once_with("test", max_results=5)
        assert "error" in json.loads(await svc._execute_tool("anysearch_search", '{"query":""}'))
        updated = deepcopy(config)
        updated.anysearch.api_key = "not applied"
        updated.anysearch.enabled = False
        updated.llm.chat_model = "new-model"
        await svc.sync_config(updated)
        assert svc._anysearch_client is search
        assert config.anysearch.api_key == "mock-search"
        assert _schema_names(svc).count("anysearch_search") == 1
        handler.assert_not_called()
        await svc.close()


async def test_existing_song_schemas_and_handlers_still_route():
    from plugins.song_tools import TOOLS

    with _runtime() as (svc, _, _, tools):
        for original in TOOLS:
            handler = Mock(return_value={"song": original.name})
            tools.register(LLMTool(original.schema, handler))
            assert original.schema in svc._current_tools_schemas()
            arguments = {"query": "测试歌曲", "limit": 3} if original.name == "song_search" else {"song_name": "测试歌曲"}
            result = await svc._execute_tool(original.name, json.dumps(arguments))
            assert json.loads(result) == {"song": original.name}
            handler.assert_called_once_with(**arguments)
        await svc.close()


async def test_model_and_parameter_updates_reuse_clients_and_only_copy_llm():
    config = _config()
    with _runtime(config) as (svc, clients, factory, _):
        old_clients = _roles(svc)
        old_llm = config.llm
        other_sections = {name: getattr(config, name) for name in vars(config) if name != "llm"}
        updated = deepcopy(config)
        updated.llm.chat_model = "new-chat"
        updated.llm.chat_temperature = 0.12
        updated.llm.chat_max_tokens = 1234
        updated.llm.vision_model = "new-vision"
        updated.llm.vision_temperature = 0.23
        updated.llm.vision_max_tokens = 2345
        updated.llm.vision_prompt = "new prompt"
        updated.llm.emotion_model = "new-emotion"
        updated.llm.emotion_temperature = 0.34
        updated.llm.emotion_max_tokens = 3456
        updated.llm.summarize_temperature = 0.45
        updated.llm.summarize_max_tokens = 4567
        updated.llm.models = ["new-chat", "new-vision"]
        updated.data_dir = "must-not-apply"
        updated.admins = [999]
        await svc.sync_config(updated)
        assert svc._cfg is config
        assert config.llm is not old_llm and config.llm is not updated.llm
        assert config.llm == updated.llm
        assert all(getattr(config, name) is value for name, value in other_sections.items())
        assert _roles(svc) == old_clients
        assert factory.call_count == len(clients) == 1
        assert svc._retired_clients == []
        updated.llm.models.append("must-not-share")
        assert "must-not-share" not in config.llm.models

        svc._build_messages = AsyncMock(return_value=[{"role": "user", "content": "mock"}])
        assert (await svc.chat("bot_test", None, [], {}))[0] == "mock reply"
        request = svc._chat_client.chat.completions.create.call_args.kwargs
        assert (request["model"], request["temperature"], request["max_tokens"]) == ("new-chat", 0.12, 1234)
        assert await svc.describe_image("data:image/png;base64,AA==") == "mock reply"
        request = svc._vision_client.chat.completions.create.call_args.kwargs
        assert (request["model"], request["temperature"], request["max_tokens"]) == ("new-vision", 0.23, 2345)
        assert request["messages"][0]["content"][0]["text"] == "new prompt"
        assert await svc.analyze_emotion("mock") == "mock reply"
        request = svc._emotion_client.chat.completions.create.call_args.kwargs
        assert (request["model"], request["temperature"], request["max_tokens"]) == ("new-emotion", 0.34, 3456)
        assert await svc.summarize_context([{"role": "user", "content": "mock"}]) == "mock reply"
        request = svc._chat_client.chat.completions.create.call_args.kwargs
        assert (request["temperature"], request["max_tokens"]) == (0.45, 4567)
        await svc.close()


async def test_each_provider_key_and_url_change_rebuilds_only_affected_role():
    for index, role in enumerate(("chat", "vision", "emotion")):
        config = _config()
        config.llm.vision_api_key = "test-vision"
        config.llm.vision_base_url = "https://vision.invalid/v1"
        config.llm.emotion_api_key = "test-emotion"
        config.llm.emotion_base_url = "https://emotion.invalid/v1"
        with _runtime(config) as (svc, clients, factory, _):
            for field, value in (("api_key", f"new-{role}"), ("base_url", f"https://new-{role}.invalid/v1")):
                old_clients = _roles(svc)
                updated = deepcopy(config)
                setattr(updated.llm, f"{role}_{field}", value)
                await svc.sync_config(updated)
                assert _roles(svc)[index] is not old_clients[index]
                assert all(new is old for i, (new, old) in enumerate(zip(_roles(svc), old_clients)) if i != index)
                assert old_clients[index] in svc._retired_clients
                old_clients[index].close.assert_not_awaited()
            assert factory.call_count == len(clients) == 5
            await svc.close()
            for client in clients:
                client.close.assert_awaited_once_with()


async def test_same_key_different_urls_are_independent_and_matching_pairs_share():
    config = _config()
    config.llm.vision_api_key = config.llm.emotion_api_key = config.llm.chat_api_key
    config.llm.vision_base_url = "https://vision.invalid/v1"
    config.llm.emotion_base_url = "https://emotion.invalid/v1"
    with _runtime(config) as (svc, clients, factory, _):
        assert len({id(client) for client in _roles(svc)}) == 3
        assert [client.base_url for client in _roles(svc)] == [config.llm.chat_base_url, config.llm.vision_base_url, config.llm.emotion_base_url]
        old_vision = svc._vision_client
        updated = deepcopy(config)
        updated.llm.vision_base_url = updated.llm.emotion_base_url
        await svc.sync_config(updated)
        assert svc._vision_client is svc._emotion_client
        assert svc._chat_client is not svc._vision_client
        assert factory.call_count == 3
        assert svc._retired_clients == [old_vision]
        await svc.close()
        for client in clients:
            client.close.assert_awaited_once_with()

    config.llm.vision_api_key = config.llm.emotion_api_key = ""
    config.llm.vision_base_url = "https://vision-fallback.invalid/v1"
    config.llm.emotion_base_url = "https://emotion-fallback.invalid/v1"
    with _runtime(config) as (svc, _, _, _):
        assert len({id(client) for client in _roles(svc)}) == 3
        assert all(client.api_key == config.llm.chat_api_key for client in _roles(svc))
        assert svc._emotion_client.base_url == config.llm.emotion_base_url
        await svc.close()


async def test_env_keys_and_chat_fallbacks_survive_initialization_and_sync():
    config = _config()
    config.llm.chat_api_key = ""
    with _runtime(config) as (svc, _, factory, _):
        assert _roles(svc) == [None, None, None]
        env = dict(zip(_KEY_ENV, ("env-chat", "env-vision", "env-emotion")))
        with patch.dict(os.environ, env):
            await svc.sync_config(config)
            assert [client.api_key for client in _roles(svc)] == list(env.values())
            assert svc._available and svc._vision_available
            assert config.llm.chat_api_key == config.llm.vision_api_key == config.llm.emotion_api_key == ""
            count = factory.call_count
            await svc.sync_config(deepcopy(config))
            assert factory.call_count == count
            updated = deepcopy(config)
            updated.llm.chat_api_key = "explicit-chat"
            updated.llm.vision_api_key = "explicit-vision"
            updated.llm.emotion_api_key = "explicit-emotion"
            await svc.sync_config(updated)
            assert [client.api_key for client in _roles(svc)] == ["explicit-chat", "explicit-vision", "explicit-emotion"]
        updated = deepcopy(config)
        updated.llm.chat_api_key = updated.llm.vision_api_key = updated.llm.emotion_api_key = ""
        with patch.dict(os.environ, {"MOHOBOT_LLM_API_KEY": "fallback-chat"}):
            await svc.sync_config(updated)
            assert svc._chat_client is svc._vision_client is svc._emotion_client
            assert svc._chat_client.api_key == "fallback-chat"
        await svc.sync_config(updated)
        assert _roles(svc) == [None, None, None]
        assert not svc._available and not svc._vision_available
        await svc.close()


async def test_constructor_resolves_env_keys_without_network():
    with patch.dict(os.environ, {"MOHOBOT_LLM_API_KEY": "env-chat", "MOHOBOT_VISION_API_KEY": "env-vision", "MOHOBOT_EMOTION_API_KEY": "env-emotion"}):
        config = _config()
        config.llm.chat_api_key = ""
        with patch("mohobot.llm_service.AsyncOpenAI", side_effect=_MockClient) as factory:
            svc = LLMService(config, usage_recorder=SimpleNamespace(record=AsyncMock(), close=AsyncMock()))
            assert [client.api_key for client in _roles(svc)] == ["env-chat", "env-vision", "env-emotion"]
            assert factory.call_count == 3
            await svc.close()


async def test_vision_availability_model_toggle_does_not_rebuild():
    with _runtime() as (svc, clients, factory, _):
        updated = deepcopy(svc._cfg)
        updated.llm.vision_model = ""
        await svc.sync_config(updated)
        assert svc._available and not svc._vision_available
        assert await svc.describe_image("data:image/png;base64,AA==") == ""
        clients[0].chat.completions.create.assert_not_awaited()
        updated.llm.vision_model = "new-vision"
        await svc.sync_config(updated)
        assert svc._vision_available
        assert factory.call_count == 1
        updated.llm.chat_api_key = ""
        updated.llm.vision_api_key = "vision-only"
        await svc.sync_config(updated)
        assert not svc._available and svc._vision_available
        assert svc._chat_client is svc._emotion_client is None
        await svc.close()


async def test_failed_client_creation_keeps_config_and_clients_and_closes_unpublished():
    config = _config()
    with _runtime(config) as (svc, clients, factory, _):
        old_llm = config.llm
        old_clients = _roles(svc)
        updated = deepcopy(config)
        updated.llm.chat_api_key = "new-chat"
        updated.llm.vision_api_key = "new-vision"
        updated.llm.emotion_api_key = "new-emotion"
        unpublished = _MockClient(api_key="new-chat", base_url=config.llm.chat_base_url)
        factory.side_effect = [unpublished, RuntimeError("mock construction failure")]
        try:
            await svc.sync_config(updated)
        except RuntimeError as exc:
            assert str(exc) == "mock construction failure"
        else:
            raise AssertionError("client construction failure must propagate")
        assert svc._cfg is config and config.llm is old_llm
        assert _roles(svc) == old_clients
        assert svc._retired_clients == []
        assert svc._available and svc._vision_available
        unpublished.close.assert_awaited_once_with()
        clients[0].close.assert_not_awaited()
        await svc.close()
        clients[0].close.assert_awaited_once_with()
        unpublished.close.assert_awaited_once_with()


async def test_inflight_chat_and_tool_followup_keep_old_client_open():
    with _runtime() as (svc, clients, _, _):
        old = svc._chat_client
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        tool_call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="get_current_time", arguments="{}"))

        async def complete(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                old.close.assert_not_awaited()
                return _response(tool_calls=[tool_call])
            return _response("old followup finished")

        old.chat.completions.create.side_effect = complete
        svc._build_messages = AsyncMock(return_value=[{"role": "user", "content": "mock"}])
        task = asyncio.create_task(svc.chat("bot_test", None, [], {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            updated = deepcopy(svc._cfg)
            updated.llm.chat_api_key = "new-chat"
            await svc.sync_config(updated)
            assert svc._chat_client is not old and svc._retired_clients == [old]
            old.close.assert_not_awaited()
            release.set()
            reply, results = await asyncio.wait_for(task, 2)
            assert reply == "old followup finished" and results
            assert len(calls) == 2
            svc._chat_client.chat.completions.create.assert_not_awaited()
            assert (await svc.chat("bot_test", None, [], {}))[0] == "mock reply"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await svc.close()
        for client in clients:
            client.close.assert_awaited_once_with()


async def test_inflight_stream_and_followup_keep_old_client_open():
    with _runtime() as (svc, clients, _, _):
        old = svc._chat_client
        entered, release = asyncio.Event(), asyncio.Event()

        async def initial_stream():
            entered.set()
            await release.wait()
            old.close.assert_not_awaited()
            tool_call = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="get_current_time", arguments="{}"))
            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[tool_call]))])

        async def final_stream():
            yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content="old stream finished", tool_calls=None))])

        old.chat.completions.create.side_effect = [initial_stream(), final_stream()]
        svc._build_messages = AsyncMock(return_value=[{"role": "user", "content": "mock"}])

        async def collect():
            return [chunk async for chunk in svc.chat_stream("bot_test", None, [], {})]

        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            updated = deepcopy(svc._cfg)
            updated.llm.chat_base_url = "https://new-chat.invalid/v1"
            await svc.sync_config(updated)
            old.close.assert_not_awaited()
            assert svc._retired_clients == [old]
            release.set()
            chunks = await asyncio.wait_for(task, 2)
            assert "".join(text for text, _ in chunks) == "old stream finished"
            assert old.chat.completions.create.await_count == 2
            svc._chat_client.chat.completions.create.assert_not_awaited()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await svc.close()
        for client in clients:
            client.close.assert_awaited_once_with()


async def test_stream_invalid_arguments_reach_controlled_error_without_handler_execution():
    with _runtime() as (svc, _, _, tools):
        handler = Mock(return_value="should not run")
        tools.register(LLMTool(tool_schema("custom_lookup", "lookup", {}, []), handler))
        svc._build_messages = AsyncMock(side_effect=lambda *args: [{"role": "user", "content": "mock"}])
        for arguments in ("", "{broken", "[]"):
            async def initial():
                call = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="custom_lookup", arguments=arguments))
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))])

            async def final():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content="error handled", tool_calls=None))])

            svc._chat_client.chat.completions.create.side_effect = [initial(), final()]
            chunks = [chunk async for chunk in svc.chat_stream("bot_test", None, [], {})]
            assert "".join(text for text, _ in chunks) == "error handled"
            messages = svc._chat_client.chat.completions.create.call_args.kwargs["messages"]
            assert "error" in json.loads(messages[-1]["content"])
        handler.assert_not_called()
        await svc.close()


async def test_close_covers_all_generations_and_deduplicates_shared_roles_once():
    config = _config()
    config.llm.vision_api_key = config.llm.emotion_api_key = "auxiliary"
    with _runtime(config) as (svc, clients, _, _):
        assert svc._vision_client is svc._emotion_client is not svc._chat_client
        for generation in range(3):
            updated = deepcopy(config)
            updated.llm.chat_api_key = f"chat-{generation}"
            updated.llm.vision_api_key = updated.llm.emotion_api_key = f"auxiliary-{generation}"
            await svc.sync_config(updated)
        assert len(clients) == 8 and len(svc._retired_clients) == 6
        for client in clients:
            client.close.assert_not_awaited()
        svc._retired_clients.extend([clients[0], svc._vision_client])
        svc._owns_usage_recorder = True
        clients[0].close.side_effect = RuntimeError("mock close failure")
        await svc.close()
        await svc.close()
        for client in clients:
            client.close.assert_awaited_once_with()
        svc._usage_recorder.close.assert_awaited_once_with()
        assert svc._retired_clients == [] and _roles(svc) == [None, None, None]


async def test_close_handles_incomplete_new_stubs():
    empty = LLMService.__new__(LLMService)
    await empty.close()
    await empty.close()
    partial = LLMService.__new__(LLMService)
    client = _MockClient(api_key="test", base_url="https://stub.invalid")
    partial._chat_client = client
    partial._vision_client = client
    await partial.close()
    await partial.close()
    client.close.assert_awaited_once_with()


async def _main():
    failed = 0
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)
