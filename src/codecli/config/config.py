import json
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()

@dataclass(slots=True)
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url: str = ""
    context_window: int = 128000
    prices: dict = field(default_factory=dict)
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0

@dataclass(slots=True)
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    timeout: float = 60.0
    batch_timeout: float = 90.0
    max_concurrent_read: int = 4

@dataclass(slots=True)
class McpConfig:
    servers: list[dict[str, Any]] = field(default_factory=list)
    auto_start: bool = True

@dataclass(slots=True)
class AutoMemoryConfig:
    """Claude Code style Markdown auto memory (MEMORY.md index + topic files).

    Aligned with the leaked Claude Code v2.1.88 memory implementation:
    sanitized-git-root project dirs, two-step writes (topic file, then index
    line), 200-line / 25KB index with <=150 chars per line, LLM side-call
    selection of up to ``recall_max_files`` topic files per user turn.
    """

    enabled: bool = True
    root: str = "~/.codecli/memory"
    max_index_lines: int = 200
    max_index_bytes: int = 25 * 1024
    max_index_line_chars: int = 150
    sidecall_enabled: bool = True
    sidecall_model: str = ""  # empty = reuse the main LLM client
    recall_max_files: int = 5
    write_after_error: bool = True  # v2.1.214+ behavior: surface over-limit to the model
    # Post-session extractor (Auto Dream): a forked agent with restricted
    # tools consolidates memories after the session ends. Triggered when the
    # last run is >= extract_min_interval_hours ago AND at least
    # extract_min_sessions sessions have passed (both gates, like Claude).
    extractor_enabled: bool = True
    extract_min_sessions: int = 5
    extract_min_interval_hours: float = 24.0
    extract_max_turns: int = 5
    extract_message_window: int = 40


@dataclass(slots=True)
class MemoryConfig:
    max_conversation_history: int = 100
    token_budget_mode: str = "balanced"
    compression_threshold: float = 0.8
    compression_target: float = 0.55
    compression_reserve_tokens: int = 1_024
    min_recent_messages: int = 6
    summary_max_chars: int = 6_000

@dataclass(slots=True)
class PolicyConfig:
    hitl_mode: str = "auto"
    path_guard_enabled: bool = True
    command_guard_enabled: bool = True
    command_blacklist: list[str] = field(
        default_factory=lambda: [
            "sudo",
            "rm -rf /",
            "rm -rf ~",
            "mkfs",
            "dd if=/dev/zero",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "curl | sh",
            "curl|sh",
            "shutdown",
            "reboot",
        ]
    )
    audit_log_path: str = "~/.codecli/audit.jsonl"

@dataclass(slots=True)
class PromptConfig:
    personality: str = "default"
    agent_mode: str = "react"
    custom_prompt_paths: list[str] = field(default_factory=list)

@dataclass(slots=True)
class FeatureConfig:
    mcp: bool = True
    skill: bool = True
    memory: bool = True
    audit_log: bool = True
    context_compression: bool = True
    code_index: bool = True

@dataclass(slots=True)
class CodeCliConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    render_mode: str = "inline"
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    auto_memory: AutoMemoryConfig = field(default_factory=AutoMemoryConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)


def config_to_public_dict(config: CodeCliConfig) -> dict[str, Any]:
    data = _config_to_dict(config)
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


def load_config(
    project_root: str | Path | None = None,
    configuration: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None
) -> CodeCliConfig:
    env_map = env if env is not None else os.environ
    config = _config_to_dict(CodeCliConfig())

    user_config = _read_json(_home() / ".codecli" / "config.json")
    if user_config:
        config = _deep_merge(config, user_config)

    root = Path(project_root).resolve() if project_root else None
    if root:
        project_config = _read_json(root / ".codecli" / "config.json")
        if project_config:
            config = _deep_merge(config, project_config)
        project_env = _read_env(root / ".env")
        if project_env:
            config = _deep_merge(config, project_env)

    if configuration:
        config = _deep_merge(config, configuration)

    config = _apply_env(config, env_map)
    result_config = _dict_to_config(config)
    result_config.auto_memory.root = _expand_home(result_config.auto_memory.root)
    result_config.policy.audit_log_path = _expand_home(result_config.policy.audit_log_path)
    return result_config

