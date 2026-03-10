#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import agent, db, soul, skills

console = Console()

LOGO = """[bold yellow]
   ██████╗ ██╗    ██╗███████╗     ██████╗ ██╗    ██╗███████╗
  ██╔═══██╗██║    ██║██╔════╝    ██╔═══██╗██║    ██║██╔════╝
  ██║   ██║██║ █╗ ██║█████╗█████╗██║   ██║██║ █╗ ██║█████╗  
  ██║▄▄ ██║██║███╗██║██╔══╝╚════╝██║▄▄ ██║██║███╗██║██╔══╝  
  ╚██████╔╝╚███╔███╔╝███████╗    ╚██████╔╝╚███╔███╔╝███████╗
   ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝     ╚══▀▀═╝  ╚══╝╚══╝ ╚══════╝[/]"""


def _soul_bar_text() -> str:
    s = soul.load()
    traits = " ".join(f"{k}:{v}" for k, v in s.items() if k not in ("name", "language"))
    return f"⚡ {s['name']} | {s['language']} | {traits}"


def _status_line() -> str:
    s = soul.load()
    s_prompt = int(db.kv_get("session_prompt_tokens") or "0")
    s_compl = int(db.kv_get("session_completion_tokens") or "0")
    s_total = s_prompt + s_compl
    s_turns = db.kv_get("session_turns") or "0"
    active_skills = skills.get_active()
    sk = f" | skills: {','.join(sorted(active_skills))}" if active_skills else ""
    return (
        f"agent {s['name']} | {agent.config.LLM_MODEL} | "
        f"tokens {s_total:,} ({s_turns} turns){sk}"
    )


def show_banner():
    console.print(LOGO)
    console.print(
        "[dim]  lightweight offline AI agent • runs on your hardware[/]",
        justify="center",
    )
    console.print(
        "  [dim]/soul  /skills  /memory  /stats  /clear  /quit[/]\n"
    )


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


def _render_slider(value: int, width: int = 20) -> str:
    pos = int(value / 10 * (width - 1))
    bar = "─" * pos + "[bold yellow]●[/]" + "─" * (width - 1 - pos)
    return bar


def handle_soul_command(args: str):
    s = soul.load()
    if args:
        parts = args.split(maxsplit=1)
        if len(parts) == 2:
            key, value = parts
            try:
                value_int = int(value)
                if 0 <= value_int <= 10:
                    value = value_int
                else:
                    console.print("  [red]Value must be 0-10[/]")
                    return
            except ValueError:
                pass
            result = soul.save(key, value)
            console.print(f"  [magenta]{result}[/]")
        return

    # Interactive mode
    console.print(Panel(
        "[bold]🧬 Soul Editor[/]\n"
        "[dim]← → adjust  │  Enter confirm  │  q done[/]",
        border_style="magenta",
        padding=(0, 2),
    ))

    for field in ("name", "language"):
        try:
            current = s[field]
            new_val = console.input(f"  [cyan]{field}[/] [dim]({current})[/] > ").strip()
            if new_val:
                soul.save(field, new_val)
                s[field] = new_val
        except (EOFError, KeyboardInterrupt):
            return

    numeric_traits = [k for k in s if k not in ("name", "language")]
    for trait in numeric_traits:
        value = s[trait]
        low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("low", "high"))
        while True:
            slider = _render_slider(value)
            console.print(
                f"\r  [dim]{low[:12]:>12s}[/] {slider} [dim]{high[:12]:<12s}[/]  "
                f"[bold cyan]{trait}[/] = [bold yellow]{value:>2d}[/]/10",
                end="",
            )
            try:
                key = console.input("  [dim](←-/+→/enter)[/] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if key in ("-", "a", "[", ","):
                value = max(0, value - 1)
            elif key in ("+", "d", "]", ".", "="):
                value = min(10, value + 1)
            elif key.isdigit():
                v = int(key)
                if v == 1 and value == 1:
                    value = 10
                else:
                    value = min(v, 10)
                soul.save(trait, value)
                s[trait] = value
                break
            elif key == "":
                soul.save(trait, value)
                s[trait] = value
                break
            elif key == "q":
                soul.save(trait, value)
                console.print()
                console.print(Panel(
                    soul.format_display(soul.load()),
                    title="[bold]🧬 Soul — saved[/]",
                    border_style="magenta",
                    padding=(0, 2),
                ))
                return

    console.print()
    console.print(Panel(
        soul.format_display(soul.load()),
        title="[bold]🧬 Soul — saved[/]",
        border_style="magenta",
        padding=(0, 2),
    ))


def handle_skills_command(args: str):
    if not args:
        all_skills = skills.list_all()
        if not all_skills:
            console.print("  [dim]No skills found in skills/ folder.[/]")
            return
        for s in all_skills:
            status = "[green]●[/]" if s["active"] else "[dim]○[/]"
            tools_count = f"[dim]({s['tools']} tools)[/]"
            desc = f"[dim]— {s['description']}[/]" if s["description"] else ""
            console.print(f"  {status} [bold]{s['name']}[/] {tools_count} {desc}")
        console.print("\n  [dim]/skills on <name>  |  /skills off <name>[/]")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        console.print("  [dim]Usage: /skills on <name>  |  /skills off <name>[/]")
        return
    action, name = parts
    if action == "on":
        console.print(f"  [green]{skills.enable(name)}[/]")
    elif action == "off":
        console.print(f"  [yellow]{skills.disable(name)}[/]")
    else:
        console.print("  [dim]Usage: /skills on <name>  |  /skills off <name>[/]")


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
            # Status line + input separator
            console.print(f"  [dim]{_status_line()}[/]")
            console.print("  [dim]" + "─" * (console.width - 4) + "[/]")
            user_input = console.input("  ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [dim]👋[/]")
            break

        if not user_input:
            continue
        if user_input == "/quit":
            console.print("  [dim]👋[/]")
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
        if user_input.startswith("/skills"):
            handle_skills_command(user_input[7:].strip())
            continue
        if user_input == "/memory":
            search_memory()
            continue
        if user_input.startswith("/"):
            console.print(f"  [dim]Unknown command: {user_input.split()[0]}[/]")
            continue

        with console.status("[yellow]  ...[/]", spinner="dots"):
            try:
                result = agent.run(user_input)
            except Exception as e:
                console.print(f"  [red]✗ {e}[/]")
                continue

        console.print()
        console.print(Markdown(result.reply))
        console.print()


if __name__ == "__main__":
    main()
