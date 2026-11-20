import pytest

import actions
from actions import ArgError, resolve_user_path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    mapping = {name: home / name.title() for name in ("desktop", "documents", "downloads")}
    for path in mapping.values():
        path.mkdir(parents=True)
    monkeypatch.setattr(actions, "HOME", home)
    monkeypatch.setattr(actions, "ALLOWED_ROOTS", mapping)
    return mapping


def test_root_aliases(roots):
    assert resolve_user_path("Desktop/Viva Prep", []) == roots["desktop"] / "Viva Prep"
    assert resolve_user_path("docs/notes.md", []) == roots["documents"] / "notes.md"
    assert resolve_user_path("~/Downloads/x.txt", []) == roots["downloads"] / "x.txt"


def test_bare_names_default_to_desktop(roots):
    assert resolve_user_path("Launch", []) == roots["desktop"] / "Launch"


def test_relative_to_folder_created_earlier(roots):
    folder = roots["documents"] / "Viva Prep"
    assert resolve_user_path("Viva Prep/checklist.md", [folder]) == folder / "checklist.md"


@pytest.mark.parametrize("path", ["/etc/passwd", "Desktop/../../secret", "~/.ssh/id_rsa", "/tmp/x", "Desktop"])
def test_refuses_paths_outside_allowed_roots(roots, path):
    with pytest.raises(ArgError):
        resolve_user_path(path, [])


