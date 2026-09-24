"""Every model provider this hub knows how to talk to, whether or not one is set up.

The point of a catalogue rather than a pair of hard-coded adapters is the sentence a person
reads when a model is *not* ready: which provider it is, what it needs, where the key comes
from, and the one command that sets it up. A hub that only lists what works cannot say any
of that, and "unknown provider" is the wrong answer to somebody who typed a real one.

## Three dialects cover the world

Almost every provider speaks one of three wire formats. Anthropic speaks Messages. OpenAI
and xAI speak Responses. Everybody else -- the Chinese labs, the inference hosts, every
local runtime -- speaks the chat-completions format OpenAI shipped first and never took
back. So a provider row names its dialect and its base URL, and three adapters do the rest;
adding a provider is adding a row, not a module.

The two exceptions are recorded rather than hidden. Google's native API is not offered,
because Google also serves the chat-completions dialect and one adapter is better than two.
Vertex AI needs an OAuth access token rather than a key, which this hub does not mint yet,
so it is listed as something a person can see and cannot use -- with the reason.

## What a row promises

`base_url` is the prefix the dialect's path is appended to, spelled the way the provider
documents it, `/v1` and all. `auth` is the header shape. `models` are current flagship ids
spelled as the API wants them, for the setup card and the first-run pick; they are not a
claim that nothing else works. `models_path` is where a listing can be fetched to prove a
key is live, and `None` where the provider documents no such thing. Traits are the
deviations that change what Lucy sends: no tool calling, JSON-object rather than
JSON-schema output, and so on.

Region variants are separate rows. A mainland-China endpoint is a different host with a
different key, and folding the two into one row with a switch is how somebody ends up
sending a key to the wrong country.

Facts here were checked against provider documentation on 2026-09-22. Where a fact could
not be confirmed it says so in `note`, and the row is still offered: an unconfirmed
model-list path costs one failed probe, and leaving a provider out costs a person the
provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Dialect(StrEnum):
    """Which wire format an adapter speaks."""

    anthropic_messages = "anthropic-messages"
    openai_responses = "openai-responses"
    openai_chat = "openai-chat"


class Auth(StrEnum):
    """How a credential travels.

    `oauth` means an access token this hub cannot mint; such a provider is catalogued so
    a person can see it and is never registered.
    """

    bearer = "bearer"
    x_api_key = "x-api-key"
    api_key_header = "api-key"
    none = "none"
    oauth = "oauth"


@dataclass(frozen=True, slots=True)
class Traits:
    """The deviations from a dialect that change what Lucy sends."""

    tools: bool = True
    """Whether function calling exists at all. Perplexity, for one, has none."""

    json_schema: bool = True
    """Whether structured output can be asked for as a schema, not merely as JSON."""

    reasoning_effort: bool = True
    """Whether `reasoning_effort` is understood rather than refused."""


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One provider, as the catalogue and the setup card describe it."""

    id: str
    title: str
    base_url: str
    console_url: str = ""
    models: tuple[str, ...] = ()
    dialect: Dialect = Dialect.openai_chat
    auth: Auth = Auth.bearer
    models_path: str | None = "/models"
    traits: Traits = field(default_factory=Traits)
    local: bool = False
    probe_url: str = ""
    needs_base_url: bool = False
    note: str = ""

    @property
    def needs_key(self) -> bool:
        return self.auth in {Auth.bearer, Auth.x_api_key, Auth.api_key_header}

    @property
    def usable(self) -> bool:
        """Whether this hub has an adapter and an auth shape for it at all."""
        return self.auth is not Auth.oauth


def _local(
    provider: str, title: str, base_url: str, probe_url: str, *, note: str = ""
) -> ProviderSpec:
    """A runtime on this machine: no key, a default port, and a probe that says it is up."""
    return ProviderSpec(
        provider, title, base_url, auth=Auth.none, local=True, probe_url=probe_url, note=note
    )


NO_TOOLS = Traits(tools=False)
JSON_OBJECT_ONLY = Traits(json_schema=False)
NO_EFFORT = Traits(reasoning_effort=False)

