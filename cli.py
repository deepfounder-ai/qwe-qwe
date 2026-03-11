#!/usr/bin/env python3
"""qwe-qwe CLI — lightweight AI agent for local models."""

import sys, time, readline
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import agent, config, db, soul, skills, tasks, scheduler
import logger

_log = logger.get("cli")

console = Console()

# Enable readline for input history + arrow keys
readline.parse_and_bind('"\\e[A": previous-history')
readline.parse_and_bind('"\\e[B": next-history')
readline.parse_and_bind('"\\e[C": forward-char')
readline.parse_and_bind('"\\e[D": backward-char')

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
    s = soul.load()
    user_name = db.kv_get("user_name") or "Boss"
    city = db.kv_get("timezone_city") or "somewhere"
    mem_count = 0
    try:
        import memory
        mem_count = memory.count()
    except Exception:
        pass
    active = skills.get_active()

    console.print(f"""
  [dim]🦆 qwe-qwe — your fully offline AI agent[/]
  [dim]No cloud. No API keys. No subscriptions. Just your GPU.[/]
  
  [dim]🧠 Model:[/]  {config.LLM_MODEL} @ {config.LLM_BASE_URL}
  [dim]👤 User:[/]   {user_name} [dim]({city}, UTC{config.TZ_OFFSET:+d})[/]
  [dim]🤖 Agent:[/]  {s['name']} [dim]| {s['language']}[/]
  [dim]💾 Memory:[/] {mem_count} memories [dim]| SQLite + Qdrant[/]
  [dim]⚙️  Skills:[/] {', '.join(sorted(active)) if active else 'none'} [dim]| /skills to manage[/]
  
  [dim]Commands: /soul  /skills  /memory  /cron  /tasks  /stats  /logs  /clear  /quit[/]
"""
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
        f"[cyan]Memory:[/]      Qdrant ({agent.config.QDRANT_MODE}, {__import__('memory').count()} points)\n"
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
    console.print()
    console.print("  [bold magenta]🧬 Soul Editor[/]")
    console.print("  [dim]Enter value or press Enter to keep current. q to finish.[/]\n")

    for field in ("name", "language"):
        try:
            current = s[field]
            new_val = console.input(f"  [cyan]{field}[/] [dim]({current})[/] > ").strip()
            if new_val:
                soul.save(field, new_val)
                s[field] = new_val
        except (EOFError, KeyboardInterrupt):
            return

    console.print()
    numeric_traits = [k for k in s if k not in ("name", "language")]
    for trait in numeric_traits:
        value = s[trait]
        low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("low", "high"))
        bar = "█" * value + "░" * (10 - value)
        console.print(f"  [{bar}] [bold cyan]{trait}[/] = [bold]{value}[/]  [dim]{low} ← → {high}[/]")
        try:
            inp = console.input(f"  [dim]new value 0-10 (enter=keep)[/] > ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if inp == "q":
            break
        if inp.isdigit():
            v = max(0, min(10, int(inp)))
            soul.save(trait, v)
            s[trait] = v

    console.print()
    console.print(Panel(
        soul.format_display(soul.load()),
        title="[bold]🧬 Soul — saved[/]",
        border_style="magenta",
        padding=(0, 2),
    ))


def handle_skills_command(args: str):
    if args:
        parts = args.split(maxsplit=1)
        if len(parts) == 2:
            action, name = parts
            if action == "on":
                console.print(f"  [green]{skills.enable(name)}[/]")
            elif action == "off":
                console.print(f"  [yellow]{skills.disable(name)}[/]")
        return

    # Interactive skill selector
    import readchar

    all_skills = skills.list_all()
    if not all_skills:
        console.print("  [dim]No skills found in skills/ folder.[/]")
        return

    selected = {s["name"] for s in all_skills if s["active"]}
    cursor = 0

    def render():
        # Clear previous render
        if render.first:
            render.first = False
        else:
            # Move up and clear lines
            lines_to_clear = len(all_skills) + 3
            sys.stdout.write(f"\033[{lines_to_clear}A\033[J")

        console.print("  [bold magenta]⚡ Skills[/]  [dim]↑↓ move  space toggle  enter save[/]\n")
        for i, s in enumerate(all_skills):
            check = "[green]●[/]" if s["name"] in selected else "[dim]○[/]"
            pointer = "[bold yellow]▸[/]" if i == cursor else " "
            name_fmt = f"[bold]{s['name']}[/]" if s["name"] in selected else s["name"]
            tools_count = f"[dim]({s['tools']} tools)[/]"
            desc = f"[dim]— {s['description']}[/]" if s["description"] else ""
            console.print(f"  {pointer} {check} {name_fmt} {tools_count} {desc}")
        console.print()

    render.first = True
    render()

    while True:
        key = readchar.readkey()

        if key in (readchar.key.UP, "k"):
            cursor = (cursor - 1) % len(all_skills)
        elif key in (readchar.key.DOWN, "j"):
            cursor = (cursor + 1) % len(all_skills)
        elif key == " ":
            name = all_skills[cursor]["name"]
            if name in selected:
                selected.discard(name)
            else:
                selected.add(name)
        elif key in (readchar.key.ENTER, "\r", "\n"):
            # Save
            skills.set_active(selected)
            console.print(f"  [green]✓ Active: {', '.join(sorted(selected)) or 'none'}[/]")
            return
        elif key in ("q", readchar.key.ESC):
            return

        render()


def handle_cron(args: str):
    if args.startswith("rm "):
        try:
            task_id = int(args[3:].strip())
            console.print(f"  {scheduler.remove(task_id)}")
        except ValueError:
            console.print("  [dim]Usage: /cron rm <id>[/]")
        return

    tasks_list = scheduler.list_tasks()
    if not tasks_list:
        console.print("  [dim]No scheduled tasks. Agent can create them with schedule_task tool.[/]")
        return
    for t in tasks_list:
        status = "[green]●[/]" if t["enabled"] else "[dim]○[/]"
        repeat = "🔄" if t["repeat"] else "⏱"
        console.print(f"  {status} #{t['id']} {repeat} [bold]{t['name']}[/] → {t['next_run']} [dim]({t['schedule']})[/]")
        console.print(f"      [dim]{t['task'][:80]}[/]")
    console.print(f"\n  [dim]/cron rm <id> to remove[/]")


def show_tasks():
    results = tasks.get_results(clear=False)
    pending = tasks.pending_count()
    if not results and pending == 0:
        console.print("  [dim]No tasks.[/]")
        return
    if pending:
        console.print(f"  [yellow]⏳ {pending} task(s) running...[/]")
    for r in results:
        icon = "[green]✅[/]" if r["status"] == "done" else "[red]❌[/]"
        console.print(f"  {icon} #{r['id']} {r['task'][:50]}")
        console.print(f"      [dim]{r['result'][:100]}[/]")


def _check_background_tasks():
    """Show completed background tasks."""
    results = tasks.get_results(clear=True)
    for r in results:
        icon = "✅" if r["status"] == "done" else "❌"
        console.print(f"\n  [bold]{icon} Task #{r['id']}:[/] {r['task'][:60]}")
        console.print(f"  [dim]{r['result'][:200]}[/]\n")


def show_logs(args: str):
    """Show recent logs. Usage: /logs [errors] [N]"""
    parts = args.split()
    log_file = "errors.log" if "errors" in parts else "qwe-qwe.log"
    n = 20
    for p in parts:
        if p.isdigit():
            n = int(p)

    log_path = Path(__file__).parent / "logs" / log_file
    if not log_path.exists():
        console.print("  [dim]No logs yet.[/]")
        return

    lines = log_path.read_text().splitlines()
    tail = lines[-n:]
    console.print(f"  [bold]📋 {log_file}[/] [dim](last {len(tail)} lines)[/]\n")
    for line in tail:
        if "| ERROR" in line or "| WARNING" in line:
            console.print(f"  [red]{line}[/]")
        elif "| EVENT" in line:
            console.print(f"  [cyan]{line}[/]")
        else:
            console.print(f"  [dim]{line}[/]")
    console.print()


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


_cron_results: list[tuple] = []

def _on_cron_complete(name, task, result):
    _cron_results.append((name, task, result))
    # Push notification immediately to terminal
    print(f"\n\n  ⏰ Cron '{name}': {task[:60]}")
    print(f"  {result[:200]}\n")
    sys.stdout.flush()

_CITY_TZ = {
    "moscow": 3, "london": 0, "berlin": 1, "paris": 1,
    "new york": -5, "los angeles": -8, "chicago": -6,
    "tokyo": 9, "sydney": 11, "dubai": 4, "mumbai": 5,
    "beijing": 8, "singapore": 8, "istanbul": 3,
    "buenos aires": -3, "são paulo": -3, "sao paulo": -3,
    "mexico city": -6, "bogota": -5, "lima": -5,
    "bangkok": 7, "seoul": 9, "jakarta": 7,
    "cairo": 2, "nairobi": 3, "lagos": 1,
    "amsterdam": 1, "madrid": 1, "rome": 1,
    "warsaw": 1, "kyiv": 2, "tbilisi": 4,
}


def _first_run_setup():
    """Interactive setup on first launch — city, name, language, soul."""
    console.print("\n  [bold yellow]⚡ Welcome to qwe-qwe! Let's set up.[/]\n")

    # 1. City / timezone
    console.print("  [yellow]🌍 What city are you in?[/]")
    try:
        city = input("  City: ").strip().lower()
        offset = _CITY_TZ.get(city)
        if offset is None:
            for c, o in _CITY_TZ.items():
                if city in c or c in city:
                    offset = o
                    break
        if offset is None:
            console.print(f"  [dim]Don't know '{city}', defaulting to UTC.[/]")
            offset = 0
        config.TZ_OFFSET = offset
        db.kv_set("timezone", str(offset))
        db.kv_set("timezone_city", city)
    except (ValueError, EOFError):
        config.TZ_OFFSET = 0

    # 2. User's name
    console.print("\n  [yellow]👤 What should I call you?[/] [dim](default: Boss)[/]")
    user_name = input("  Your name: ").strip()
    db.kv_set("user_name", user_name or "Boss")

    # 3. Agent name
    console.print("\n  [yellow]🤖 What should your agent be called?[/] [dim](default: Agent)[/]")
    name = input("  Name: ").strip()
    if name:
        db.kv_set("soul:name", name)

    # 3. Language
    console.print("\n  [yellow]🗣 What language should it speak?[/] [dim](default: English)[/]")
    lang = input("  Language: ").strip()
    if lang:
        db.kv_set("soul:language", lang)

    # 4. Quick personality
    console.print("\n  [yellow]✨ Quick personality setup (0-10, Enter to skip):[/]")
    quick_traits = [
        ("humor", "Humor", "serious ↔ funny"),
        ("honesty", "Honesty", "diplomatic ↔ brutally direct"),
        ("brevity", "Brevity", "verbose ↔ concise"),
        ("formality", "Formality", "casual ↔ formal"),
        ("creativity", "Creativity", "practical ↔ unconventional"),
    ]
    for key, label, desc in quick_traits:
        try:
            val = input(f"  {label} ({desc}): ").strip()
            if val and val.isdigit():
                v = max(0, min(10, int(val)))
                db.kv_set(f"soul:{key}", str(v))
        except EOFError:
            break

    db.kv_set("setup_complete", "1")
    console.print("\n  [green]✓ Setup complete! Use /soul to tweak later.[/]\n")


def main():
    # Check first run
    if not db.kv_get("setup_complete"):
        _first_run_setup()
    else:
        # Load timezone from DB
        tz_val = db.kv_get("timezone")
        if tz_val is not None:
            config.TZ_OFFSET = int(tz_val)

    # Start scheduler
    scheduler.on_complete(_on_cron_complete)
    scheduler.start()
    console.print(f"  [dim]⏰ Scheduler running (UTC{config.TZ_OFFSET:+d})[/]")

    show_banner()
    _log.info("session started | model=%s | user=%s", config.LLM_MODEL, db.kv_get("user_name") or "Boss")

    while True:
        try:
            # Check background + scheduled tasks
            _check_background_tasks()
            while _cron_results:
                name, task, result = _cron_results.pop(0)
                console.print(f"\n  [bold]⏰ Cron '{name}':[/] {task[:60]}")
                console.print(f"  [dim]{result[:200]}[/]\n")
            # Status line + input separator
            console.print(f"  [dim]{_status_line()}[/]")
            console.print("  [dim]" + "─" * (console.width - 4) + "[/]")
            user_input = input("  ⚡ > ").strip()
        except (EOFError, KeyboardInterrupt):
            _log.info("session ended (user exit)")
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
        if user_input == "/tasks":
            show_tasks()
            continue
        if user_input.startswith("/cron"):
            handle_cron(user_input[5:].strip())
            continue
        if user_input == "/memory":
            search_memory()
            continue
        if user_input.startswith("/logs"):
            show_logs(user_input[5:].strip())
            continue
        if user_input.startswith("/"):
            console.print(f"  [dim]Unknown command: {user_input.split()[0]}[/]")
            continue

        try:
            result = agent.run(user_input)
        except Exception as e:
            _log.error(f"agent.run crashed: {e}", exc_info=True)
            console.print(f"  [red]✗ {str(e).replace('[', '(').replace(']', ')')}[/]")
            continue

        console.print()
        console.print(f"  🦆 ", end="")
        console.print(Markdown(result.reply))
        console.print()


def main_entry():
    """Unified entry point: `qwe-qwe` for CLI, `qwe-qwe --web` for web server."""
    import argparse
    parser = argparse.ArgumentParser(description="qwe-qwe — offline AI agent")
    parser.add_argument("--web", action="store_true", help="Start web server instead of CLI")
    parser.add_argument("--host", default="0.0.0.0", help="Web server host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=7860, help="Web server port (default: 7860)")
    args = parser.parse_args()

    if args.web:
        import server
        server.start(host=args.host, port=args.port)
    else:
        main()


if __name__ == "__main__":
    main_entry()
