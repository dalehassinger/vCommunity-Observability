#!/usr/bin/env python3
"""Apple Reminders as MCP tools for vCommunity Ask (macOS).

A small stdio MCP server (standard library only). The Ask host agent runs it on the Mac,
like your PowerShell MCP server, so the model can add to a Reminders list such as
"Groceries" from a chat prompt or a scheduled prompt. Add it to the agent's MCP config
(~/.config/ask-host-agent/mcp.json):

    "apple": { "type": "stdio", "command": "python3",
               "args": ["/path/to/vCommunity-Observability/ask/agent/apple-mcp.py"], "env": {} }

Tools:
  reminders_lists                      the Reminders lists (read-only)
  reminders_list   {list}              open (not completed) items of a list (read-only)
  reminders_add    {list, items, note?} adds items; items already open on the list are skipped
  reminders_complete {list, items}     ticks off open items with these names

It talks to Reminders with osascript, so macOS asks once to let python3 control Reminders.
APPLE_MCP_DEFAULT_LIST (env) is the list used when a call names none (default "Groceries")."""

from __future__ import annotations

import json
import os
import subprocess
import sys

VERSION = "1.0.0"
DEFAULT_LIST = os.environ.get("APPLE_MCP_DEFAULT_LIST", "Groceries")
SEP = "\x1f"                      # between names in osascript output (names may hold commas)

LIST_PROP = {"type": "string", "description": f'Reminders list name, for example "{DEFAULT_LIST}" (default {DEFAULT_LIST})'}
ITEMS_PROP = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
              "description": 'One entry per item, for example ["Milk", "Eggs", "Bread"]'}

TOOLS = [
    {"name": "reminders_lists", "description": "Lists the names of the Apple Reminders lists on this Mac.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "title": "Reminders lists"}},
    {"name": "reminders_list",
     "description": "Shows the open (not yet completed) items of an Apple Reminders list, such as the Groceries list.",
     "inputSchema": {"type": "object", "properties": {"list": LIST_PROP}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "title": "Reminders: open items"}},
    {"name": "reminders_add",
     "description": "Adds items to an Apple Reminders list, such as the Groceries list. Give each item separately "
                    "(\"Milk\", \"Eggs\"), not one comma-joined string. Items already open on the list are skipped.",
     "inputSchema": {"type": "object", "properties": {
         "list": LIST_PROP, "items": ITEMS_PROP,
         "note": {"type": "string", "description": "Optional note added to every new item (for example a quantity)"}},
         "required": ["items"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "title": "Reminders: add items"}},
    {"name": "reminders_complete",
     "description": "Marks open items of an Apple Reminders list as completed (ticks them off), matched by name.",
     "inputSchema": {"type": "object", "properties": {"list": LIST_PROP, "items": ITEMS_PROP},
                     "required": ["items"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "title": "Reminders: complete items"}},
]

# Every script takes its values as arguments (no quoting problems with names).
SCRIPTS = {
    "lists": """on run argv
  tell application "Reminders" to set n to name of every list
  set AppleScript's text item delimiters to (character id 31)
  return n as text
end run""",
    "open": """on run argv
  set listName to item 1 of argv
  tell application "Reminders"
    if not (exists list listName) then error "No Reminders list named " & listName number 1001
    set n to name of (every reminder of list listName whose completed is false)
  end tell
  set AppleScript's text item delimiters to (character id 31)
  return n as text
end run""",
    "add": """on run argv
  set listName to item 1 of argv
  set noteText to item 2 of argv
  tell application "Reminders"
    if not (exists list listName) then error "No Reminders list named " & listName number 1001
    repeat with i from 3 to count of argv
      if noteText is "" then
        tell list listName to make new reminder with properties {name:(item i of argv)}
      else
        tell list listName to make new reminder with properties {name:(item i of argv), body:noteText}
      end if
    end repeat
  end tell
  return (count of argv) - 2
end run""",
    "complete": """on run argv
  set listName to item 1 of argv
  set done to 0
  tell application "Reminders"
    if not (exists list listName) then error "No Reminders list named " & listName number 1001
    repeat with i from 2 to count of argv
      set hits to (every reminder of list listName whose completed is false and name is (item i of argv))
      repeat with r in hits
        set completed of r to true
        set done to done + 1
      end repeat
    end repeat
  end tell
  return done
end run""",
}


class ToolError(Exception):
    pass


def osa(script: str, *args: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", SCRIPTS[script], *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ToolError("osascript not found: Apple Reminders works on macOS only.") from None
    except subprocess.TimeoutExpired:
        raise ToolError("Reminders did not answer within 60 seconds.") from None
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise ToolError("macOS did not allow controlling Reminders. Allow it in System Settings > Privacy & "
                            "Security > Automation (python3 > Reminders), then try again.")
        if "1001" in err or "No Reminders list" in err:
            raise ToolError(f'There is no Reminders list named "{args[0]}". Call reminders_lists to see the names.')
        raise ToolError(err[:300] or "osascript failed")
    return r.stdout.rstrip("\n")


def names(text: str) -> list[str]:
    return [x for x in text.split(SEP) if x.strip()] if text else []


def clean_items(raw) -> list[str]:
    if isinstance(raw, str):                         # a model sometimes sends "Milk, Eggs"
        raw = raw.split(",")
    out, seen = [], set()
    for x in raw or []:
        x = " ".join(str(x).split())[:200]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:50]


def call(name: str, args: dict) -> str:
    lst = " ".join(str(args.get("list") or DEFAULT_LIST).split()) or DEFAULT_LIST
    if name == "reminders_lists":
        return "Reminders lists: " + ", ".join(names(osa("lists")))
    if name == "reminders_list":
        items = names(osa("open", lst))
        return f'{lst}: {len(items)} open item(s)' + ("".join(f"\n- {i}" for i in items) if items else ".")
    items = clean_items(args.get("items"))
    if not items:
        raise ToolError("items is empty: give one or more item names.")
    if name == "reminders_add":
        have = {i.lower() for i in names(osa("open", lst))}
        new = [i for i in items if i.lower() not in have]
        skipped = [i for i in items if i.lower() in have]
        if new:
            osa("add", lst, " ".join(str(args.get("note") or "").split())[:500], *new)
        msg = f'Added {len(new)} item(s) to {lst}' + (": " + ", ".join(new) if new else "")
        if skipped:
            msg += f'. Already on the list, skipped: {", ".join(skipped)}'
        return msg + "."
    if name == "reminders_complete":
        n = osa("complete", lst, *items)
        return f"Completed {n} item(s) on {lst}."
    raise ToolError(f"Unknown tool {name}")


def reply(mid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": mid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if mid is None:                               # notifications
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "apple", "version": VERSION}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                text = call(str(params.get("name")), params.get("arguments") or {})
                reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except ToolError as e:
                reply(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        elif method == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
