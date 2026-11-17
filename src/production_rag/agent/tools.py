"""教学入口 3：模型只提供名字和参数，执行权留在程序白名单中。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500)
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    top_k: int = Field(default=4, ge=1, le=8)


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chunk_id: str = Field(min_length=1, max_length=64)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索技术知识库，可改写 query 或切换检索模式。",
            "parameters": SearchArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chunk",
            "description": "复查本次搜索已经发现的片段原文。",
            "parameters": ReadArgs.model_json_schema(),
        },
    },
]


class ToolRegistry:
    def __init__(self, retriever):
        self.retriever = retriever

    def execute(self, name: str, arguments: dict, state, budget):
        if name == "search_knowledge":
            args = SearchArgs.model_validate(arguments)
            return self.retriever.search(args.query, budget, mode=args.mode, top_k=args.top_k)
        if name == "read_chunk":
            args = ReadArgs.model_validate(arguments)
            if args.chunk_id not in state.evidence:
                raise ValueError("只能读取本次搜索已发现的 chunk_id")
            return [self.retriever.hit(args.chunk_id)]
        raise ValueError("不允许调用此工具")
