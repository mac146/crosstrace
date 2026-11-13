"""A refund agent that calls a model (official openai SDK) and then an MCP tool, with a retry policy you can switch.

    python agent.py --mode naive|careful|restart

naive    retries the tool on any error. When a tool succeeded but its response was lost, the retry refunds the customer twice.
careful  on an error, asks a read-only tool whether the refund happened; if it cannot confirm, it stops with a clear error.
restart  does not retry the tool, but when a MODEL call fails it starts the whole task over, tool call included. It recovers and prints the
         right answer, and has refunded the customer twice.

The MCP server is launched through `crosstrace mcp-wrap`, which only records or replays when CrossTrace is running it. The official Python
SDK starts servers with a reduced environment, so CROSSTRACE_MCP is passed on explicitly.
"""
import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import APIError, OpenAI

HERE = os.path.dirname(os.path.abspath(__file__))


def openai_tools(listing):
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
            for t in listing.tools]


async def call_tool(session, name, args, mode):
    if mode == "naive":
        for attempt in range(3):
            try:
                return await session.call_tool(name, args)
            except Exception as exc:                                   # noqa: BLE001 - this is the bug being demonstrated
                print(f"tool call failed ({exc}); retrying", file=sys.stderr)
        raise RuntimeError(f"{name} failed 3 times")
    if mode == "restart":
        return await session.call_tool(name, args)
    try:
        return await session.call_tool(name, args)
    except Exception as exc:                                           # noqa: BLE001
        status = await session.call_tool("refund_status", {"customer_id": args.get("customer_id", "")})
        if status.is_error:
            raise RuntimeError(f"{name} failed ({exc}) and I could not check whether it went through; stopping") from exc
        return status


async def flow(session, model, tools, mode):
    messages = [{"role": "user", "content": "Customer 42 reports a damaged item. Refund them."}]
    first = model.chat.completions.create(model="mock", messages=messages, tools=tools).choices[0].message
    call = first.tool_calls[0]
    result = await call_tool(session, call.function.name, json.loads(call.function.arguments), mode)
    messages += [{"role": "assistant", "content": None, "tool_calls": [
        {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}]},
        {"role": "tool", "tool_call_id": call.id, "content": result.content[0].text}]
    return model.chat.completions.create(model="mock", messages=messages, tools=tools).choices[0].message.content


async def main(mode):
    env = {k: v for k, v in os.environ.items() if k in ("CROSSTRACE_MCP", "REFUND_LEDGER", "PATH", "HOME", "PYTHONPATH")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "crosstrace", "mcp-wrap", "--", sys.executable,
                                                                   os.path.join(HERE, "server.py")], env=env)
    model = OpenAI(max_retries=0 if mode == "restart" else 2)         # restart handles model failures itself
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        tools = None
        for attempt in range(2 if mode == "restart" else 1):
            try:
                if tools is None:
                    await session.initialize()
                    tools = openai_tools(await session.list_tools())
                print(await flow(session, model, tools, mode))
                return
            except APIError as exc:
                print(f"model call failed ({exc}); starting over", file=sys.stderr)
        raise RuntimeError("the model call failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["naive", "careful", "restart"], default="naive")
    sys.exit(asyncio.run(main(ap.parse_args().mode)) or 0)
