from datetime import datetime

import planner

NOW = datetime(2026, 11, 2, 20, 0)


def test_validate_maps_aliases_and_parses_time():
    plan = planner.validate(
        {
            "title": "Prep",
            "unsupported": "",
            "steps": [{"action": "create_reminder", "args": {"name": "Rehearse", "due": "tomorrow 9am"}}],
        },
        NOW,
    )
    assert not plan.errors
    step = plan.steps[0]
    assert step.args["title"] == "Rehearse"
    assert step.args["at"] == "2026-11-03T09:00:00"
    assert step.risk == "reversible"


def test_unknown_actions_and_bad_args_are_reported_not_guessed():
    plan = planner.validate(
        {
            "title": "x",
            "unsupported": "",
            "steps": [
                {"action": "rm_rf", "args": {}},
                {"action": "create_event", "args": {"title": "No time"}},
            ],
        },
        NOW,
    )
    assert plan.steps == []
    assert len(plan.errors) == 2


def test_placeholders_are_rejected():
    plan = planner.validate(
        {"title": "x", "unsupported": "", "steps": [{"action": "write_file", "args": {"path": "Desktop/a.md", "content": "Dear [Professor's Name],"}}]},
        NOW,
    )
    assert plan.steps == []
    assert "placeholder" in plan.errors[0]


def test_references_are_renumbered_after_a_rejected_step():
    plan = planner.validate(
        {
            "title": "x",
            "unsupported": "",
            "steps": [
                {"action": "rm_rf", "args": {}},
                {"action": "read_screen", "args": {}},
                {"action": "compose", "args": {"instruction": "Summarise", "input": "{{ step 2 }}"}},
            ],
        },
        NOW,
    )
    assert [s.action for s in plan.steps] == ["read_screen", "compose"]
    assert plan.steps[1].args["input"] == "{{step1}}"


def test_forward_references_and_dependencies_on_rejected_steps_fail():
    plan = planner.validate(
        {
            "title": "x",
            "unsupported": "",
            "steps": [
                {"action": "speak", "args": {"text": "{{step2}}"}},
                {"action": "create_event", "args": {"title": "No time"}},
                {"action": "speak", "args": {"text": "{{step2}}"}},
            ],
        },
        NOW,
    )
    assert plan.steps == []
    assert len(plan.errors) == 3


def test_templates_are_stripped_from_scalar_fields():
    plan = planner.validate(
        {"title": "x", "unsupported": "", "steps": [{"action": "create_reminder", "args": {"title": "Break", "when": "{{step2}} in 25 minutes"}}]},
        NOW,
    )
    assert plan.steps[0].args["when"] == "in 25 minutes"


def test_compose_that_pretends_to_send_is_removed():
    plan = planner.validate(
        {"title": "x", "unsupported": "", "steps": [{"action": "compose", "args": {"instruction": "Send a WhatsApp to Rahul", "input": ""}}]},
        NOW,
        goal="WhatsApp Rahul",
    )
    assert plan.steps == []
    assert plan.warnings


def test_compose_kept_when_user_asked_for_a_draft():
    plan = planner.validate(
        {"title": "x", "unsupported": "", "steps": [{"action": "compose", "args": {"instruction": "Draft an email to my professor", "input": ""}}]},
        NOW,
        goal="Draft an email to my professor",
    )
    assert len(plan.steps) == 1


def test_out_of_scope_is_named_when_model_forgets():
    plan = planner.Plan(title="x", steps=[], unsupported="")
    planner.flag_out_of_scope("Make a folder and Telegram Rahul", plan)
    assert "Sending messages" in plan.unsupported


def test_whatsapp_is_no_longer_out_of_scope():
    plan = planner.Plan(title="x", steps=[], unsupported="")
    planner.flag_out_of_scope("WhatsApp Mom that I'll be late", plan)
    assert plan.unsupported == ""


def test_out_of_scope_not_duplicated_when_model_explains_it():
    plan = planner.Plan(title="x", steps=[], unsupported="Sending messages is not possible.")
    planner.flag_out_of_scope("Telegram Mom", plan)
    assert plan.unsupported == "Sending messages is not possible."


