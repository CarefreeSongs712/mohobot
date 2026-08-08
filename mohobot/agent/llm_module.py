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
from openai import AsyncOpenAI

from mohobot.agent.prompts import PROMPT_TEMPLATES


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
        use_json = kwargs.pop("use_json", self.use_json)
        if use_json:
            params["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**params)
            content = resp.choices[0].message.content or ""
            duration_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                f"LLM[{self.module_name}] {self.model} OK {duration_ms:.0f}ms "
                f"({len(prompt)} prompt chars)"
            )
            self._record_usage(resp)
            return content
        except Exception as e:
            logger.error(f"LLM[{self.module_name}] call failed: {e}")
            raise

    def _record_usage(self, resp) -> None:
        """把本次调用的 token 用量写入 stats/llm_usage.jsonl(面板统计)。"""
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        try:
            import aiofiles as _aiofiles
            from pathlib import Path as _Path
            usage_dir = _Path(self._data_dir) / "stats"
            usage_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "time": time.time(),
                "bot_id": self._bot_id,
                "module": self.module_name,
                "model": self.model,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }
            # 同步写文件即可(每次调用一次, 量小); 失败不影响主流程
            import json as _json
            with open(usage_dir / "llm_usage.jsonl", "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"Record LLM usage failed: {e}")

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
