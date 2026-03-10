#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
import agent, db

console = Console()

LOGO = """[bold yellow]
   ██████╗ ██╗    ██╗███████╗     ██████╗ ██╗    ██╗███████╗
  ██╔═══██╗██║    ██║██╔════╝    ██╔═══██╗██║    ██║██╔════╝
  ██║   ██║██║ █╗ ██║█████╗█████╗██║   ██║██║ █╗ ██║█████╗  
  ██║▄▄ ██║██║███╗██║██╔══╝╚════╝██║▄▄ ██║██║███╗██║██╔══╝  
  ╚██████╔╝╚███╔███╔╝███████╗    ╚██████╔╝╚███╔███╔╝███████╗
   ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝     ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝[/]"""

COMMANDS = {
    "/clear": "Reset conversation history",
    "/memory": "Search your memories",
    "/stats": "Show session stats",
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


def show_stats():
    history = db.get_recent_messages(limit=9999)
    user_msgs = sum(1 for m in history if m["role"] == "user")
    asst_msgs = sum(1 for m in history if m["role"] == "assistant")
    s_prompt = db.kv_get("session_prompt_tokens") or "0"
    s_compl = db.kv_get("session_completion_tokens") or "0"
    s_turns = db.kv_get("session_turns") or "0"
    s_total = int(s_prompt) + int(s_compl)
    console.print(Panel(
        f"[cyan]Messages:[/]    {user_msgs} you • {asst_msgs} agent\n"
        f"[cyan]Turns:[/]       {s_turns}\n"
        f"[cyan]Tokens:[/]      ↑{s_prompt} prompt  ↓{s_compl} completion  Σ{s_total} total\n"
        f"[cyan]Model:[/]       {agent.config.LLM_MODEL}\n"
        f"[cyan]Database:[/]    qwe_qwe.db\n"
        f"[cyan]Memory:[/]      Qdrant in-memory",
        title="[bold]📊 Session Stats[/]",
        border_style="cyan",
        padding=(0, 2),
    ))


def search_memory():
    query = console.input("[cyan]  search query >[/] ").strip()
    if not query:
        return
    import memory
    results = memory.search(query, limit=5)
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

    turn = 0
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
        if user_input == "/memory":
            search_memory()
            continue

        turn += 1
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
