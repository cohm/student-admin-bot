"""The programme / admission-year picker in app.js, run in node against small
fake selects (no JS test runner here; see test_markdown_href_escape.py).

A pick that answers the bot's last message is sent once, as the student's
reply; a pick made while an answer streams survives that answer; only the
student's own picks are stored; an emptied picker is sent once as ""; what
the pickers show is what is sent.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from student_bot.config import PROJECT_ROOT

APP_JS = (PROJECT_ROOT / "src" / "student_bot" / "web" / "static" / "app.js").read_text(
    encoding="utf-8"
)

_FUNCTIONS = [
    "restorePick",
    "fillSelect",
    "renderPickers",
    "setProgram",
    "pickerPayload",
    "syncPickers",
    "restoreOwnPicks",
    "pickProgram",
    "pickYear",
    "answerPending",
    "answerOnPick",
]


def _function_source(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    end = APP_JS.index("\n}\n", start) + 2
    return APP_JS[start:end]


def _listeners() -> str:
    start = APP_JS.index('pickCategoryEl.addEventListener("change"')
    end = APP_JS.index("\n", APP_JS.index("answerOnPick(pickYearEl"))
    return APP_JS[start:end]


# A <select> as far as the picker uses it: setting a value with no option
# selects nothing, and truncating the options keeps the first one selected.
_FAKES = """
class Select {
  constructor(values) {
    this.options = values.map((v) => ({ textContent: v, value: v }));
    this.selectedIndex = 0; this.hidden = false; this.handlers = {};
  }
  get length() { return this.options.length; }
  set length(n) { this.options.length = n; if (this.selectedIndex >= n) this.selectedIndex = 0; }
  add(o) { this.options.push(o); }
  get value() { return this.selectedIndex < 0 ? "" : this.options[this.selectedIndex].value; }
  set value(v) { this.selectedIndex = this.options.findIndex((o) => o.value === v); }
  addEventListener(type, f) { this.handlers[type] = f; }
  choose(v) { this.value = v; this.handlers.change(); }
  key(k) { this.handlers.keydown({ key: k, preventDefault() {} }); }
  type(k, v) { this.key(k); this.choose(v); }  // a key that moves the selection
  leave() { this.handlers.blur(); }
  press() { this.handlers.pointerdown(); }
}
function Option(label, value) { return { textContent: label, value }; }
const store = JSON.parse(process.argv[1]);
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const window = { currentLang: () => "sv" };
const sendButton = { disabled: false };
const el = (sel) => ({ "#send": sendButton })[sel];
const pickCategoryEl = new Select([""]);
const pickProgramEl = new Select([""]);
const pickYearEl = new Select(["", "2026", "2025", "2023"]);
const categories = [{ key: "civ", name_sv: "Civ", name_en: "Civ",
  programs: [["CTFYS", "Teknisk fysik", "Engineering Physics"], ["CDATE", "Datateknik", "CS"]] },
  { key: "master", name_sv: "Master", name_en: "Master",
    programs: [["TTFYM", "Teknisk fysik", "Engineering Physics"]] }];
const categoryOf = new Map([["CTFYS", "civ"], ["CDATE", "civ"], ["TTFYM", "master"]]);
const state = { pendingKind: "", pickProgram: store.pick_program || "", pickYear: "",
  emptied: { program: false, year: false } };
state.pickYear = restorePick(pickYearEl, "pick_year");
const sent = []; // [question, picker fields] per sendQuestion
function sendQuestion(q) { sent.push([q, pickerPayload()]); }
const shown = () => ({ program: pickProgramEl.value, category: pickCategoryEl.value,
  year: pickYearEl.value, state: state.pickProgram, stateYear: state.pickYear });
