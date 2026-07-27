"""Direct API providers, used when you want to bypass kiro-cli."""

import logging

import httpx

log = logging.getLogger(__name__)

HISTORY_LIMIT = 40

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "key": "deepseek_api_key",
        "models": ("deepseek-v4-flash", "deepseek-v4-pro"),
        "thinking": True,
    },
}


class ProviderError(Exception):
    pass


class Session:
    """One conversation against a provider. Keeps its own history."""

    def __init__(self, name, api_key, model=None, system=None, max_length=4000):
        try:
            self.spec = PROVIDERS[name]
        except KeyError:
            raise ProviderError("unknown provider: %s" % name)

        if not api_key:
            raise ProviderError("no api key set for %s" % name)

        self.name = name
        self.api_key = api_key
        self.model = model or self.spec["models"][0]
        self.thinking = False
        self.effort = "medium"
        self.history = []
        self.system = system
        self.max_length = max_length

    @property
    def label(self):
        return self.spec["label"]

    @property
    def models(self):
        return self.spec["models"]

    @property
    def supports_thinking(self):
        return bool(self.spec.get("thinking"))

    def reset(self):
        self.history.clear()

    def set_model(self, model):
        if model not in self.models:
            raise ProviderError("available models: %s" % ", ".join(self.models))
        self.model = model

    async def ask(self, prompt):
        messages = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages += self.history
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        if self.supports_thinking and self.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.effort

        url = self.spec["base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }

        log.info("%s -> %s: %s", self.name, self.model, prompt[:60])

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                res = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ProviderError("network error: %s" % exc)

        if res.status_code != 200:
            raise ProviderError(_error_text(res))

        try:
            reply = res.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            raise ProviderError("unexpected response: %s" % res.text[:200])

        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": reply})
        self._trim()

        text = (reply or "").strip() or "Empty response"
        if len(text) > self.max_length:
            text = text[: self.max_length] + "\n\n[truncated]"
        return text

    def _trim(self):
        """Drop oldest turns, always in whole user/assistant pairs.

        Cutting mid-pair would leave the history starting with an assistant
        message, which providers can reject.
        """
        excess = len(self.history) - HISTORY_LIMIT
        if excess > 0:
            del self.history[: excess + (excess % 2)]


def _error_text(res):
    try:
        body = res.json()
        detail = body.get("error", {}).get("message") or body.get("message")
    except ValueError:
        detail = res.text[:200]

    if res.status_code == 401:
        return "401 unauthorized, check the api key"
    if res.status_code == 402:
        return "402 out of credit"
    if res.status_code == 429:
        return "429 rate limited, slow down"
    return "http %s: %s" % (res.status_code, detail or "no detail")
