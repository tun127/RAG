from time import monotonic, sleep

import httpx

from production_rag.domain.errors import DependencyError

from .diagnostics import transport_diagnostic


class SparkError(DependencyError):
    def __init__(self, kind, *, final_answer=None):
        self.kind = kind
        self.final_answer = final_answer
        super().__init__(f"Spark: {kind}")


class SparkChat:
    def __init__(self, settings, *, transport=None, clock=monotonic, sleeper=sleep):
        self.settings = settings
        self.model = settings.model
        self.transport = transport
        self.clock = clock
        self.sleep = sleeper
        self.attempts = []

    def chat(self, messages, budget, *, tools=None, schema=None):
        self.attempts = []
        if tools is not None or schema is not None:
            raise SparkError("unsupported_tools_or_schema")
        if not messages or any(
            set(m) != {"role", "content"}
            or m["role"] not in {"system", "user", "assistant"}
            or not isinstance(m["content"], str) for m in messages
        ):
            raise SparkError("unsupported_messages")
        deadline = self.clock() + min(self.settings.timeout, budget.remaining_seconds())

        def remaining():
            value = min(deadline - self.clock(), budget.remaining_seconds())
            if value <= 0:
                raise SparkError("deadline")
            return value

        payload = dict(model=self.model, messages=messages, stream=False,
                       thinking={"type": self.settings.thinking})
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False) as client:
            for attempt in range(self.settings.retries + 1):
                cap = min(self.settings.max_completion_tokens, budget.allowance())
                if self.settings.thinking != "disabled" and cap < self.settings.max_completion_tokens:
                    raise SparkError("insufficient_reasoning_budget")
                payload["max_completion_tokens"] = cap
                timeout = remaining()
                started = self.clock()
                record = dict(attempt=attempt + 1, model=None, request_id=None,
                              usage=None, usage_known=False, usage_charged=False, possibly_billed=True,
                              requested_completion_tokens=cap)
                self.attempts.append(record)
                data = None
                retry = False
                try:
                    response = client.post(
                        self.settings.base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + self.settings.api_key.get_secret_value()},
                        timeout=timeout,
                    )
                    record["http_status"] = response.status_code
                    record["request_id"] = response.headers.get("x-request-id")
                    try:
                        data = response.json()
                    except ValueError:
                        pass
                    if isinstance(data, dict):
                        # Preserve usage verbatim, but never response body/reasoning in diagnostics.
                        record.update(model=data.get("model"), request_id=data.get("id")
                                      or record["request_id"], usage=data.get("usage"))
                    usage = record["usage"]
                    if isinstance(usage, dict) and all(
                        type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("prompt_tokens", "completion_tokens")
                    ):
                        record["usage_known"] = True
                        # Charge each response exactly once, including HTTP/business failures.
                        # completion_tokens includes reasoning_tokens already.
                        budget.charge({"prompt_eval_count": usage["prompt_tokens"],
                                       "eval_count": usage["completion_tokens"]})
                        record["usage_charged"] = True
                        if usage["completion_tokens"] > cap:
                            raise SparkError("reported_output_over_limit")
                    status = response.status_code
                    if status in (401, 403):
                        raise SparkError("authentication")
                    if status == 429 or status in (500, 502, 503, 504):
                        retry = True
                        raise SparkError("rate_limit" if status == 429 else "server")
                    if status >= 300:
                        raise SparkError("http_error")
                    if not isinstance(data, dict):
                        raise SparkError("invalid_json")
                    if data.get("error") or data.get("code", 0) != 0:
                        raise SparkError("business_error")
                    if not record["usage_known"]:
                        raise SparkError("usage_unknown")
                    try:
                        choice = data["choices"][0]
                        message = choice["message"]
                        finish = choice["finish_reason"]
                        content = message.get("content")
                    except (KeyError, IndexError, TypeError, AttributeError):
                        raise SparkError("invalid_response") from None
                    record["finish_reason"] = finish
                    if finish == "length":
                        raise SparkError("truncated", final_answer=content if isinstance(content, str) else None)
                    if finish != "stop" or message.get("tool_calls"):
                        raise SparkError("unsupported_finish")
                    if not isinstance(content, str) or not content.strip():
                        raise SparkError("empty_content")
                    remaining()
                    record["outcome"] = "success"
                    return {"role": "assistant", "content": content}
                except httpx.TimeoutException as exc:
                    record["transport_diagnostic"] = transport_diagnostic(exc)
                    error = SparkError("timeout")
                    retry = True
                except httpx.TransportError as exc:
                    record["transport_diagnostic"] = transport_diagnostic(exc)
                    error = SparkError("transport")
                    retry = True
                except SparkError as exc:
                    error = exc
                finally:
                    record["elapsed_seconds"] = self.clock() - started
                record["outcome"] = error.kind
                if not retry or attempt >= self.settings.retries:
                    raise error from None
                budget.allowance()  # No retry or backoff after known token exhaustion.
                delay = min(0.25 * (2 ** attempt), remaining())
                self.sleep(delay)
                remaining()
