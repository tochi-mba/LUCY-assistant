# Models and providers

Lucy can think with any of forty-three providers. Most of them speak the same wire format,
and the three that do not are the ones everybody has heard of. What a person actually needs
to know is which of them work *right now*, and what the rest would need -- so that is what
the hub reports, and everything else here is the reasoning behind that report.

## One string names a model

A model is `provider:model` -- `anthropic:claude-opus-5-5`, `deepseek:deepseek-v4-pro`,
`ollama:llama3.3`. The left half is a row in the catalogue; the right half is passed to the
provider exactly as typed. A session records the string it was answered by, a setting
(`lucy.model`) names a default, and a request may override it. Keeping one spelling means
the provider is a lookup rather than a branch, and it means a conversation answered by one
model is never silently answered by another after a configuration change.

## Three dialects cover the world

| Dialect | Who speaks it | Adapter |
| --- | --- | --- |
| Messages (`POST /v1/messages`) | Anthropic | `model/anthropic.py` |
| Responses (`POST /v1/responses`) | OpenAI, xAI | `model/openai.py` |
| Chat completions (`POST /chat/completions`) | everybody else: Google, Mistral, Groq, DeepSeek, Moonshot, Qwen, every local runtime | `model/chat.py` |

A provider is a row in `model/catalogue.py`: its id, its base URL spelled the way it
documents it, how the key travels, where a key comes from, a few current model ids, where a
listing can be fetched to prove a key, and the deviations that change what Lucy sends.
Adding a provider is adding a row. Nothing else names one.

Two things are recorded rather than hidden. Google's native API is not offered, because
Google also serves the chat-completions dialect and one adapter is better than two. Vertex
AI needs an OAuth access token this hub does not mint, so it is listed as something a person
can see and cannot use -- with the reason.

Region variants are separate rows (`moonshot` and `moonshot-cn`, `dashscope` and
`dashscope-cn`). A mainland-China endpoint is a different host with a different key, and
folding the two into one row with a switch is how somebody sends a key to the wrong country.

### Deviations the catalogue declares

| Trait | Effect | Who |
| --- | --- | --- |
| no tool calling | usable for answers, not for plans | Perplexity |
| JSON object only | a plan's schema is put in words in the prompt rather than enforced by the provider | DeepSeek, Moonshot, iFlytek Spark, Ollama |
| no reasoning effort | Lucy does not send a thinking depth | Cohere |
| needs a base URL | there is no public endpoint to default to | Azure OpenAI, Amazon Bedrock |

A plan through a JSON-only provider is a weaker guarantee than a schema the provider
enforces. The loop validates every plan regardless; what it costs is a repair round now and
then, which is cheaper than losing the provider.

## What is configured

```
LUCY_MODEL_KEYS='{"anthropic": "sk-ant-...", "deepseek": "sk-...", "ollama": "local"}'
LUCY_MODEL_BASE_URLS='{"azure-openai": "https://tenant.openai.azure.com/openai/v1"}'
```

`LUCY_MODEL_KEYS` is one key per provider id. The older `LUCY_ANTHROPIC_API_KEY` and
`LUCY_OPENAI_API_KEY` still work and are folded in. A local runtime has no key; naming it
with any value, or giving it a base URL, is what switches it on. `LUCY_MODEL_BASE_URLS`
overrides a row's endpoint -- a cloud tenant, a runtime on another port, a proxy -- and is
required for the rows that have no public endpoint.

A key for a provider the hub has no row for is a startup error, not a warning. It is a typo
in configuration, and a deployment that silently dropped it would fail later, at the first
turn, in a place that does not mention the setting.

## What is ready

`GET /v1/models` sorts every provider into exactly one of three sections:

| Section | Meaning |
| --- | --- |
| `ready` | configured, and a listing call answered. Usable this turn. |
| `available` | configured, but not proven: the provider documents no listing endpoint, or the check has not run yet. |
| `unavailable` | not configured, refused, or unusable -- each with the sentence that fixes it and the command to run. |

Proving a key means one `GET` to the provider's models endpoint, with a short timeout,
remembered for a minute. It happens only when asked (`?check=true`, or `lucy models
--check`) and never on a turn's path: a turn resolves the provider it was told to use and
finds out the ordinary way. A local runtime is probed at its own health endpoint instead,
because "is it running" is the whole question for something on this machine.

Every `unavailable` row carries a `setup` block:

```json
{
  "provider": "deepseek",
  "section": "unavailable",
  "detail": "no key configured",
  "setup": {
    "command": "lucy models connect deepseek",
    "console_url": "https://platform.deepseek.com",
    "setting": "LUCY_MODEL_KEYS",
    "instructions": "Create a key at https://platform.deepseek.com and supply it."
  }
}
```

## From the command line

```
lucy models                 three sections, one line per provider
lucy models --check         prove the configured keys now
lucy models --json          the same report, for a script
lucy models connect ollama  switch a local runtime on
lucy models connect groq    prompt for a key and save it
```

`connect` writes the key into the `.env` at the family checkout, which is what a hub started
with `lucy serve` or `make up` reads, and tells you to restart it. The key is typed at a
prompt, never passed as a flag: a flag lands in shell history and in `ps`. For a hub on
another machine the command refuses and names the variable to set there.

## What is not here yet

Keys are a fact about the deployment, not about a person. A per-person key -- your own
DeepSeek account rather than the operator's -- would live in the identity vault beside a
Spotify connection and be exchanged for on each turn. The catalogue and the readiness
report are built so that can be added without changing their shape; the vault side is not
built.

## Where this lives

| | |
| --- | --- |
| `model/catalogue.py` | every provider, as data |
| `model/chat.py` | the chat-completions adapter |
| `model/registry.py` | `provider:model` → an adapter, built once and kept |
| `model/readiness.py` | the three sections, and the proof behind each |
| `api/routers/models.py` | `GET /v1/models`, `GET /v1/models/{provider}` |
| `cli/models.py` | `lucy models`, `lucy models connect` |
