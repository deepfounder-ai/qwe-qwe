"""Soul Editor skill — add/remove custom personality traits."""

DESCRIPTION = "Add or remove custom personality traits for the agent"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_trait",
            "description": "Add a new custom personality trait with low/high descriptions and a value 0-10.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Trait name (e.g. 'sarcasm', 'patience')"},
                    "low_desc": {"type": "string", "description": "Description when trait is 0 (e.g. 'never sarcastic')"},
                    "high_desc": {"type": "string", "description": "Description when trait is 10 (e.g. 'very sarcastic')"},
                    "value": {"type": "integer", "description": "Initial value 0-10"},
                },
                "required": ["name", "low_desc", "high_desc", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_trait",
            "description": "Remove a custom personality trait.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Trait name to remove"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_traits",
            "description": "List all personality traits including custom ones.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def execute(name: str, args: dict) -> str:
    import json
    import db
    import soul

    if name == "add_trait":
        trait_name = args["name"].lower().replace(" ", "_")
        value = max(0, min(10, args.get("value", 5)))
        low_desc = args["low_desc"]
        high_desc = args["high_desc"]

        # Save trait value
        db.kv_set(f"soul:{trait_name}", str(value))

        # Save custom trait descriptions
        custom = _load_custom_traits()
        custom[trait_name] = {"low": low_desc, "high": high_desc}
        db.kv_set("soul:_custom_traits", json.dumps(custom))

        # Register in DEFAULTS and TRAIT_DESCRIPTIONS for this session
        soul.DEFAULTS[trait_name] = value
        soul.TRAIT_DESCRIPTIONS[trait_name] = (low_desc, high_desc)

        return f"✓ Added trait '{trait_name}' = {value}/10 ({low_desc} ← → {high_desc})"

    elif name == "remove_trait":
        trait_name = args["name"].lower().replace(" ", "_")

        # Don't allow removing core traits
        core = {"humor", "honesty", "curiosity", "brevity", "formality", "proactivity", "empathy", "creativity"}
        if trait_name in core:
            return f"Can't remove core trait '{trait_name}'. Only custom traits can be removed."

        custom = _load_custom_traits()
        if trait_name not in custom:
            return f"Custom trait '{trait_name}' not found."

        del custom[trait_name]
        db.kv_set("soul:_custom_traits", json.dumps(custom))
        db.kv_set(f"soul:{trait_name}", "")

        # Remove from runtime
        soul.DEFAULTS.pop(trait_name, None)
        soul.TRAIT_DESCRIPTIONS.pop(trait_name, None)

        return f"✓ Removed trait '{trait_name}'"

    elif name == "list_traits":
        _ensure_custom_loaded()
        s = soul.load()
        lines = []
        custom = _load_custom_traits()
        for trait, value in s.items():
            if trait in ("name", "language"):
                continue
            marker = " ★" if trait in custom else ""
            low, high = soul.TRAIT_DESCRIPTIONS.get(trait, ("?", "?"))
            bar = "█" * value + "░" * (10 - value)
            lines.append(f"  [{bar}] {trait}={value}{marker}  ({low} ← → {high})")
        return "\n".join(lines) if lines else "No traits configured."

    return f"Unknown tool: {name}"


def _load_custom_traits() -> dict:
    import json, db
    raw = db.kv_get("soul:_custom_traits")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _ensure_custom_loaded():
    """Load custom traits into soul module at runtime."""
    import soul
    custom = _load_custom_traits()
    for trait_name, descs in custom.items():
        if trait_name not in soul.DEFAULTS:
            soul.DEFAULTS[trait_name] = 5
            soul.TRAIT_DESCRIPTIONS[trait_name] = (descs["low"], descs["high"])