def test_compose_feeding_only_a_webpage_is_folded_into_it():
    plan = planner.validate(
        {
            "title": "x",
            "unsupported": "",
            "steps": [
                {"action": "compose", "args": {"instruction": "Write copy", "input": "Case is an offline Mac agent."}},
                {"action": "build_webpage", "args": {"topic": "Case", "details": "{{step1}}"}},
                {"action": "speak", "args": {"text": "{{step2}}"}},
            ],
        },
        NOW,
    )
    assert [s.action for s in plan.steps] == ["build_webpage", "speak"]
    assert plan.steps[0].args["details"] == "Case is an offline Mac agent."
    assert plan.steps[1].args["text"] == "{{step1}}"


def test_compose_used_elsewhere_is_not_folded():
    plan = planner.validate(
        {
            "title": "x",
            "unsupported": "",
            "steps": [
                {"action": "compose", "args": {"instruction": "Write copy", "input": "facts"}},
                {"action": "build_webpage", "args": {"topic": "Case", "details": "{{step1}}"}},
                {"action": "copy_to_clipboard", "args": {"text": "{{step1}}"}},
            ],
        },
        NOW,
    )
    assert [s.action for s in plan.steps] == ["compose", "build_webpage", "copy_to_clipboard"]


def test_schema_has_one_variant_per_action_with_required_args():
    variants = planner.PLAN_SCHEMA["properties"]["steps"]["items"]["anyOf"]
    by_name = {v["properties"]["action"]["enum"][0]: v for v in variants}
    assert set(by_name) == set(planner.PLANNER_CATALOG)
    assert "reply" not in by_name
    event_args = by_name["create_event"]["properties"]["args"]
    assert list(event_args["properties"]) == ["title", "when", "duration_minutes", "notes"]
    assert event_args["required"] == ["title", "when", "duration_minutes"]


def test_whatsapp_step_needs_an_explicit_request_naming_the_contact():
    raw = {
        "title": "x",
        "unsupported": "",
        "steps": [
            {"action": "create_event", "args": {"title": "Lunch", "when": "tomorrow 2pm", "duration_minutes": 30}},
            {"action": "send_whatsapp", "args": {"contact": "Pizza Place", "message": "One large please"}},
        ],
    }
    plan = planner.validate(raw, NOW, "Order a pizza and block 30 minutes tomorrow at 2pm for lunch")
    assert [s.action for s in plan.steps] == ["create_event"]
    assert any("never asked for a WhatsApp message" in w for w in plan.warnings)

    plan = planner.validate(raw, NOW, "WhatsApp Mom that lunch is at 2")
    assert [s.action for s in plan.steps] == ["create_event"]
    assert any("not named in the goal" in w for w in plan.warnings)

    ok = {"title": "x", "unsupported": "", "steps": [{"action": "send_whatsapp", "args": {"to": "Mom", "text": "hi"}}]}
    plan = planner.validate(ok, NOW, "Send hi to mom on WhatsApp")
    assert plan.steps[0].args == {"contact": "Mom", "message": "hi"}
    assert plan.steps[0].risk == "one-way"


def test_open_path_plus_vscode_app_is_repaired_into_open_in_vscode(tmp_path, monkeypatch):
    import actions

    desktop = tmp_path / "Desktop"
    (desktop / "final-year-Project").mkdir(parents=True)
    monkeypatch.setattr(actions, "ALLOWED_ROOTS", {"desktop": desktop, "documents": tmp_path / "Documents", "downloads": tmp_path / "Downloads"})
    raw = {
        "title": "Open project",
        "unsupported": "",
        "steps": [
            {"action": "open_path", "args": {"target": "Desktop/final year project"}},
            {"action": "open_app", "args": {"app": "VS Code"}},
        ],
    }
    plan = planner.validate(raw, NOW, "Open my final year project folder in VS Code")
    assert [s.action for s in plan.steps] == ["open_in_vscode"]
    assert plan.steps[0].args["path"] == str(desktop / "final-year-Project")


def test_pdf_in_the_goal_turns_on_pages_export():
    raw = {"title": "Essay", "unsupported": "", "steps": [{"action": "pages_create", "args": {"title": "Energy", "content": "Text"}}]}
    plan = planner.validate(raw, NOW, "Write an essay in Pages and export it as a PDF")
    assert plan.steps[0].args["export_pdf"] is True
    plan = planner.validate(raw, NOW, "Write an essay in Pages")
    assert plan.steps[0].args["export_pdf"] is False


