"""CLI REPL (LLD 2.13): in-process ChatService, no HTTP. `agent-chat [--debug] [--record NAME]`."""

import argparse
import asyncio
import json
import re
import sys

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import ChatReply, ChatService, InvalidMessage
from advisor_agent.config import get_settings

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _render(text: str, tty: bool) -> str:
    return _BOLD.sub(r"\033[1m\1\033[0m" if tty else r"\1", text)


def _print_reply(reply: ChatReply, service: ChatService, *, debug: bool, tty: bool) -> None:
    for msg in reply.messages:
        print(f"agent> {_render(msg, tty)}")
    if reply.quick_replies:
        print(f"       [{' | '.join(reply.quick_replies)}]")
    if debug and service.last_turn is not None:
        turn = service.last_turn
        nlu = turn.nlu.model_dump(mode="json", exclude_none=True) if turn.nlu else None
        print(f"  [debug] state={turn.state} templates={turn.template_ids}")
        if nlu:
            print(f"  [debug] nlu={json.dumps(nlu)}")


async def _run(debug: bool, record: str | None) -> None:
    settings = get_settings()
    if record:
        settings = settings.model_copy(update={"record_golden": True})
    service = build_chat_service(settings, scenario=record)
    runtime = service.runtime
    if runtime is not None:
        await runtime.start()  # MCP client + outbox worker run alongside the REPL
    try:
        await _repl(service, debug=debug)
    finally:
        if runtime is not None:
            await runtime.stop(drain=True)  # flush queued Google writes before exiting


async def _repl(service: ChatService, *, debug: bool) -> None:
    tty = sys.stdout.isatty()
    reply = await service.start()
    _print_reply(reply, service, debug=debug, tty=tty)
    while not reply.done:
        try:
            # input() in a thread so the outbox worker keeps running while we wait
            text = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not tty:
            print(text)
        if not text.strip():
            continue
        try:
            reply = await service.send(reply.session_id, text)
        except InvalidMessage as e:
            print(f"agent> ({e})")
            continue
        _print_reply(reply, service, debug=debug, tty=tty)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the advisor scheduling agent")
    parser.add_argument("--debug", action="store_true", help="print state + NLU after each turn")
    parser.add_argument("--record", metavar="SCENARIO", help="write tests/golden/SCENARIO.jsonl")
    args = parser.parse_args()
    asyncio.run(_run(args.debug, args.record))


if __name__ == "__main__":
    main()