CLYDE_TRAITS = Traits(tools=False, reasoning_effort=False)
"""What clyde accepts: a JSON-schema answer, and neither function calling nor effort.

clyde puts Claude Code's own `--json-schema` behind the chat-completions dialect and ignores
`tools` and `reasoning_effort` -- the model it runs has every one of its own tools removed,
so there is nothing for a function call to reach. Lucy's plans travel as schema output, which
is the one structured shape clyde does honour.
"""

CATALOGUE: tuple[ProviderSpec, ...] = (
    # ------------------------------------------------------------- the two native dialects
    ProviderSpec(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com",
        "https://platform.claude.com",
        ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
        dialect=Dialect.anthropic_messages,
        auth=Auth.x_api_key,
        models_path="/v1/models",
    ),
    ProviderSpec(
        "openai",
        "OpenAI",
        "https://api.openai.com",
        "https://platform.openai.com/api-keys",
        ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"),
        dialect=Dialect.openai_responses,
        models_path="/v1/models",
    ),
    ProviderSpec(
        "xai",
        "xAI",
        "https://api.x.ai",
        "https://console.x.ai",
        ("grok-4.7", "grok-4.6"),
        dialect=Dialect.openai_responses,
        models_path="/v1/models",
    ),
    # ------------------------------------------------------------- hosted, chat dialect
    ProviderSpec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://aistudio.google.com/apikey",
        ("gemini-3.8-flash", "gemini-3.1-pro-preview"),
        note="Served through Google's OpenAI-compatible endpoint. Unknown fields are ignored "
        "silently rather than refused.",
    ),
    ProviderSpec(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "https://console.mistral.ai",
        ("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"),
    ),
    ProviderSpec(
        "cohere",
        "Cohere",
        "https://api.cohere.ai/compatibility/v1",
        "https://dashboard.cohere.com/api-keys",
        ("command-a-plus-05-2026",),
        models_path=None,
        traits=NO_EFFORT,
        note="Reasoning effort accepts only none or high; Lucy does not send it.",
    ),
    ProviderSpec(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
        ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"),
    ),
    ProviderSpec(
        "together",
        "Together AI",
        "https://api.together.ai/v1",
        "https://api.together.ai/settings/api-keys",
        ("MiniMaxAI/MiniMax-M3", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        note="Model ids are namespaced; OpenAI's flat ids are not found here.",
    ),
    ProviderSpec(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        "https://fireworks.ai/account/api-keys",
        ("accounts/fireworks/models/llama-v3p1-8b-instruct",),
    ),
    ProviderSpec(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/keys",
        ("openai/gpt-5.2", "anthropic/claude-sonnet-4.6"),
        note="One key reaches many providers; the model id names which.",
    ),
    ProviderSpec(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        "https://www.perplexity.ai/settings/api",
        ("sonar-pro", "sonar", "sonar-reasoning-pro"),
        models_path=None,
        traits=NO_TOOLS,
        note="No function calling: usable for answers, not for plans.",
    ),
    ProviderSpec(
        "deepinfra",
        "DeepInfra",
        "https://api.deepinfra.com/v1/openai",
        "https://deepinfra.com/dash/api_keys",
        ("deepseek-ai/DeepSeek-V4-Flash-0731",),
    ),
    ProviderSpec(
        "cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        "https://cloud.cerebras.ai",
        ("qwen-3.8-27b", "gpt-oss-120b"),
    ),
    ProviderSpec(
        "nebius",
        "Nebius Token Factory",
        "https://api.tokenfactory.nebius.com/v1",
        "https://tokenfactory.nebius.com",
        ("deepseek-ai/DeepSeek-R1-0528",),
    ),
    ProviderSpec(
        "hyperbolic",
        "Hyperbolic",
        "https://api.hyperbolic.xyz/v1",
        "https://app.hyperbolic.ai",
        (),
        note="Endpoint unconfirmed on 2026-09-22; the documentation had moved.",
    ),
    # ------------------------------------------------------------- cloud variants
    ProviderSpec(
        "azure-openai",
        "Azure OpenAI",
        "",
        "https://portal.azure.com",
        (),
        auth=Auth.api_key_header,
        needs_base_url=True,
        note="Set the base URL to https://<resource>.openai.azure.com/openai/v1. The model "
        "id is the deployment name.",
    ),
    ProviderSpec(
        "bedrock",
        "Amazon Bedrock",
        "",
        "https://console.aws.amazon.com/bedrock",
        ("openai.gpt-oss-120b-1:0", "anthropic.claude-opus-5-5"),
        models_path=None,
        needs_base_url=True,
        note="Set the base URL to https://bedrock-runtime.<region>.amazonaws.com/openai/v1 "
        "and the key to a Bedrock API key.",
    ),
    ProviderSpec(
        "vertex",
        "Google Vertex AI",
        "https://aiplatform.googleapis.com/v1",
        "https://console.cloud.google.com/vertex-ai",
        (),
        auth=Auth.oauth,
        models_path=None,
        note="Needs an OAuth access token rather than a key, which this hub does not mint "
        "yet. Use Gemini through Google AI Studio instead.",
    ),
    # ------------------------------------------------------------- Chinese providers
    ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        "https://platform.deepseek.com",
        ("deepseek-v4-pro", "deepseek-flash"),
        traits=JSON_OBJECT_ONLY,
    ),
    ProviderSpec(
        "moonshot",
        "Moonshot (Kimi)",
        "https://api.moonshot.ai/v1",
        "https://platform.kimi.ai/console/api-keys",
        ("kimi-k3", "kimi-k2.6"),
        traits=JSON_OBJECT_ONLY,
    ),
    ProviderSpec(
        "moonshot-cn",
        "Moonshot (Kimi), mainland China",
        "https://api.moonshot.cn/v1",
        "https://platform.moonshot.cn/console/api-keys",
        ("kimi-k3", "kimi-k2.6"),
        traits=JSON_OBJECT_ONLY,
    ),
    ProviderSpec(
        "zai",
        "Z.ai (GLM)",
        "https://api.z.ai/api/paas/v4",
        "https://z.ai/manage-apikey/apikey-list",
        ("glm-5.3", "glm-5.3-flash"),
        models_path=None,
    ),
    ProviderSpec(
        "zhipu-cn",
        "Zhipu (GLM), mainland China",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn",
        ("glm-5.3", "glm-5.3-flash"),
        models_path=None,
    ),
    ProviderSpec(
        "dashscope",
        "Alibaba Model Studio (Qwen)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://modelstudio.console.alibabacloud.com",
        ("qwen3.8-max", "qwen-plus"),
        note="Keys are bound to a region; a mainland key does not work here.",
    ),
    ProviderSpec(
        "dashscope-cn",
        "Alibaba Bailian (Qwen), mainland China",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://bailian.console.aliyun.com",
        ("qwen3.8-max", "qwen-plus"),
    ),
    ProviderSpec(
        "minimax",
        "MiniMax",
        "https://api.minimax.io/v1",
        "https://platform.minimax.io",
        ("MiniMax-M3", "MiniMax-M2.7"),
        models_path=None,
    ),
    ProviderSpec(
        "minimax-cn",
        "MiniMax, mainland China",
        "https://api.minimaxi.com/v1",
        "https://platform.minimaxi.com",
        ("MiniMax-M3", "MiniMax-M2.7"),
        models_path=None,
    ),
    ProviderSpec(
        "baichuan",
        "Baichuan",
        "https://api.baichuan-ai.com/v1",
        "https://platform.baichuan-ai.com",
        ("Baichuan4", "Baichuan4-Air"),
        models_path=None,
    ),
    ProviderSpec(
        "stepfun",
        "StepFun",
        "https://api.stepfun.com/v1",
        "https://platform.stepfun.com",
        ("step-3.5-flash",),
    ),
    ProviderSpec(
        "hunyuan",
        "Tencent Hunyuan",
        "https://api.hunyuan.cloud.tencent.com/v1",
        "https://console.cloud.tencent.com/hunyuan",
        ("hunyuan-turbos-latest", "hunyuan-lite"),
        models_path=None,
    ),
    ProviderSpec(
        "qianfan",
        "Baidu Qianfan (ERNIE)",
        "https://qianfan.baidubce.com/v2",
        "https://console.bce.baidu.com/iam/#/iam/apikey",
        (),
        note="Model ids unconfirmed on 2026-09-22; the listing endpoint names them.",
    ),
    ProviderSpec(
        "volcengine",
        "Volcengine Ark (Doubao)",
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://console.volcengine.com/ark",
        ("doubao-seed-2-1-pro-260628",),
        models_path=None,
    ),
    ProviderSpec(
        "spark",
        "iFlytek Spark",
        "https://spark-api-open.xf-yun.com/v1",
        "https://console.xfyun.cn",
        ("4.0Ultra", "generalv3.5"),
        models_path=None,
        traits=JSON_OBJECT_ONLY,
        note="The key is the console's API password. Function calling only on the Ultra, "
        "Max and Pro tiers.",
    ),
    ProviderSpec(
        "siliconflow",
        "SiliconFlow",
        "https://api.siliconflow.com/v1",
        "https://cloud.siliconflow.com/account/ak",
        ("Qwen/Qwen3-32B", "deepseek-ai/DeepSeek-V3"),
    ),
    ProviderSpec(
        "siliconflow-cn",
        "SiliconFlow, mainland China",
        "https://api.siliconflow.cn/v1",
        "https://cloud.siliconflow.cn/account/ak",
        ("Qwen/Qwen3-32B", "deepseek-ai/DeepSeek-V3"),
    ),
    # ------------------------------------------------------------- local runtimes
    _local(
        "ollama",
        "Ollama",
        "http://localhost:11434/v1",
        "http://localhost:11434/api/tags",
        note="Tool calling yes, tool choice no; structured output is JSON, not a schema.",
    ),
    _local("lmstudio", "LM Studio", "http://localhost:1234/v1", "http://localhost:1234/v1/models"),
    replace(
        _local(
            "clyde",
            "clyde (Claude Code)",
            "http://localhost:8127/v1",
            "http://localhost:8127/v1/models",
            note=(
                "Claude, through a Claude Code subscription signed in on the machine clyde "
                "runs on. Its models are whatever clyde lists. Claude Code's own tools are "
                "removed."
            ),
        ),
        traits=CLYDE_TRAITS,
    ),
    _local(
        "llamacpp", "llama.cpp server", "http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/health"
    ),
    _local("vllm", "vLLM", "http://localhost:8000/v1", "http://localhost:8000/health"),
    _local("localai", "LocalAI", "http://localhost:8080/v1", "http://localhost:8080/readyz"),
    _local("jan", "Jan", "http://127.0.0.1:1337/v1", "http://127.0.0.1:1337/v1/models"),
    _local(
        "koboldcpp",
        "KoboldCpp",
        "http://localhost:5001/v1",
        "http://localhost:5001/api/extra/version",
    ),
    _local("mlx", "mlx-lm server", "http://localhost:8080/v1", "http://localhost:8080/v1/models"),
)

BY_ID: dict[str, ProviderSpec] = {spec.id: spec for spec in CATALOGUE}


@dataclass(frozen=True, slots=True)
class Binding:
    """A catalogued provider plus what this deployment supplies for it.

    The key and the base URL are the two things a row cannot know: one is a secret and the
    other, for a cloud tenant or a runtime on another port, is a fact about this machine.
    An adapter is built from a binding, never from a bare row.
    """

    spec: ProviderSpec
    api_key: str = ""
    base_url: str = ""

    @property
    def url(self) -> str:
        return self.base_url or self.spec.base_url


KNOWN: tuple[str, ...] = tuple(spec.id for spec in CATALOGUE)
"""Every provider id this hub has a row for, in catalogue order."""


def spec_for(provider: str) -> ProviderSpec | None:
    """The row for one provider id, or nothing. Never a guess at a near miss."""
    return BY_ID.get(provider)


__all__ = [
    "BY_ID",
    "CATALOGUE",
    "KNOWN",
    "Auth",
    "Binding",
    "Dialect",
    "ProviderSpec",
    "Traits",
    "spec_for",
]
