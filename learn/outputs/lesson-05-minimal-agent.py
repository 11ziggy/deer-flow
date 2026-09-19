from __future__ import annotations

import asyncio
import json
import operator
import os
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    tool_call_count: Annotated[int, operator.add]


@tool
def add_numbers(left: float, right: float) -> float:
    """计算两个数字的和。"""
    return left + right


@tool
def multiply_numbers(left: float, right: float) -> float:
    """计算两个数字的乘积。"""
    return left * right


TOOLS: list[BaseTool] = [add_numbers, multiply_numbers]
TOOLS_BY_NAME = {item.name: item for item in TOOLS}

model = ChatOpenAI(
    model=os.environ["LLM_MODEL"],
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
    temperature=0,
    timeout=60,
)
model_with_tools = model.bind_tools(TOOLS)


def call_model(state: AgentState) -> dict:
    response = model_with_tools.invoke(
        [
            SystemMessage(
                "你是计算助手。涉及加法或乘法时必须使用工具；"
                "工具成功后只报告结果。"
            ),
            *state.get("messages", []),
        ]
    )
    return {"messages": [response]}


def execute_tools(state: AgentState) -> dict:
    last_message = state["messages"][-1]
    if not isinstance(last_message, AIMessage):
        raise TypeError("tools 节点要求最后一条消息是 AIMessage")

    results: list[ToolMessage] = []
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        selected_tool = TOOLS_BY_NAME.get(tool_name)
        if selected_tool is None:
            result = f"错误：未授权工具 {tool_name!r}"
        else:
            try:
                result = selected_tool.invoke(tool_call["args"])
            except Exception as error:
                result = f"工具执行失败：{type(error).__name__}: {error}"

        results.append(
            ToolMessage(
                content=json.dumps(result, ensure_ascii=False),
                tool_call_id=tool_call["id"],
                name=tool_name,
            )
        )

    return {
        "messages": results,
        "tool_call_count": len(last_message.tool_calls),
    }


def route_after_model(state: AgentState) -> Literal["tools", "__end__"]:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return END


builder = StateGraph(AgentState)
builder.add_node("model", call_model)
builder.add_node("tools", execute_tools)
builder.add_edge(START, "model")
builder.add_conditional_edges(
    "model",
    route_after_model,
    {"tools": "tools", END: END},
)
builder.add_edge("tools", "model")

checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)
config = {"configurable": {"thread_id": "calculator-demo"}}


def run_first_turn() -> None:
    final_state = graph.invoke(
        {
            "messages": [HumanMessage("计算 15 × 4，必须使用工具。")],
            "tool_call_count": 0,
        },
        config=config,
    )
    print("第一轮：", final_state["messages"][-1].content)
    print("累计工具调用：", final_state["tool_call_count"])


async def stream_second_turn() -> None:
    print("第二轮：", end="", flush=True)
    async for chunk, metadata in graph.astream(
        {"messages": [HumanMessage("在刚才结果上加 5，必须使用工具。")]},
        config=config,
        stream_mode="messages",
    ):
        # messages 模式还会流出工具调用 Chunk；这里只显示 model 节点的文本。
        if (
            isinstance(chunk, AIMessageChunk)
            and metadata.get("langgraph_node") == "model"
            and isinstance(chunk.content, str)
            and chunk.content
        ):
            print(chunk.content, end="", flush=True)

    snapshot = graph.get_state(config)
    print("\n累计工具调用：", snapshot.values["tool_call_count"])


if __name__ == "__main__":
    run_first_turn()
    asyncio.run(stream_second_turn())