import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelConfig:
    name: str
    endpoint: str
    model: str
    api_key_env: str
    mode: str
    context_window: int
    output_reserve: int

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    models: dict[str, ModelConfig]
    safety_margin: int = 1024
    max_model_calls: int = 12
    max_summary_calls: int = 2
    max_protocol_repairs: int = 2
    model_timeout: float = 60
    tool_timeout: float = 15
    run_timeout: float = 180
    soft_context_ratio: float = .70
    hard_context_ratio: float = .85
    target_context_ratio: float = .55


def load_config(path: Path | None = None) -> AppConfig:
    env_path = Path(os.environ.get("MINI_AGENT_ENV_FILE", PROJECT_ROOT / ".env"))
    load_dotenv(env_path, override=False)
    path = path or Path(os.environ.get("MINI_AGENT_CONFIG", "models.toml"))
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data_dir = Path(os.environ.get("MINI_AGENT_DATA_DIR", raw.get("app", {}).get("data_dir", ".mini-agent"))).resolve()
    models: dict[str, ModelConfig] = {}
    for name, value in raw.get("models", {}).items():
        mode = value.get("mode", "native")
        if mode not in {"native", "json"}:
            raise ValueError(f"model {name}: mode must be native or json")
        models[name] = ModelConfig(name=name, endpoint=value["endpoint"], model=value["model"], api_key_env=value["api_key_env"], mode=mode, context_window=int(value["context_window"]), output_reserve=int(value.get("output_reserve", 2048)))
    runtime = raw.get("runtime", {})
    config = AppConfig(
        data_dir=data_dir,
        models=models,
        safety_margin=int(runtime.get("safety_margin", 1024)),
        max_model_calls=int(runtime.get("max_model_calls", 12)),
        max_summary_calls=int(runtime.get("max_summary_calls", 2)),
        max_protocol_repairs=int(runtime.get("max_protocol_repairs", 2)),
        model_timeout=float(runtime.get("model_timeout", 60)),
        tool_timeout=float(runtime.get("tool_timeout", 15)),
        run_timeout=float(runtime.get("run_timeout", 180)),
        soft_context_ratio=float(runtime.get("soft_context_ratio", .70)),
        hard_context_ratio=float(runtime.get("hard_context_ratio", .85)),
        target_context_ratio=float(runtime.get("target_context_ratio", .55)),
    )
    if not 0 < config.target_context_ratio < config.soft_context_ratio < config.hard_context_ratio < 1:
        raise ValueError("context ratios must satisfy 0 < target < soft < hard < 1")
    if min(config.max_model_calls, config.max_summary_calls, config.max_protocol_repairs + 1) <= 0:
        raise ValueError("runtime call limits must be positive")
    if min(config.model_timeout, config.tool_timeout, config.run_timeout) <= 0:
        raise ValueError("runtime timeouts must be positive")
    for model in models.values():
        if model.context_window - model.output_reserve - config.safety_margin <= 0:
            raise ValueError(f"model {model.name}: context budget must be positive")
    return config