def test_refuses_symlink_escape(roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (roots["desktop"] / "link").symlink_to(outside)
    with pytest.raises(ArgError):
        resolve_user_path("Desktop/link/file.md", [])


def test_write_file_normalize_adds_extension(roots):
    ctx = actions.PlanContext(now=None)  # type: ignore[arg-type]
    args = actions.WriteFile().normalize({"path": "Desktop/plan", "content": "hello"}, ctx)
    assert args["path"].endswith("plan.md")


def test_note_html_escapes_and_builds_lists():
    html = actions._note_html("<b>Title</b>", "- one\n- two & three\nplain")
    assert "&lt;b&gt;Title&lt;/b&gt;" in html
    assert "<ul><li>one</li><li>two &amp; three</li></ul>" in html
    assert "<div>plain</div>" in html


def test_plain_strips_markdown_and_placeholders():
    assert actions._plain("- **Mission Planning**") == "Mission Planning"
    assert actions._plain("[Back to Home]") == ""
    assert actions._plain("See [docs](https://x.y) now") == "See now"
    assert actions._plain("## Heading") == "Heading"


def test_locate_finds_existing_folder_despite_spelling(roots):
    project = roots["desktop"] / "final-year-Project"
    (project / "code-base").mkdir(parents=True)
    assert actions.locate_user_path("Desktop/final year project", []) == project
    assert actions.locate_user_path("FinalYearProject", []) == project
    assert actions.locate_user_path("code base", []) == project / "code-base"


def test_locate_keeps_unknown_paths_and_stays_inside_roots(roots, tmp_path):
    assert actions.locate_user_path("Desktop/Nothing Like This", []) == roots["desktop"] / "Nothing Like This"
    outside = tmp_path / "outside" / "secret-project"
    outside.mkdir(parents=True)
    (roots["documents"] / "secret-project").symlink_to(outside)
    with pytest.raises(ArgError):
        actions.locate_user_path("Desktop/secret project", [])


def test_pages_body_marks_headings_and_bullets():
    body, headings = actions.pages_body("Viva Notes", "# Intro\nWhy it matters\n\n- one\n* two\n## Plan")
    assert body.split("\n") == ["Viva Notes", "Intro", "Why it matters", "", "• one", "• two", "Plan"]
    assert headings == [2, 7]


def test_pages_create_defaults_to_documents(roots):
    ctx = actions.PlanContext(now=None)  # type: ignore[arg-type]
    action = actions.PagesCreate()
    args = action.normalize(action.canonical_args({"title": "AI Essay", "content": "Text", "pdf": "yes"}), ctx)
    assert args["path"] == str(roots["documents"] / "AI Essay.pages")
    assert args["export_pdf"] is True


def _node(i, role, label):
    return {"i": i, "role": role, "label": label, "value": "", "focused": False}


def test_whatsapp_header_must_name_the_exact_contact():
    nodes = [
        _node(0, "AXButton", "Hi"),
        _node(1, "AXButton", "\u200eOpen call dropdown menu with Hi"),
        _node(2, "AXTextArea", "\u200eCompose message"),
    ]
    assert actions.whatsapp_header_matches(nodes, "hi")
    assert not actions.whatsapp_header_matches(nodes, "Hiba")
    other = [_node(0, "AXButton", "\u200eStart voice call with Ridhi")]
    assert not actions.whatsapp_header_matches(other, "Hi")


def test_whatsapp_sent_counts_only_our_bubbles_in_the_open_chat():
    nodes = [
        _node(0, "AXStaticText", "Your message, hi, 1:00 pm (chat list preview)"),
        _node(1, "AXButton", "\u200eOpen call dropdown menu with Hi"),
        _node(2, "AXStaticText", "\u200eYour photo, 12:01 pm, \u200eSent to Hi, \u200eRead"),
        _node(3, "AXStaticText", "\u200eYour message, hi, 1:13 pm, \u200eSent to Hi, \u200eDelivered"),
        _node(4, "AXStaticText", "\u200eYour message, hi there, 1:14 pm, \u200eSent to Hi, \u200eSent"),
        _node(5, "AXTextArea", "Compose message"),
    ]
    assert actions.whatsapp_sent(nodes, "hi") == (1, "delivered")
    assert actions.whatsapp_sent(nodes, "hi there") == (1, "sent")
    assert actions.whatsapp_sent(nodes[:1], "hi") == (0, "")


def test_send_whatsapp_is_one_way_and_strips_quotes():
    action = actions.CATALOG["send_whatsapp"]
    assert action.risk == actions.ONE_WAY
    args = action.normalize(action.canonical_args({"to": "“Mom”", "text": "On my way"}), actions.PlanContext(now=None))  # type: ignore[arg-type]
    assert args == {"contact": "Mom", "message": "On my way"}


def test_whatsapp_suggests_close_chat_names_but_never_picks_one():
    nodes = [_node(0, "AXButton", n) for n in ("\u200eNew Chat", "Jaasiir.", "Samnan", "Hi", "\u200eStart voice call with Hi")]
    assert actions.whatsapp_suggestions(nodes, "Jasir") == "“Jaasiir.”"
    assert actions.whatsapp_suggestions(nodes, "Zebra") == ""


def test_header_match_ignores_edge_punctuation_only():
    nodes = [_node(0, "AXButton", "\u200eOpen call dropdown menu with Jaasiir.")]
    assert actions.whatsapp_header_matches(nodes, "Jaasiir")
    assert actions.whatsapp_header_matches(nodes, "jaasiir.")
    assert not actions.whatsapp_header_matches(nodes, "Jasir")


def test_locate_never_swaps_in_scripts_or_paths_the_plan_creates(roots):
    (roots["downloads"] / "Launch.command").write_text("#!/bin/sh\n")
    (roots["desktop"] / "old-notes.md").write_text("old")
    ctx = actions.PlanContext(now=None)  # type: ignore[arg-type]
    actions.CreateFolder().normalize({"path": "Desktop/Launch"}, ctx)
    opened = actions.OpenPath().normalize({"target": "Desktop/Launch"}, ctx)
    assert opened["target"] == str(roots["desktop"] / "Launch")
    actions.WriteFile().normalize({"path": "Desktop/notes.md", "content": "x"}, ctx)
    opened = actions.OpenPath().normalize({"target": "Desktop/notes.md"}, ctx)
    assert opened["target"] == str(roots["desktop"] / "notes.md")
    # A fresh plan with no matching folder never falls back to the script.
    assert actions.locate_user_path("Launch", []) == roots["desktop"] / "Launch"


def test_open_path_refuses_scripts_and_approved_paths_are_not_researched(roots):
    script = roots["desktop"] / "run.command"
    script.write_text("#!/bin/sh\n")
    with pytest.raises(ArgError):
        actions.OpenPath().normalize({"target": str(script)}, actions.PlanContext(now=None))  # type: ignore[arg-type]
    (roots["desktop"] / "final-year-Project").mkdir()
    approved = str(roots["desktop"] / "final year project")
    assert actions.locate_user_path(approved, []) == roots["desktop"] / "final year project"


def test_suggestions_use_row_names_and_skip_search_filters():
    nodes = [
        _node(0, "AXButton", "Ridhi Sharma, 2 unread messages, Pinned"),
        _node(1, "AXButton", "\u200eFrom Ridhi"),
        _node(2, "AXButton", "Samnan"),
    ]
    assert actions.whatsapp_suggestions(nodes, "Ridhi Sharmaa") == "“Ridhi Sharma”"
    assert "From" not in actions.whatsapp_suggestions(nodes, "Ridhi")


def test_pick_chat_takes_exact_or_the_only_prefix_match_never_a_guess():
    names = ["Hi", "Ridhi Sharma", "Ajay", "Ajay Kumar"]
    assert actions.pick_chat("ridhi", names) == "Ridhi Sharma"
    assert actions.pick_chat("Ajay", names) == "Ajay"
    assert actions.pick_chat("aj", names) is None
    assert actions.pick_chat("Ridhi", names + ["Ridhi Verma"]) is None
    nodes = [_node(0, "AXButton", "From Ridhi"), _node(1, "AXButton", "Ridhi, 2 unread messages"), _node(2, "AXButton", "New Chat")]
    assert actions.whatsapp_chat_names(nodes) == ["Ridhi"]


def test_calculator_keys_and_display_reading():
    assert actions.calc_keys("12.5*-3") == ["All Clear|Clear", "1", "2", "Point|Decimal", "5", "Multiply", "Subtract", "3", "Equals"]
    assert actions.display_number("5,888") == 5888
    assert actions.display_number("\u221212.5") == -12.5
    assert actions.display_number("Error") is None


def test_compose_output_is_plain_text_for_notes_pages_and_speech():
    assert actions.tidy_markdown("*   **User Interface**: menus\n• item\n# Heading") == "- User Interface: menus\n- item\n# Heading"
