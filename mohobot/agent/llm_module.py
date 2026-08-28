"""LLM 调用模块 — 配置驱动的 OpenAI 兼容封装。

移植自 Agent-LuoTianyi (src/utils/llm/llm_module.py)。
每个模块(main_chat / topic_extractor / memory_writer / user_profile_updater)
使用自己的模型配置与 prompt 模板。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from mohobot.agent.prompts import PROMPT_TEMPLATES
from mohobot.services.usage import UsageRecorder


class LLMModule:
    """一次 LLM 调用的封装: 模型 + prompt 模板 + 参数。

    成功调用后记录 token 用量到 stats/llm_usage.jsonl
    (与 LLMService 旧路径同格式, 供 Web 面板统计)。
    """

    def __init__(
        self,
        module_name: str,
        config: dict[str, Any],
        *,
        prompt_name: str | None = None,
        template: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        use_json: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        data_dir: str = "",
        bot_id: str = "",
        fallback_model: str = "",
        usage_recorder: UsageRecorder | None = None,
    ):
        self.module_name = module_name
        self._cfg = config
        self.prompt_name = prompt_name or (config.get("prompt_name") if isinstance(config, dict) else None)
        self.template = template
        self.model = model or (config.get("model") if isinstance(config, dict) else None) or ""
        self.base_url = base_url or (config.get("base_url") if isinstance(config, dict) else None) or ""
        self.api_key = api_key or (config.get("api_key") if isinstance(config, dict) else None) or ""
        self.use_json = use_json
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._data_dir = data_dir or "./data"
        self._bot_id = bot_id
        self._usage_recorder = usage_recorder or UsageRecorder(self._data_dir)
        self._owns_usage_recorder = usage_recorder is None
        # 全局备用模型: 主模型连接类失败时换用(空 = 不回退)
        self.fallback_model = (
            fallback_model
            or (config.get("fallback_model") if isinstance(config, dict) else None)
            or ""
        )

        self._tools_schemas: list[dict[str, Any]] = []
        self._tool_registry = None
        try:
            from mohobot.services.llm_tools import registry
            self._tool_registry = registry
            self._tools_schemas = registry.schemas()
        except Exception as e:
            logger.warning(f"插件工具加载失败: {e}")

        if self.template is None and self.prompt_name:
            self.template = PROMPT_TEMPLATES.get(self.prompt_name, "")

        self._client: AsyncOpenAI | None = None
        if self.api_key and self.base_url:
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    def is_available(self) -> bool:
        return self._client is not None and bool(self.model)

    def get_variables(self) -> list[str]:
        """返回模板中的变量名(简化: 通过 {{ }} 匹配)。"""
        import re
        if not self.template:
            return []
        return list(dict.fromkeys(re.findall(r"{{\s*(\w+)\s*}}", self.template)))

    async def generate_response(self, **kwargs) -> str:
        """用模板 + 参数生成 prompt 并调用 LLM,返回文本响应。

        若传 use_json=True 则使用 JSON 模式(部分网关支持 response_format)。
        """
        if not self.is_available():
            raise RuntimeError(
                f"LLM module '{self.module_name}' not configured "
                f"(model={self.model!r}, base_url={self.base_url!r})"
            )

        prompt = self._render_prompt(kwargs)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self._tools_schemas:
            params["tools"] = self._tools_schemas
            params["tool_choice"] = "auto"
        use_json = kwargs.pop("use_json", self.use_json)
        if use_json:
            params["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**params)
            choice = resp.choices[0] if resp.choices else None
            if choice and getattr(choice.message, "tool_calls", None) and self._tool_registry is not None:
                messages = list(params["messages"])
                messages.append({
                    "role": "assistant",
                    "content": choice.message.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in choice.message.tool_calls
                    ],
                })
                for tc in choice.message.tool_calls:
                    result = await self._tool_registry.execute(
                        tc.function.name, tc.function.arguments
                    )
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                follow_params = dict(params)
                follow_params["messages"] = messages
                resp = await self._client.chat.completions.create(**follow_params)
            content = resp.choices[0].message.content or ""
            duration_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                f"LLM[{self.module_name}] {self.model} OK {duration_ms:.0f}ms "
                f"({len(prompt)} prompt chars)"
            )
            await self._record_usage_async(resp, self.model)
            return content
        except Exception as e:
            # 仅连接类错误(连接失败/超时)回退全局备用模型重试一次
            if (
                self.fallback_model
                and self.fallback_model != self.model
                and isinstance(e, (APIConnectionError, APITimeoutError))
            ):
                logger.warning(
                    f"LLM[{self.module_name}] {self.model} 连接失败({e}), "
                    f"回退备用模型 {self.fallback_model} 重试"
                )
                try:
                    retry_params = dict(params)
                    retry_params["model"] = self.fallback_model
                    resp = await self._client.chat.completions.create(**retry_params)
                    content = resp.choices[0].message.content or ""
                    logger.debug(
                        f"LLM[{self.module_name}] {self.fallback_model} 回退成功 "
                        f"({len(prompt)} prompt chars)"
                    )
                    await self._record_usage_async(resp, self.fallback_model)
                    return content
                except Exception as e2:
                    logger.error(
                        f"LLM[{self.module_name}] fallback {self.fallback_model} "
                        f"也失败: {e2}"
                    )
            logger.error(f"LLM[{self.module_name}] call failed: {e}")
            raise

    async def _record_usage_async(self, resp, model_name: str | None = None) -> None:
        """异步记录一次成功的 provider HTTP 调用。"""
        await self._usage_recorder.record(
            getattr(resp, "usage", None),
            model=model_name or self.model,
            bot_id=self._bot_id,
            module=self.module_name,
            kind="chat",
        )

    def _record_usage(self, resp, model_name: str | None = None) -> None:
        """保留旧私有方法签名；调度异步记录，不再同步 open 写文件。"""
        import asyncio
        try:
            asyncio.get_running_loop().create_task(
                self._record_usage_async(resp, model_name)
            )
        except RuntimeError:
            logger.debug("Record LLM usage skipped: no running event loop")

    async def close(self) -> None:
        """关闭 OpenAI client；仅关闭本模块自行创建的 recorder。"""
        if self._client is not None:
            await self._client.close()
        if self._owns_usage_recorder:
            await self._usage_recorder.close()

    def _render_prompt(self, kwargs: dict[str, Any]) -> str:
        """Jinja2 渲染模板。"""
        if not self.template:
            raise RuntimeError(f"LLM module '{self.module_name}' has no prompt template")
        try:
            from jinja2 import Template
            return Template(self.template).render(**kwargs)
        except ImportError:
            # 无 jinja2 时退化为简单替换
            prompt = self.template
            for k, v in kwargs.items():
                prompt = prompt.replace("{{" + k + "}}", str(v if v is not None else ""))
            import re
            prompt = re.sub(r"{%.*?%}", "", prompt)
            prompt = re.sub(r"{{\s*\w+\s*}}", "", prompt)
            return prompt
        except Exception as e:
            logger.error(f"Prompt render failed for {self.module_name}: {e}")
            raise


def parse_json_response(response: str) -> dict[str, Any] | None:
    """解析 LLM 返回的 JSON(兼容 ```json 代码块包装)。"""
    if not response:
        return None
    raw = response.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError as e:
        logger.debug(f"JSON parse failed: {e}; raw={raw[:200]}")
        return None