"""


def _run(scenario: str, stored: dict | None = None) -> dict:
    script = (
        _FAKES
        + "".join(_function_source(n) for n in _FUNCTIONS)
        + _listeners()
        + "\nsetProgram(state.pickProgram);\n"
        + "const out = {};\n"
        + scenario
        + "\nout.sent = sent; out.store = store; out.pendingKind = state.pendingKind;"
        + "\nprocess.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps(stored or {})],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_the_picked_year_is_sent_once_as_the_reply():
    out = _run('state.pendingKind = "year"; pickYearEl.choose("2023"); pickYearEl.choose("2025");')
    assert out["sent"] == [["2023", {"program_code": None, "admission_year": "2023"}]]
    assert out["pendingKind"] == ""


@needs_node
def test_the_picked_programme_is_sent_as_the_reply_but_not_its_category():
    out = _run(
        'state.pendingKind = "programme"; pickCategoryEl.choose("civ");'
        "out.afterCategory = sent.length;"
        'pickProgramEl.choose("CTFYS");'
    )
    assert out["afterCategory"] == 0
    assert out["sent"] == [["CTFYS", {"program_code": "CTFYS", "admission_year": None}]]


@needs_node
@pytest.mark.parametrize(
    "scenario",
    [
        'state.pendingKind = "year"; pickCategoryEl.choose("civ"); pickProgramEl.choose("CDATE");',
        'state.pendingKind = "year"; pickYearEl.choose("");',
        'state.pendingKind = "programme"; pickYearEl.choose("2023");',
        'state.pendingKind = ""; pickYearEl.choose("2023");',
        'state.pendingKind = "year"; sendButton.disabled = true; pickYearEl.choose("2023");',
    ],
    ids=["wrong kind", "empty pick", "year for a programme", "nothing waiting", "in flight"],
)
def test_other_picks_send_nothing(scenario):
    assert _run(scenario)["sent"] == []


@needs_node
@pytest.mark.parametrize("commit", ['pickYearEl.key("Enter")', "pickYearEl.leave()"])
def test_a_year_typed_in_the_dropdown_answers_when_the_student_is_done(commit):
    """Typing "2023" selects 2026 on the first key. Only the year the student
    ends on is the reply, on Enter or when they leave the dropdown."""
    out = _run(
        'state.pendingKind = "year";'
        'pickYearEl.type("2", "2026"); pickYearEl.type("3", "2023");'
        "out.before = sent.length; out.taken = state.pickYear;"
        f"{commit}; {commit};"
    )
    assert (out["before"], out["taken"]) == (0, "2023")
    assert out["sent"] == [["2023", {"program_code": None, "admission_year": "2023"}]]


@needs_node
def test_a_programme_typed_in_the_dropdown_answers_when_the_student_is_done():
    out = _run(
        'state.pendingKind = "programme"; pickCategoryEl.choose("civ");'
        'pickProgramEl.type("C", "CDATE"); pickProgramEl.type("T", "CTFYS");'
        "out.before = sent.length; pickProgramEl.leave();"
    )
    assert out["before"] == 0
    assert out["sent"] == [["CTFYS", {"program_code": "CTFYS", "admission_year": None}]]


@needs_node
def test_a_click_after_keys_answers_at_once():
    out = _run(
        'state.pendingKind = "year";'
        'pickYearEl.key("ArrowDown"); pickYearEl.press(); pickYearEl.choose("2025");'
    )
    assert out["sent"] == [["2025", {"program_code": None, "admission_year": "2025"}]]


@needs_node
def test_a_pick_made_while_the_answer_streams_survives_the_answer():
    out = _run(
        "const before = { program: state.pickProgram, year: state.pickYear };"
        'pickProgramEl.choose("CTFYS");'  # while the answer streams
        'syncPickers({ program_code: "CDATE", admission_year: "2023" }, before);'
        "out.shown = shown();",
        {"pick_program": "CDATE", "pick_year": "2025"},
    )
    assert out["shown"]["state"] == "CTFYS" and out["shown"]["program"] == "CTFYS"
    # The year was not touched meanwhile, so it follows the answer.
    assert out["shown"]["stateYear"] == "2023"


@needs_node
def test_a_year_picked_while_the_answer_streams_survives_the_answer():
    out = _run(
        "const before = { program: state.pickProgram, year: state.pickYear };"
        'pickYearEl.choose("2026");'  # while the answer streams
        'syncPickers({ program_code: "CTFYS", admission_year: "2023" }, before);'
        "out.shown = shown();",
        {"pick_year": "2025"},
    )
    assert (out["shown"]["stateYear"], out["shown"]["year"]) == ("2026", "2026")
    assert out["shown"]["state"] == "CTFYS"


@needs_node
def test_a_value_the_pickers_take_from_the_answer_is_shown_and_sent_but_not_stored():
    out = _run(
        'syncPickers({ program_code: "CTFYS", admission_year: "2023" }, { program: "", year: "" });'
        "out.shown = shown(); out.payload = pickerPayload();"
    )
    assert out["shown"] == {
        "program": "CTFYS",
        "category": "civ",
        "year": "2023",
        "state": "CTFYS",
        "stateYear": "2023",
    }
    assert out["payload"] == {"program_code": "CTFYS", "admission_year": "2023"}
    assert out["store"] == {}


@needs_node
def test_a_value_the_pickers_cannot_show_shows_as_none_and_is_not_sent():
    """A programme outside the list, a year before the ones offered: the
    student's own pick would contradict the answer and overwrite it next turn."""
    out = _run(
        'syncPickers({ program_code: "TBASD", admission_year: "2015" },'
        " { program: state.pickProgram, year: state.pickYear });"
        "out.shown = shown(); out.payload = pickerPayload();",
        {"pick_program": "CDATE", "pick_year": "2025"},
    )
    assert (out["shown"]["program"], out["shown"]["year"]) == ("", "")
    assert out["payload"] == {"program_code": None, "admission_year": None}
    assert out["store"] == {"pick_program": "CDATE", "pick_year": "2025"}


