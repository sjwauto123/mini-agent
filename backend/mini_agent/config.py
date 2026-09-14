"""配置加载。

配置来源与优先级：``models.toml``（可被 ``MINI_AGENT_CONFIG`` 覆盖）提供模型与运行时参数，
``.env``（可被 ``MINI_AGENT_ENV_FILE`` 覆盖）只提供密钥等环境变量。密钥不写进 toml，
而是通过 ``api_key_env`` 间接引用环境变量名，避免误提交。

加载时会把明显不合理的配置直接拒绝（见文件末尾的校验），让问题在启动时就暴露，
而不是等到某次请求才报错。
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# 仓库根目录：配置文件与数据目录的默认基准。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelConfig:
    """单个模型的配置。``mode`` 决定用哪套工具调用协议：

    - ``native``：原生 function calling（推荐，本项目默认）；
    - ``json``：要求模型输出固定 JSON。

    注意：思考型模型在"带工具清单 + 工具调用形态历史"的上下文里与 ``json`` 模式不兼容，
    会只返回空白正文，详见 docs/qa-report.md。
    """
    name: str
    endpoint: str
    model: str
    api_key_env: str
    mode: str
    context_window: int
    output_reserve: int

    @property
    def api_key(self) -> str:
        # 只在真正发起请求时读取环境变量，测试里可以随时替换而不必重建配置对象。
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class AppConfig:
    """应用级配置：数据目录、可用模型，以及运行时的各种预算与上限。"""

    data_dir: Path
    models: dict[str, ModelConfig]
    # 预留余量：估算 token 时多扣一点，避免把上下文顶到上限导致请求被拒。
    safety_margin: int = 1024
    # 单次运行最多调用多少次模型（防止模型在工具循环里停不下来）。
    max_model_calls: int = 12
    # 单次运行最多触发多少次上下文压缩。
    max_summary_calls: int = 2
    # 模型响应不符合协议时，最多要求它重答几次。
    max_protocol_repairs: int = 2
    model_timeout: float = 60
    tool_timeout: float = 15
    run_timeout: float = 180
    # 上下文水位：超过 soft 触发压缩；硬上限是 hard；压缩后回落到 target。
    # 三者必须满足 0 < target < soft < hard < 1（下方校验）。
    soft_context_ratio: float = .70
    hard_context_ratio: float = .85
    target_context_ratio: float = .55


def load_config(path: Path | None = None) -> AppConfig:
    """读取配置并做一致性校验。

    ``override=False`` 表示进程里已存在的环境变量优先于 .env，便于部署时用真实环境变量覆盖。
    """
    env_path = Path(os.environ.get("MINI_AGENT_ENV_FILE", PROJECT_ROOT / ".env"))
    load_dotenv(env_path, override=False)
    path = path or Path(os.environ.get("MINI_AGENT_CONFIG", "models.toml"))
    # 配置文件缺失时不报错，用空字典走全默认值，方便首次克隆后直接跑测试。
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
    # 水位顺序错了会让压缩逻辑无从判断，宁可启动即失败。
    if not 0 < config.target_context_ratio < config.soft_context_ratio < config.hard_context_ratio < 1:
        raise ValueError("context ratios must satisfy 0 < target < soft < hard < 1")
    if min(config.max_model_calls, config.max_summary_calls, config.max_protocol_repairs + 1) <= 0:
        raise ValueError("runtime call limits must be positive")
    if min(config.model_timeout, config.tool_timeout, config.run_timeout) <= 0:
        raise ValueError("runtime timeouts must be positive")
    for model in models.values():
        # 可用输入预算 = 上下文窗口 - 输出预留 - 安全余量，必须为正，否则任何请求都必然超限。
        if model.context_window - model.output_reserve - config.safety_margin <= 0:
            raise ValueError(f"model {model.name}: context budget must be positive")
    return config
