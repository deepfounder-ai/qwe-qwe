#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
import agent, db, soul

console = Console()

LOGO = """[bold yellow]
   ██████╗ ██╗    ██╗███████╗     ██████╗ ██╗    ██╗███████╗
  ██╔═══██╗██║    ██║██╔════╝    ██╔═══██╗██║    ██║██╔════╝
  ██║   ██║██║ █╗ ██║█████╗█████╗██║   ██║██║ █╗ ██║█████╗  
  ██║▄▄ ██║██║███╗██║██╔══╝╚════╝██║▄▄ ██║██║███╗██║██╔══╝  
  ╚██████╔╝╚███╔███╔╝███████╗    ╚██████╔╝╚███╔███╔╝███████╗
   ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝     ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝[/]"""

COMMANDS = {
    "/soul": "Show/edit personality",
    "/memory": "Search memories",
    "/stats": "Session stats",
    "/clear": "Reset history",
    "/quit": "Exit",
}


def show_banner():
    console.print(LOGO)
    console.print(
        "[dim]  lightweight offline AI agent • runs on your hardware[/]\n",
        justify="center",
    )
    cols = "  ".join(f"[bold cyan]{k}[/][dim] {v}[/]" for k, v in COMMANDS.items())
    console.print(f"  {cols}\n")

    # Show current soul
    s = soul.load()
    console.print(f"  [yellow]⚡ {s['name']}[/] [dim]| {s['language']} | "
                  f"humor:{s['humor']} honesty:{s['honesty']} brevity:{s['brevity']}[/]\n")


def show_stats():
    s_prompt = db.kv_get("session_prompt_tokens") or "0"
    s_compl = db.kv_get("session_completion_tokens") or "0"
    s_turns = db.kv_get("session_turns") or "0"
    s_total = int(s_prompt) + int(s_compl)
    s = soul.load()
    console.print(Panel(
        f"[cyan]Agent:[/]       {s['name']}\n"
        f"[cyan]Turns:[/]       {s_turns}\n"
        f"[cyan]Tokens:[/]      ↑{s_prompt} prompt  ↓{s_compl} completion  Σ{s_total} total\n"
        f"[cyan]Model:[/]       {agent.config.LLM_MODEL}\n"
        f"[cyan]Memory:[/]      Qdrant ({agent.config.QDRANT_MODE})\n"
        f"[cyan]Database:[/]    {agent.config.DB_PATH}",
        title="[bold]📊 Session Stats[/]",
        border_style="cyan",
        padding=(0, 2),
    ))


def handle_soul_command(args: str):
    s = soul.load()
    if not args:
        # Show current soul
        console.print(Panel(
            soul.format_display(s),
            title="[bold]🧬 Soul Config[/]",
            border_style="magenta",
            padding=(0, 2),
        ))
        console.print("  [dim]Set: /soul name Джоник  |  /soul humor 8  |  /soul language English[/]")
        return

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        console.print(f"  [dim]Current {parts[0]}: {s.get(parts[0], '?')}[/]")
        return

    key, value = parts
    # Try to parse as int for numeric traits
    try:
        value_int = int(value)
        if 0 <= value_int <= 10:
            value = value_int
        else:
            console.print("  [red]Value must be 0-10[/]")
            return
    except ValueError:
        pass  # string value (name, language)

    result = soul.save(key, value)
    console.print(f"  [magenta]{result}[/]")


def search_memory():
    query = console.input("[cyan]  search query >[/] ").strip()
    if not query:
        return
    import memory as mem
    results = mem.search(query, limit=5)
    if not results:
        console.print("  [dim]No memories found.[/]")
        return
    for r in results:
        score_color = "green" if r["score"] > 0.7 else "yellow" if r["score"] > 0.5 else "dim"
        console.print(
            f"  [{score_color}]●[/] [{score_color}]{r['score']}[/] "
            f"[bold]{r['tag']}[/] → {r['text']}"
        )


def main():
    show_banner()

    while True:
        try:
            user_input = console.input("[bold green]  ⚡ >[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]👋 bye[/]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            console.print("  [dim]👋 bye[/]")
            break
        if user_input == "/clear":
            db.clear_history()
            console.print("  [yellow]✓ History cleared.[/]")
            continue
        if user_input == "/stats":
            show_stats()
            continue
        if user_input.startswith("/soul"):
            handle_soul_command(user_input[5:].strip())
            continue
        if user_input == "/memory":
            search_memory()
            continue

        t0 = time.time()

        with console.status("[yellow]  thinking...[/]", spinner="dots"):
            try:
                result = agent.run(user_input)
            except Exception as e:
                console.print(f"  [red]✗ {e}[/]")
                continue

        elapsed = time.time() - t0

        # Build footer
        parts = [f"{elapsed:.1f}s"]
        parts.append(f"↑{result.prompt_tokens} ↓{result.completion_tokens}")
        session_total = int(db.kv_get("session_prompt_tokens") or "0") + \
                        int(db.kv_get("session_completion_tokens") or "0")
        parts.append(f"Σ{session_total}")
        if result.auto_context_hits:
            parts.append(f"📎{result.auto_context_hits}")
        if result.tool_calls_made:
            parts.append(f"🔧 {', '.join(result.tool_calls_made)}")
        footer = " │ ".join(parts)

        console.print()
        console.print(Panel(
            Markdown(result.reply),
            border_style="yellow",
            padding=(0, 2),
            subtitle=f"[dim]{footer}[/]",
            subtitle_align="right",
        ))


if __name__ == "__main__":
    main()