@needs_node
def test_an_answer_before_the_programme_list_has_loaded_leaves_the_stored_pick():
    """No list yet: every code looks unlisted, and the stored pick must still
    be there when the list arrives."""
    out = _run(
        "categoryOf.clear();"
        'syncPickers({ program_code: "CTFYS" }, { program: state.pickProgram, year: "" });'
        "out.shown = shown();",
        {"pick_program": "CDATE"},
    )
    assert out["shown"]["state"] == "CDATE"


@needs_node
def test_the_students_own_picks_are_stored():
    out = _run(
        'pickCategoryEl.choose("civ"); pickProgramEl.choose("CTFYS"); pickYearEl.choose("2023");'
    )
    assert out["store"] == {"pick_program": "CTFYS", "pick_year": "2023"}


@needs_node
def test_another_category_shows_no_programme_and_stops_sending_the_old_one():
    out = _run(
        'pickCategoryEl.choose("master"); out.shown = shown();'
        "out.first = pickerPayload(); out.second = pickerPayload();",
        {"pick_program": "CDATE"},
    )
    assert out["shown"]["category"] == "master"
    assert (out["shown"]["program"], out["shown"]["state"]) == ("", "")
    assert (out["first"]["program_code"], out["second"]["program_code"]) == ("", None)
    assert out["store"] == {"pick_program": ""}


@needs_node
def test_looking_through_the_categories_with_none_picked_clears_nothing():
    """The bot may remember a programme the pickers do not show."""
    out = _run(
        'pickCategoryEl.choose("master"); pickCategoryEl.choose("");out.payload = pickerPayload();'
    )
    assert out["payload"] == {"program_code": None, "admission_year": None}


@needs_node
def test_starting_over_brings_back_the_students_own_picks_and_waits_for_nothing():
    out = _run(
        'syncPickers({ program_code: "CTFYS", admission_year: "2023", pending_kind: "year" },'
        " { program: state.pickProgram, year: state.pickYear });"
        'state.pendingKind = "year"; state.emptied.year = true;'
        "restoreOwnPicks();"
        "out.shown = shown(); out.payload = pickerPayload();"
        'pickYearEl.choose("2026");',
        {"pick_program": "CDATE"},
    )
    assert (out["shown"]["state"], out["shown"]["program"], out["shown"]["year"]) == (
        "CDATE",
        "CDATE",
        "",
    )
    assert out["payload"] == {"program_code": "CDATE", "admission_year": None}
    assert out["sent"] == []


@needs_node
@pytest.mark.parametrize("emptied", ['pickCategoryEl.choose("")', 'pickProgramEl.choose("")'])
def test_an_emptied_picker_is_sent_once_as_empty(emptied):
    """The empty category, or the programme list's own empty choice."""
    out = _run(
        f'{emptied}; pickYearEl.choose("");'
        "out.first = pickerPayload(); out.second = pickerPayload();",
        {"pick_program": "CDATE", "pick_year": "2025"},
    )
    assert out["first"] == {"program_code": "", "admission_year": ""}
    assert out["second"] == {"program_code": None, "admission_year": None}
    assert out["store"] == {"pick_program": "", "pick_year": ""}


@needs_node
def test_a_code_the_list_does_not_have_is_not_sent():
    out = _run("out.payload = pickerPayload(); out.shown = shown();", {"pick_program": "ZZZZZ"})
    assert out["payload"] == {"program_code": None, "admission_year": None}
    assert out["shown"]["program"] == ""


@needs_node
def test_a_turn_waiting_for_the_programme_lets_the_shown_one_answer_it():
    """The ambiguous-programme list can come back with the picked programme
    among the choices; picking it again must still send it."""
    out = _run(
        'const before = { program: "CTFYS", year: "" };'
        'syncPickers({ pending_kind: "programme" }, before); state.pendingKind = "programme";'
        "out.payload = pickerPayload();"
        'pickCategoryEl.choose("master"); pickCategoryEl.choose("civ");'  # looking for it
        "out.stored = store.pick_program; out.looked = pickerPayload();"
        'pickProgramEl.choose("CTFYS");',
        {"pick_program": "CTFYS"},
    )
    assert out["payload"] == {"program_code": None, "admission_year": None}
    # Looking through the categories un-picks nothing: the page blanked the picker.
    assert out["stored"] == "CTFYS"
    assert out["looked"] == {"program_code": None, "admission_year": None}
    assert out["sent"] == [["CTFYS", {"program_code": "CTFYS", "admission_year": None}]]


@needs_node
def test_a_turn_waiting_for_the_programme_keeps_a_pick_made_while_it_streamed():
    out = _run(
        'const before = { program: "CTFYS", year: "" };'
        'pickProgramEl.choose("CDATE");'  # while the answer streams
        'syncPickers({ pending_kind: "programme" }, before);'
        "out.shown = shown();",
        {"pick_program": "CTFYS"},
    )
    assert (out["shown"]["program"], out["shown"]["state"]) == ("CDATE", "CDATE")
