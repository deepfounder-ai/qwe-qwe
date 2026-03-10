#!/usr/bin/env python3
"""NanoClaw CLI — lightweight AI agent for local models."""

import sys
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import agent, db

console = Console()


def main():
    console.print(Panel.fit(
        "[bold yellow]⚡ NanoClaw[/] — lightweight offline AI agent\n"
        "[dim]Tools: memory_search, memory_save, read_file, write_file, shell, web_fetch\n"
        "Commands: /clear (reset history) | /quit (exit)[/]",
        border_style="yellow",
    ))

    while True:
        try:
            user_input = console.input("\n[bold green]you >[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye ✨[/]")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            console.print("[dim]bye ✨[/]")
            break

        if user_input == "/clear":
            db.clear_history()
            console.print("[yellow]History cleared.[/]")
            continue

        with console.status("[yellow]thinking...[/]", spinner="dots"):
            try:
                reply = agent.run(user_input)
            except Exception as e:
                console.print(f"[red]Error: {e}[/]")
                continue

        console.print()
        try:
            console.print(Markdown(reply))
        except Exception:
            console.print(reply)


if __name__ == "__main__":
    main()
