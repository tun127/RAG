from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SparkSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPARK_", extra="ignore", hide_input_in_errors=True)
    api_key: SecretStr
    base_url: Literal["https://maas-api.cn-huabei-1.xf-yun.com/v2"] = (
        "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    )
    model: Literal["spark-x2.6"] = "spark-x2.6"
    timeout: float = Field(default=60, gt=0, le=300)
    max_completion_tokens: int = Field(default=768, gt=0, le=131072)
    thinking: Literal["enabled", "disabled", "auto"] = "disabled"
    retries: int = Field(default=1, ge=0, le=3)

    @field_validator("api_key")
    @classmethod
    def nonempty(cls, value):
        if not value.get_secret_value().strip():
            raise ValueError("SPARK_API_KEY is required")
        return value

    @classmethod
    def load(cls, env_file=None, **overrides):
        # Never expose Pydantic errors containing environment inputs.
        try:
            # Explicit validated runner settings take precedence over environment
            # and dotenv values; credentials still use env > explicit dotenv.
            return cls(_env_file=env_file, **overrides)
        except Exception:
            raise ValueError("Invalid Spark configuration; check SPARK_API_KEY and options") from None
