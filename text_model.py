"""The open-weight model the text-only workers use (owner, 2026-09-28: "Lets do 1").

The reply AI, outreach writer and send judge, follow-ups, the posting judge, the
recovery decisions and the answering AI all build a `codex exec` command for the ChatGPT
subscription. When `config["text_model"]` names a model, `command` rewrites that command to the
configured OpenRouter model and `environment` supplies its key; every other setting of the
worker's command is left exactly as it was. Without `text_model` nothing changes, so browser
work and research (which need OpenAI's browser tool and live web search) stay on GPT.

config.json:
    "text_model": {"model": "z-ai/glm-5.3"}
The key is read from `openrouter.key` in the user's Sidedoor home (`user_profile.home()`), or
from `key_file` when set.

`complete` (owner, 2026-10-13) is the direct alternative: one OpenRouter chat-completions call
with a chosen reasoning effort and a bounded max_tokens. Through codex the reasoning effort did
not reach the model, and codex asked for up to 131072 tokens, which OpenRouter refuses with 402
once credits run low. The posting judge and the outreach send judge use it; other workers may
adopt it.
"""

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

import user_profile


KEY_VARIABLE = "OPENROUTER_API_KEY"
BASE_URL = "https://openrouter.ai/api/v1"
SUBSCRIPTION_SETTINGS = {'forced_login_method="chatgpt"', 'model_provider="openai"'}


def settings(config):
    value = (config or {}).get("text_model")
    return value if isinstance(value, dict) and isinstance(value.get("model"), str) and value["model"] else None


def model(config):
    """The model a text worker actually runs, for its recorded provenance."""
    chosen = settings(config)
    return chosen["model"] if chosen else (config or {}).get("model")


def auth(config):
    return "openrouter_api_key" if settings(config) else "existing_chatgpt_subscription"


def command(config, arguments):
    """The worker's own codex command, pointed at the open-weight model when one is configured."""
    chosen = settings(config)
    if not chosen:
        return arguments
    result = []
    for argument in arguments:
        if argument in SUBSCRIPTION_SETTINGS:
            result.pop()  # the "-c" in front of it
            continue
        result.append(argument)
    result[result.index("--model") + 1] = chosen["model"]
    start = result.index("exec") + 1
    result[start:start] = [
        "-c", 'model_providers.openrouter={name="OpenRouter",base_url="%s",env_key="%s",wire_api="responses"}'
        % (chosen.get("base_url") or BASE_URL, KEY_VARIABLE),
        "-c", 'model_provider="openrouter"']
    return result


def key(config):
    chosen = settings(config)
    configured = chosen.get("key_file")
    path = Path(os.path.expanduser(configured)) if configured else user_profile.home() / "openrouter.key"
    return path.read_text(encoding="utf-8").strip()


def environment(config, variables):
    """`variables` plus the OpenRouter key when a text model is configured. Raises OSError if unreadable."""
    if not settings(config):
        return variables
    return dict(variables, **{KEY_VARIABLE: key(config)})


# --- Direct chat-completions call (owner, 2026-10-13: "Yes" to low reasoning for the yes/no judges) ---

ERROR_BODY_LIMIT = 300
CONTENT_LIMIT = 32768


class TextModelError(Exception):
    """A direct call that produced no usable answer. `record()` is safe to save: never the key."""

    status = None

    def record(self):
        return type(self).__name__ + ": " + str(self)