def test_invented_vscode_path_is_grounded_in_the_users_words(tmp_path, monkeypatch):
    import actions

    desktop = tmp_path / "Desktop"
    (desktop / "final-year-Project").mkdir(parents=True)
    monkeypatch.setattr(actions, "ALLOWED_ROOTS", {"desktop": desktop, "documents": tmp_path / "Documents", "downloads": tmp_path / "Downloads"})
    raw = {"title": "x", "unsupported": "", "steps": [{"action": "open_in_vscode", "args": {"path": "Desktop/MyProject"}}]}
    plan = planner.validate(raw, NOW, "Open my final year project folder in VS Code")
    assert plan.steps[0].args["path"] == str(desktop / "final-year-Project")
    assert any(w.startswith("Grounded:") for w in plan.warnings)


# --------------------------------------------------------------- deterministic fast paths

import asyncio  # noqa: E402

import pytest  # noqa: E402

import intents  # noqa: E402
import llm  # noqa: E402


@pytest.mark.parametrize(
    "goal, contact, message",
    [
        # Logged failures: garbled voice text, a greeting before the request, a rewritten message.
        ("Send hi to Hi on WhatsApp to ridhi", "Ridhi", "Hi"),
        ("hi say hi to jasir on whatsapp", "Jasir", "Hi"),
        ("say i love you to sadaf on whatsapp", "Sadaf", "I love you"),
        ("wish ridhi a very happy diwali on whatsapp", "Ridhi", "Wishing you a very happy Diwali! 🪔"),
        ("whatsapp ridhi saying I'll be late", "Ridhi", "I'll be late"),
        ("tell ridhi on whatsapp to call me", "Ridhi", "Please call me."),
        ('send "welcome to the team!" to Anurag Svit on whatsapp', "Anurag Svit", "welcome to the team!"),
        ("say ridhi good night on whatsapp", "Ridhi", "Good night"),
    ],
)
def test_whatsapp_requests_are_parsed_without_the_model(goal, contact, message):
    intent = intents.parse_whatsapp(goal)
    assert (intent.contact, intent.message) == (contact, message)


def test_written_pieces_are_drafted_and_compound_goals_go_to_the_planner():
    intent = intents.parse_whatsapp("text and send Ridhi an apology letter on whatsapp")
    assert intent.contact == "Ridhi" and intent.message == "" and "an apology letter to Ridhi" in intent.compose
    assert intents.parse_whatsapp("say hi to ridhi on whatsapp and set volume to 20") is None
    assert intents.parse_whatsapp("open whatsapp") is None