def _config_to_dict(config: CodeCliConfig) -> dict[str, Any]:
    return asdict(config)

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None

def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in source.items():
        if value is None:
            continue
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _deep_merge(old, value)
        else:
            result[key] = deepcopy(value)
    return result

def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result

    for raw_lint in lines:
        line = raw_lint.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        result[key] = value

    return result

def _apply_env(config: dict[str, Any], env: dict[str, str | None]) -> dict[str, Any]:
    result = deepcopy(config)
    llm = result.setdefault("llm", {})
    features = result.setdefault("features", {})
    policy = result.setdefault("policy", {})

    mappings: list[tuple[str, str, Any]] = [
        ("CODECLI_API_KEY", "api_key", str),
        ("CODECLI_PROVIDER", "provider", str),
        ("CODECLI_MODEL", "model", str),
        ("CODECLI_BASE_URL", "base_url", str),
        ("CODECLI_CONTEXT_WINDOW", "context_window", int),
        ("CODECLI_MAX_TOKENS", "max_tokens", int),
        ("CODECLI_TEMPERATURE", "temperature", float),
    ]

    for env_key, config_key, caster in mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                llm[config_key] = caster(raw)

    provider = str(llm.get("provider") or "").lower()
    if not llm.get("api_key"):
        provider_key_map = {
            "deepseek": ("DEEPSEEK_API_KEY",),
            "glm": ("ZAI_API_KEY", "GLM_API_KEY"),
            "zhipu": ("ZAI_API_KEY", "GLM_API_KEY"),
            "step": ("STEP_API_KEY",),
            "kimi": ("KIMI_API_KEY",),
            "moonshot": ("KIMI_API_KEY",),
            "freellmapi": ("FREELLMAPI_API_KEY",),
            "xfyun": ("XFYUN_API_KEY",),
            "agnes": ("AGNES_API_KEY",),
        }
        for provider_key in provider_key_map.get(provider, ()):
            if env.get(provider_key):
                llm["api_key"] = env[provider_key]
                break

    provider_model = f"{provider.upper()}_MODEL" if provider else ""
    provider_base_url = f"{provider.upper()}_BASE_URL" if provider else ""
    if provider_model and env.get(provider_model):
        llm["model"] = env[provider_model]
    if provider_base_url and env.get(provider_base_url):
        llm["base_url"] = env[provider_base_url]

    for env_key, feature_key in [
        ("CODECLI_MCP", "mcp"),
        ("CODECLI_SKILL", "skill"),
        ("CODECLI_MEMORY", "memory")
    ]:
        raw = env.get(env_key)
        if raw == "false":
            features[feature_key] = False
        elif raw == "true":
            features[feature_key] = True

    auto_memory = result.setdefault("auto_memory", {})
    for env_key, config_key in [
        ("CODECLI_AUTO_MEMORY", "enabled"),
        ("CODECLI_AUTO_MEMORY_SIDECALL", "sidecall_enabled"),
        ("CODECLI_AUTO_MEMORY_WRITE_ERROR", "write_after_error"),
        ("CODECLI_AUTO_MEMORY_EXTRACTOR", "extractor_enabled"),
    ]:
        raw = env.get(env_key)
        if raw == "false":
            auto_memory[config_key] = False
        elif raw == "true":
            auto_memory[config_key] = True

    return result

def _dict_to_config(data: dict[str, Any]) -> CodeCliConfig:
    return CodeCliConfig(
        llm = LlmConfig(**data.get("llm", {})),
        tools=ToolsConfig(**data.get("tools", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        auto_memory=AutoMemoryConfig(**data.get("auto_memory", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        features=FeatureConfig(**data.get("features", {}))
    )

def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())