class TextModelHTTPError(TextModelError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__("HTTP %s: %s" % (status, body))


class TextModelResponseError(TextModelError):
    finish_reason = None          # set when the provider returned no content
    content_limit_exceeded = False


def _short(text, secret):
    text = text if isinstance(text, str) else repr(text)
    if secret:
        text = text.replace(secret, "[key]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[key]", text)
    return " ".join(text.split())[:ERROR_BODY_LIMIT]


def _post(url, data, headers, timeout):
    """(status, body bytes). An HTTP error status is returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1 << 16)


def complete(config, prompt, *, system=None, effort="low", max_tokens=4000, timeout=60, transport=None,
             response_format=None, provider=None, api_key=None, content_limit=CONTENT_LIMIT):
    """One chat-completions call to the configured OpenRouter model.

    Returns {"content", "usage": {prompt_tokens, completion_tokens, reasoning_tokens, cost},
    "model", "finish_reason"}. Raises TextModelHTTPError (status and a short, key-free body) for a
    refused call, TextModelResponseError for an unusable answer and TimeoutError past `timeout`
    seconds in all (the answer is then discarded, as a codex run past its timeout is killed).
    `transport(url, data, headers, timeout) -> (status, body)` replaces the network in tests.
    `response_format` (optional) is sent as given, e.g. a strict json_schema: the structured
    output codex's --output-schema asked for (the outreach send judge, 2026-10-13).
    `provider` (optional) is OpenRouter's provider-routing object, sent as given; without it
    `config["text_model"]["provider"]` is sent when set, else nothing (OpenRouter's default).
    `api_key` (optional) is used instead of the saved key (`key(config)`), so a key can be tried
    before it is saved; a refused key is a TextModelHTTPError with its status (e.g. 401).
    `content_limit` (optional) raises the answer's byte limit for one caller (résumé intake).
    """
    chosen = settings(config)
    if not chosen:
        raise ValueError("TEXT_MODEL_NOT_CONFIGURED")
    if timeout <= 0:
        raise TimeoutError("Text model deadline exhausted")
    secret = api_key if api_key is not None else key(config)
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": chosen["model"], "messages": messages, "reasoning": {"effort": effort},
            "max_tokens": max_tokens, "usage": {"include": True}}
    if response_format is not None:
        body["response_format"] = response_format
    routing = provider if provider is not None else chosen.get("provider")
    if isinstance(routing, dict) and routing:
        body["provider"] = routing
    url = (chosen.get("base_url") or BASE_URL).rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + secret, "Content-Type": "application/json",
               "X-Title": "Sidedoor"}
    started = time.monotonic()
    try:
        status, raw = (transport or _post)(url, json.dumps(body).encode(), headers, timeout)
    except TimeoutError:
        raise
    except OSError as error:
        if "timed out" in str(error):
            raise TimeoutError("Text model call timed out") from None
        raise TextModelResponseError("transport " + type(error).__name__ + ": " + _short(str(error), secret)) from None
    if time.monotonic() - started > timeout:
        raise TimeoutError("Text model call exceeded its deadline")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if status != 200:
        raise TextModelHTTPError(status, _short(text, secret))
    try:
        answer = json.loads(text)
    except ValueError:
        raise TextModelResponseError("unparsed response: " + _short(text, secret)) from None
    error = answer.get("error") if isinstance(answer, dict) else None
    choice = (answer.get("choices") or [None])[0] if isinstance(answer, dict) else None
    error = error or (choice.get("error") if isinstance(choice, dict) else None)
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        raise TextModelHTTPError(code if code is not None else status, _short(json.dumps(error), secret))
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        error = TextModelResponseError("no content (finish_reason %s)" % (choice or {}).get("finish_reason"))
        error.finish_reason = (choice or {}).get("finish_reason")
        raise error
    if len(content.encode()) > content_limit:
        error = TextModelResponseError("content exceeds %d bytes" % content_limit)
        error.content_limit_exceeded = True
        raise error
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    return {"content": content, "model": answer.get("model"), "finish_reason": choice.get("finish_reason"),
            "usage": {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                      "reasoning_tokens": details.get("reasoning_tokens"), "cost": usage.get("cost")}}


def json_object(content):
    """The JSON value in a model's answer: the whole text, else a fenced block, else the outermost
    {...}. Raises ValueError when none parses; the caller's own validation decides the rest."""
    text = content.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except ValueError:
            pass
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        return json.loads(text[first:last + 1])
    raise ValueError("No JSON object in the model's answer")
