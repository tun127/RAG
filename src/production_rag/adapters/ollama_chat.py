from ollama import Client

from production_rag.domain.errors import BudgetExceeded, DependencyError


class OllamaChat:
    def __init__(self, settings):
        self.settings = settings
        self.model = settings.chat_model

    def chat(self, messages, budget, *, tools=None, schema=None) -> dict:
        num_predict = budget.allowance()
        client = Client(host=self.settings.ollama_url, timeout=budget.remaining_seconds())
        try:
            response = client.chat(
                model=self.model,
                messages=messages,
                tools=tools,
                format=schema,
                options={
                    "temperature": 0,
                    "num_predict": num_predict,
                    "num_ctx": self.settings.model_context_tokens,
                },
                stream=False,
            ).model_dump(exclude_none=True)
            budget.charge(response)
            budget.remaining_seconds()
            return response["message"]
        except BudgetExceeded:
            raise
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Ollama call failed: %s", exc)
            raise DependencyError("Ollama 生成失败；检查服务、模型工具能力或请求超时") from exc
        finally:
            client.close()
