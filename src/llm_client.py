# -*- coding: utf-8 -*-
"""
LLM 客户端与确定性本地缓存模块 (Dual-Insurance LLM Client)
- 优先从环境变量或项目根目录 .env 文件加载 LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
- 内置线程安全的 SHA-256 本地缓存 (data/cache/llm_cache.json)
- 内置基于 JSONDecodeError.pos 的未转义引号自动修复器与退避重试机制
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "llm_cache.json"

_CACHE_LOCK = threading.Lock()
_LLM_CACHE = {}


def _load_dotenv():
    """轻量级 .env 加载器（不依赖第三方 python-dotenv 库）"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


def _init_cache():
    global _LLM_CACHE
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _LLM_CACHE = json.load(f)
        except Exception:
            _LLM_CACHE = {}
    proto_v2_cache = BASE_DIR / "prototype_test" / "outputs_v2" / "proto_v2_llm_cache.json"
    if proto_v2_cache.exists():
        try:
            with open(proto_v2_cache, "r", encoding="utf-8") as f:
                p_cache = json.load(f)
                for k, v in p_cache.items():
                    if k not in _LLM_CACHE:
                        _LLM_CACHE[k] = v
        except Exception:
            pass


_init_cache()


def save_cache():
    with _CACHE_LOCK:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_LLM_CACHE, f, ensure_ascii=False, indent=2)


def robust_parse_json(text: str) -> dict:
    """鲁棒 JSON 解析器：剥离代码块、截取最外层 {...}、清理非法控制字符并自动修复字符串内未转义的双引号"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    # 1. 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 清理未转义控制字符（保留合法结构换行或替换为空格）
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    # 将字符串内部裸换行替换为空格（按行合并为单行 JSON）
    single_line = " ".join(line.strip() for line in cleaned.splitlines())

    # 3. 基于 JSONDecodeError.pos 循环修复未转义的内部双引号
    candidate = single_line
    for _ in range(40):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            if "Expecting ',' delimiter" in e.msg or "Expecting ':' delimiter" in e.msg or "Extra data" in e.msg:
                q_pos = candidate.rfind('"', 0, e.pos)
                if q_pos > 0:
                    candidate = candidate[:q_pos] + "「" + candidate[q_pos + 1 :]
                    # 同时如果 e.pos 之后紧接着还有成对的未转义引号，下一次迭代会自动修复
                    continue
            elif "Invalid \\escape" in e.msg:
                candidate = candidate[: e.pos] + "\\" + candidate[e.pos :]
                continue
            break

    return json.loads(single_line)


def call_llm_json(system_prompt: str, user_prompt: str, temperature: float = 0.0) -> dict:
    llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.longcat.chat/openai/v1").rstrip("/")
    llm_model = os.environ.get("LLM_MODEL", "LongCat-2.5-Preview")
    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()

    cache_key = hashlib.sha256(f"{llm_model}||{system_prompt}||{user_prompt}".encode("utf-8")).hexdigest()
    with _CACHE_LOCK:
        if cache_key in _LLM_CACHE:
            return _LLM_CACHE[cache_key]

    if not llm_api_key or llm_api_key == "your_api_key_here":
        raise RuntimeError(
            "未命中本地缓存且未配置有效的 LLM_API_KEY。请在 .env 文件或环境变量中设置 LLM_API_KEY。"
        )

    url = f"{llm_base_url}/chat/completions"
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_api_key}",
        "User-Agent": "DailyAIInsightEngine/2.0",
    }

    for attempt in range(5):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=75) as resp:
                resp_body = resp.read().decode("utf-8")
                resp_json = json.loads(resp_body)
                content = resp_json["choices"][0]["message"]["content"]
                parsed = robust_parse_json(content)
                with _CACHE_LOCK:
                    _LLM_CACHE[cache_key] = parsed
                save_cache()
                return parsed
        except Exception as e:
            print(f"    [LLM Retry {attempt+1}/5] {e}")
            payload["temperature"] = min(0.4, 0.1 * (attempt + 1))
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("LLM API 调用在 5 次重试后仍失败。")


def call_llm_markdown(system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
    """调用大模型生成结构化分析日报 Markdown 文本，并写入本地 SHA-256 确定性缓存"""
    llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.longcat.chat/openai/v1").rstrip("/")
    llm_model = os.environ.get("LLM_MODEL", "LongCat-2.5-Preview")
    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()

    cache_key = hashlib.sha256(f"MD||{llm_model}||{system_prompt}||{user_prompt}".encode("utf-8")).hexdigest()
    with _CACHE_LOCK:
        if cache_key in _LLM_CACHE:
            cached_val = _LLM_CACHE[cache_key]
            if isinstance(cached_val, dict) and "markdown" in cached_val:
                return cached_val["markdown"]
            if isinstance(cached_val, str):
                return cached_val

    if not llm_api_key or llm_api_key == "your_api_key_here":
        raise RuntimeError(
            "未命中本地缓存且未配置有效的 LLM_API_KEY。请在 .env 文件或环境变量中设置 LLM_API_KEY。"
        )

    url = f"{llm_base_url}/chat/completions"
    payload = {
        "model": llm_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "stream": True,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_api_key}",
        "User-Agent": "DailyAIInsightEngine/2.0",
    }

    for attempt in range(5):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            chunks = []
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_json = json.loads(data_str)
                        delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            chunks.append(piece)
                    except Exception:
                        continue
            content = "".join(chunks).strip()
            if not content:
                raise ValueError("Empty streaming response content")
            if content.startswith("```markdown"):
                content = content[len("```markdown") :].strip()
                if content.endswith("```"):
                    content = content[:-3].strip()
            elif content.startswith("```"):
                lines = content.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines).strip()
            with _CACHE_LOCK:
                _LLM_CACHE[cache_key] = {"markdown": content}
            save_cache()
            return content
        except Exception as e:
            print(f"    [LLM Markdown Retry {attempt+1}/5] {e}")
            payload["temperature"] = min(0.4, 0.1 * (attempt + 1))
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("LLM Markdown 生成调用在 5 次重试后仍失败。")
