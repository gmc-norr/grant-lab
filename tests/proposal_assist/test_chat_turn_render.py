def test_chat_turn_renders_turn_outcome_notice():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader("src/call_manager/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("proposal_assist/partials/chat_turn.html")

    class _Turn:
        role = "system"
        workspace_id = "00000000-0000-0000-0000-000000000000"
        content = {
            "type": "turn_outcome",
            "outcome": "failed",
            "text": "The drafting assistant couldn't finish this turn. Please try again.",
        }

    html = tmpl.render(turn=_Turn(), chat_reference_map={})
    assert 'class="turn-outcome"' in html
    assert "finish this turn" in html
    assert "<pre>" not in html  # not the fallback branch