def _no_model(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(llm, "chat", fail)


def test_small_talk_gets_an_instant_read_only_reply(monkeypatch):
    _no_model(monkeypatch)
    plan, runs = asyncio.run(planner.make_plan("hi", NOW))
    assert runs == [] and [s.action for s in plan.steps] == ["reply"] and plan.steps[0].risk == "read"
    assert intents.small_talk("Hello, how are you feeling?", NOW).startswith("I'm running smoothly")
    assert intents.small_talk("hi say hi to ridhi on whatsapp", NOW) is None


def test_whatsapp_fast_path_matches_the_real_chat_name(monkeypatch):
    _no_model(monkeypatch)

    async def chats():
        return ["Hi", "Ridhi Sharma", "Ajay"]

    monkeypatch.setattr(planner, "whatsapp_visible_chats", chats)
    plan, runs = asyncio.run(planner.make_plan("say hi to ridhi on whatsapp", NOW))
    assert runs == []
    assert [(s.action, s.args) for s in plan.steps] == [("send_whatsapp", {"contact": "Ridhi Sharma", "message": "Hi"})]
    assert any("Ridhi Sharma" in note for note in plan.notes)


def test_drafted_whatsapp_text_is_cleaned_and_shown_before_approval(monkeypatch):
    async def chats():
        return []

    async def draft(messages, **_kwargs):
        assert "apology" in messages[-1]["content"]
        return llm.LLMResult(text='"I\'m really sorry, Ridhi.\nCan we talk? [Your Name]"', tokens=9, tokens_per_s=9.0, duration_ms=5)

    monkeypatch.setattr(planner, "whatsapp_visible_chats", chats)
    monkeypatch.setattr(llm, "chat", draft)
    plan, runs = asyncio.run(planner.make_plan("text Ridhi an apology letter on whatsapp", NOW))
    assert len(runs) == 1
    assert plan.steps[0].args == {"contact": "Ridhi", "message": "I'm really sorry, Ridhi. Can we talk?"}


@pytest.mark.parametrize(
    "goal, expression, answer",
    [
        ("Use Calculator to work out 128 × 46", "128*46", 5888),
        ("what is 25 times 4 plus 10", "25*4+10", 110),
        ("what's 1,280 divided by 16", "1280/16", 80),
        ("128 into 46 in calculator", "128*46", 5888),
    ],
)
def test_calculations_are_parsed_and_checked_with_normal_precedence(goal, expression, answer):
    assert intents.parse_calculation(goal) == expression
    assert intents.evaluate(expression) == answer


def test_tool_fast_paths_leave_other_requests_to_the_planner():
    assert intents.parse_calculation("Every day at 9am tell me what's on my calendar") is None
    assert intents.parse_press("click the Search button in WhatsApp") == ("Search", "WhatsApp")
    assert intents.parse_describe("describe my WhatsApp screen") == "WhatsApp"
    assert intents.parse_describe("Read my screen and save the key points as a note") is None


def test_calculator_and_describe_plan_instantly_without_approval(monkeypatch):
    _no_model(monkeypatch)
    plan, runs = asyncio.run(planner.make_plan("Use Calculator to work out 128 × 46", NOW))
    assert runs == [] and [(s.action, s.args, s.risk) for s in plan.steps] == [("calculate", {"expression": "128*46"}, "read")]
    plan, _ = asyncio.run(planner.make_plan("Describe my screen", NOW))
    assert [(s.action, s.risk) for s in plan.steps] == [("describe_screen", "read")]
    plan, _ = asyncio.run(planner.make_plan("Press OK in Nonexistent App", NOW))
    assert plan.steps[0].action == "reply" and "No app named" in plan.steps[0].args["text"]


VIVA = ("Set up my viva prep: create a Viva Prep folder on the Desktop with a checklist of 5 things to rehearse, "
        "remind me tomorrow at 9am to rehearse, and block 45 minutes on Thursday at 4pm for a mock viva")


def test_reminder_times_come_from_the_user_and_forgotten_events_are_added():
    raw = {"title": "x", "unsupported": "", "steps": [
        {"action": "create_folder", "args": {"path": "Desktop/Viva Prep"}},
        {"action": "create_reminder", "args": {"title": "Rehearse", "when": "Thursday 2:25pm"}},
    ]}
    plan = planner.validate(raw, NOW, VIVA)
    reminder, event = plan.steps[1], plan.steps[2]
    assert reminder.args["at"] == "2026-11-03T09:00:00"
    assert (event.action, event.args["title"], event.args["duration_minutes"]) == ("create_event", "Mock viva", 45)
    assert any(w.startswith("Grounded") for w in plan.warnings) and any(w.startswith("Added") for w in plan.warnings)


def test_steps_copied_from_the_examples_are_removed():
    raw = {"title": "x", "unsupported": "", "steps": [
        {"action": "create_folder", "args": {"path": "Desktop/Launch"}},
        {"action": "set_volume", "args": {"level": 10}},
        {"action": "create_event", "args": {"title": "Deep work: writing", "when": "tomorrow 2pm", "duration_minutes": 30}},
    ]}
    assert planner.validate(raw, NOW, "Write an essay about AI in Pages").steps == []
    assert [s.action for s in planner.validate(raw, NOW, "set volume to 10 and make a Launch folder on my desktop").steps][:2] == ["create_folder", "set_volume"]


def test_goals_that_are_only_out_of_scope_are_refused_instantly(monkeypatch):
    _no_model(monkeypatch)
    plan, runs = asyncio.run(planner.make_plan("Order a pizza for dinner and email my boss that I'm running late", NOW))
    assert runs == [] and plan.steps == [] and plan.unsupported.startswith("Not allowed")


def test_pages_documents_are_planned_without_the_model(monkeypatch):
    _no_model(monkeypatch)
    plan, runs = asyncio.run(planner.make_plan("Write a one-page essay about why on-device AI agents matter in Pages and export it as a PDF", NOW))
    compose, doc = plan.steps
    assert runs == [] and compose.args["instruction"].startswith("Write a one-page essay about why on-device AI agents matter")
    assert doc.args["title"] == "Why On-device AI Agents Matter" and doc.args["content"] == "{{step1}}" and doc.args["export_pdf"] is True
