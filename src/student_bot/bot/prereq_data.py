"""Curated KTH study-plan prerequisite data, as a third retrieval source
alongside Chroma (`retrieval.py`) and the live web fetch (`web_retrieval.py`).

`data/studieplaner/` is a one-time, manually maintained copy of the curated
JSON from the (separate) ProgramVisualization project. Its `programs.json`
already splits the *Särskild behörighet* free text into two typed lists per
course — `prerequisitesCompleted` ("slutförd kurs krävs innan") and
`prerequisitesParticipation` ("aktivt deltagande räcker, kan gå parallellt")
— and filters out alternative course codes that aren't part of the given
programme. This module only reads that data; how it is produced or kept
up to date is out of scope here.

Deliberately does NOT try to guess which course or programme a question is
about (v1 of this module did, via a course-code regex plus a jargon
dictionary, and that approach was abandoned — see the v2 implementation
plan). By the time `pipeline.py` calls into this module, the programme
(and, where it matters, the admission cohort) have already been pinned down
by the clarification flow in `web_retrieval.py`; this module just reads that
programme+cohort's curated file and hands back either one course's data
(`course_data`) or the whole (optionally year-filtered) roster
(`roster_chunk`). The LLM does the work of matching the student's own
wording ("diff och trans", "flervarren") against the roster it's given —
no server-side nickname matching lives here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from student_bot.bot.retrieval import RetrievedChunk
from student_bot.config import Config

log = logging.getLogger("student_bot")

# Bound on how many courses go into a single programme/year roster chunk
# (see `roster_chunk`). TIEMM year 1 alone lists 88 courses (a master's
# programme's whole elective pool tagged year 1) — past this, truncate
# rather than risk crowding out everything else in the prompt.
_MAX_ROSTER_COURSES = 60

_COURSE_PAGE_URL = "https://www.kth.se/student/kurser/kurs/{code}"


@dataclass
class CoursePrereqInfo:
    code: str
    name: str
    name_en: str
    prerequisites_completed: list[str] = field(default_factory=list)
    prerequisites_participation: list[str] = field(default_factory=list)
    # Reverse lookup, computed, in-programme only. Split the same way as the
    # forward direction: a downstream course that only needs concurrent
    # participation in this one (e.g. SK1104 ← SF1674) is NOT blocked on
    # having completed it, so it must not be reported the same way as one
    # that needs it completed first (e.g. SF1683 ← SF1674). Conflating the
    # two under one "required for" list said SF1674 had to be completed
    # before SK1104, which is false — SK1104 only needs concurrent
    # enrollment in it.
    required_for_completed: list[str] = field(default_factory=list)
    required_for_participation: list[str] = field(default_factory=list)
    program: str = ""
    year: int | None = None
    verified: bool = False
    source_file: str = ""  # relative to data_dir, for debugging/citation


def _data_dir(cfg: Config) -> Path:
    return cfg.absolute(Path(cfg.prereq_data.data_dir))


@lru_cache(maxsize=1)
def _registry(data_dir_str: str) -> dict[str, dict]:
    """{PROGRAM_CODE: entry} from `programs.json`. Cached: this file never
    changes while the bot is running (see module docstring)."""
    path = Path(data_dir_str) / "programs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("prereq_data: failed to read %s: %s", path, e)
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict] = {}
    for entry in raw:
        if isinstance(entry, dict) and entry.get("code"):
            out[str(entry["code"]).upper()] = entry
    return out


def known_program_codes(cfg: Config) -> list[str]:
    """All programme codes the studieplan data set knows about."""
    return sorted(_registry(str(_data_dir(cfg))))


def cohort_data_exists(cfg: Config, program_code: str, cohort: str) -> bool:
    """True if a cohort-specific file exists for this programme+admission
    year (e.g. CTFYS-HT2023.json). Used to tell an outdated/out-of-range
    admission year apart from one we simply haven't been told yet — see
    Designbeslut punkt 3 in the v2 plan."""
    path = _data_dir(cfg) / "cohorts" / f"{program_code.upper()}-HT{cohort}.json"
    return path.exists()


def _program_file_path(data_dir: Path, program_code: str, cohort: str | None) -> Path | None:
    if cohort:
        cohort_file = data_dir / "cohorts" / f"{program_code}-HT{cohort}.json"
        if cohort_file.exists():
            return cohort_file
    entry = _registry(str(data_dir)).get(program_code)
    if entry and entry.get("dataFile"):
        candidate = data_dir / str(entry["dataFile"])
        if candidate.exists():
            return candidate
    # Defensive fallback if the registry is missing or stale: prefer the
    # curated top-level file, else the newest cohort archive.
    curated = data_dir / f"{program_code}.json"
    if curated.exists():
        return curated
    cohort_files = sorted((data_dir / "cohorts").glob(f"{program_code}-HT*.json"))
    return cohort_files[-1] if cohort_files else None


def _course_entries(raw: list) -> list[dict]:
    # Skips optionGroup-style entries (e.g. "Kandidatexamensarbete"), which
    # have no `code` of their own and no prerequisites to report.
    return [c for c in raw if isinstance(c, dict) and c.get("code")]


def _prereqs_of(course: dict) -> tuple[list[str], list[str]]:
    # A course carries either the typed pair, or — for the handful not yet
    # migrated — the older untyped `prerequisites`, which the ProgramVisualization
    # convention treats as "completed" (see its tooltipText.ts).
    completed = course.get("prerequisitesCompleted")
    if completed is None:
        completed = course.get("prerequisites") or []
    participation = course.get("prerequisitesParticipation") or []
    return list(completed), list(participation)


def _build_infos(
    raw: list, program_code: str, source_file: str, verified: bool
) -> list[CoursePrereqInfo]:
    courses = _course_entries(raw)
    prereqs_by_code = {c["code"]: _prereqs_of(c) for c in courses}
    # Kept separate by the *downstream* course's requirement type — see the
    # CoursePrereqInfo field comment for why conflating them is wrong.
    required_for_completed: dict[str, list[str]] = {code: [] for code in prereqs_by_code}
    required_for_participation: dict[str, list[str]] = {code: [] for code in prereqs_by_code}
    for code, (completed, participation) in prereqs_by_code.items():
        for dep in completed:
            if dep in required_for_completed and code not in required_for_completed[dep]:
                required_for_completed[dep].append(code)
        for dep in participation:
            if dep in required_for_participation and code not in required_for_participation[dep]:
                required_for_participation[dep].append(code)

    infos = []
    for c in courses:
        code = c["code"]
        completed, participation = prereqs_by_code[code]
        year_raw = c.get("year")
        year = int(year_raw) if isinstance(year_raw, int) else None
        infos.append(
            CoursePrereqInfo(
                code=code,
                name=str(c.get("name", "")),
                name_en=str(c.get("nameEn", "")),
                prerequisites_completed=completed,
                prerequisites_participation=participation,
                required_for_completed=sorted(required_for_completed[code]),
                required_for_participation=sorted(required_for_participation[code]),
                program=program_code,
                year=year,
                verified=verified,
                source_file=source_file,
            )
        )
    return infos


@lru_cache(maxsize=64)
def _load_program_data_cached(
    data_dir_str: str, program_code: str, cohort: str | None
) -> tuple[CoursePrereqInfo, ...]:
    data_dir = Path(data_dir_str)
    path = _program_file_path(data_dir, program_code, cohort)
    if path is None:
        log.info("prereq_data: no studieplan data for program %s", program_code)
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("prereq_data: failed to read %s: %s", path, e)
        return ()
    if not isinstance(raw, list):
        return ()
    verified = bool(_registry(data_dir_str).get(program_code, {}).get("verified", False))
    return tuple(_build_infos(raw, program_code, str(path.relative_to(data_dir)), verified))


def _load_program_data(
    cfg: Config, program_code: str, cohort: str | None = None
) -> list[CoursePrereqInfo]:
    return list(_load_program_data_cached(str(_data_dir(cfg)), program_code.upper(), cohort))


def course_data(
    cfg: Config, program_code: str, course_code: str, cohort: str | None = None
) -> CoursePrereqInfo | None:
    """One named course's curated prerequisite data within a programme
    (+ admission cohort, once that's known) — for "vad krävs för SF1673?"
    shaped questions about a single, explicitly named course. Program and
    cohort are expected to already be resolved by the clarification flow;
    this does no course-name matching of its own."""
    if not cfg.prereq_data.enabled:
        return None
    code = course_code.upper()
    for info in _load_program_data(cfg, program_code, cohort):
        if info.code == code:
            return info
    return None


def _format_course_list(codes: list[str]) -> str:
    return ", ".join(codes)


def _chunk_text(info: CoursePrereqInfo, lang: str) -> str:
    name = info.name_en if lang == "en" and info.name_en else info.name
    parts = [f"{info.code} {name} ({info.program})."]

    if lang == "en":
        if info.prerequisites_completed:
            parts.append(
                f"Requires completed course: {_format_course_list(info.prerequisites_completed)}."
            )
        if info.prerequisites_participation:
            parts.append(
                "Requires concurrent enrollment in, NOT completion of: "
                f"{_format_course_list(info.prerequisites_participation)}. "
                "These may be studied at the same time as this course, so not "
                "yet having passed them does NOT block enrollment in this course."
            )
        if not info.prerequisites_completed and not info.prerequisites_participation:
            parts.append("No prerequisites beyond general entry requirements.")
        # Each direction gets its own explicit sentence, even when empty —
        # otherwise the model has to *infer* "no hard block" from the
        # absence of a sentence, rather than being told it outright.
        # Regression: a question like "which courses can't I take if I
        # haven't passed X?" got refused ("not in the provided context")
        # for a course whose required_for_participation was non-empty but
        # required_for_completed was empty, because nothing in the chunk
        # said so explicitly — the model didn't reliably conclude "not
        # blocking" from silence.
        if not info.required_for_completed and not info.required_for_participation:
            parts.append(f"Not a prerequisite for any later course in {info.program}.")
        else:
            if info.required_for_completed:
                parts.append(
                    f"Must be completed before (hard prerequisite for): "
                    f"{_format_course_list(info.required_for_completed)}."
                )
            else:
                parts.append(
                    f"Not required as a completed prerequisite for any course in {info.program}."
                )
            if info.required_for_participation:
                parts.append(
                    "Must only be studied at the same time as (does NOT need to "
                    "be completed first for): "
                    f"{_format_course_list(info.required_for_participation)}."
                )
            else:
                parts.append(f"Not required as concurrent study for any course in {info.program}.")
        if not info.verified:
            parts.append(
                f"Note: the {info.program} study plan data has not yet been "
                "verified by the programme director — treat with caution."
            )
    else:
        if info.prerequisites_completed:
            parts.append(
                f"Kräver avklarad kurs: {_format_course_list(info.prerequisites_completed)}."
            )
        if info.prerequisites_participation:
            parts.append(
                "Kräver att man läser samtidigt (INTE att man avklarat) "
                f"{_format_course_list(info.prerequisites_participation)}. "
                "Dessa får läsas parallellt med denna kurs, så det är inget "
                "hinder att ännu inte ha klarat dem."
            )
        if not info.prerequisites_completed and not info.prerequisites_participation:
            parts.append("Inga förkunskapskrav utöver grundläggande behörighet.")
        # Se motsvarande kommentar i den engelska grenen ovan: varje
        # riktning får sin egen explicita mening, även när den är tom.
        if not info.required_for_completed and not info.required_for_participation:
            parts.append(f"Krävs inte för någon senare kurs i {info.program}.")
        else:
            if info.required_for_completed:
                parts.append(
                    "Måste vara avklarad innan (hårt krav för): "
                    f"{_format_course_list(info.required_for_completed)}."
                )
            else:
                parts.append(f"Krävs inte som avklarad förkunskap för någon kurs i {info.program}.")
            if info.required_for_participation:
                parts.append(
                    "Behöver bara läsas samtidigt med (behöver INTE vara avklarad "
                    "innan) "
                    f"{_format_course_list(info.required_for_participation)}."
                )
            else:
                parts.append(f"Krävs inte som samtidig läsning för någon kurs i {info.program}.")
        if not info.verified:
            parts.append(
                f"Obs: studieplansdatan för {info.program} är ännu inte verifierad "
                "av programansvarig — använd med försiktighet."
            )

    return " ".join(parts)


def build_chunks(infos: list[CoursePrereqInfo], lang: str) -> list[RetrievedChunk]:
    """One `RetrievedChunk` per course, formatted in natural language — for
    `course_data` results (a specific, explicitly named course)."""
    section = "Prerequisites" if lang == "en" else "Förkunskapskrav"
    chunks = []
    for info in infos:
        name = info.name_en if lang == "en" and info.name_en else info.name
        chunks.append(
            RetrievedChunk(
                chunk_id=f"studieplan:{info.program}:{info.code}",
                text=_chunk_text(info, lang),
                rel_source=f"studieplan:{info.program}",
                doc_title=f"{info.code} {name}".strip(),
                doc_type="studieplan",
                language=lang,
                section_path=section,
                chunk_index=0,
                chroma_distance=0.0,
                source_url=_COURSE_PAGE_URL.format(code=info.code),
                is_stale=False,
            )
        )
    return chunks


def roster_chunk(
    cfg: Config,
    program_code: str,
    lang: str,
    year: int | None = None,
    cohort: str | None = None,
) -> RetrievedChunk | None:
    """One compact chunk listing every course's prerequisites for a whole
    programme (optionally filtered to one study year, ÅK1/2/3) — for "vilka
    kurser kan/kan inte jag läsa"-shaped questions, which need the WHOLE
    roster at once so the LLM can match the student's own wording against
    it, rather than a lookup of one named course (see `course_data`).
    Program (and, once it matters, admission cohort) are expected to
    already be resolved by the clarification flow."""
    if not cfg.prereq_data.enabled:
        return None
    infos = _load_program_data(cfg, program_code, cohort)
    if year is not None:
        infos = [i for i in infos if i.year == year]
    infos = infos[:_MAX_ROSTER_COURSES]
    if not infos:
        return None

    lines = []
    for info in sorted(infos, key=lambda i: (i.year or 0, i.code)):
        name = info.name_en if lang == "en" and info.name_en else info.name
        bits = []
        if info.prerequisites_completed:
            label = "requires COMPLETED, blocking" if lang == "en" else "kräver AVKLARAD, hindrande"
            bits.append(f"{label} {_format_course_list(info.prerequisites_completed)}")
        if info.prerequisites_participation:
            label = (
                "requires concurrent study, NOT completed, does not block"
                if lang == "en"
                else "kräver samtidig läsning, EJ avklarad, hindrar inte"
            )
            bits.append(f"{label} {_format_course_list(info.prerequisites_participation)}")
        if not bits:
            bits.append("no prerequisites" if lang == "en" else "inga förkunskapskrav")
        req = "; ".join(bits)
        year_tag = f" (ÅK{info.year})" if info.year and year is None else ""
        lines.append(f"{info.code} {name}{year_tag}: {req}.")

    year_suffix = f" årskurs {year}" if year else ""
    if lang == "en":
        header = (
            f"{program_code}{year_suffix} — courses and prerequisites. "
            "'Requires COMPLETED' blocks enrollment until passed. 'Requires "
            "concurrent study' does NOT block enrollment — the listed course "
            "may still be in progress, not yet passed."
        )
    else:
        header = (
            f"{program_code}{year_suffix} — kurser och förkunskapskrav. "
            "'Kräver AVKLARAD' hindrar tills kursen är godkänd. 'Kräver samtidig "
            "läsning' hindrar INTE — den listade kursen får fortfarande vara "
            "pågående, inte godkänd än."
        )
    caveat = ""
    if not infos[0].verified:
        caveat = (
            "\n(Not yet verified by the programme director — treat with caution.)"
            if lang == "en"
            else "\n(Ej verifierad av programansvarig — använd med försiktighet.)"
        )
    text = header + "\n" + "\n".join(lines) + caveat

    if lang == "en":
        section = f"Year {year}" if year else "Programme overview"
    else:
        section = f"Årskurs {year}" if year else "Programöversikt"

    return RetrievedChunk(
        chunk_id=f"studieplan-roster:{program_code}:{year or 'all'}:{cohort or 'current'}",
        text=text,
        rel_source=f"studieplan:{program_code}",
        doc_title=f"{program_code}{f' ÅK{year}' if year else ''}".strip(),
        doc_type="studieplan",
        language=lang,
        section_path=section,
        chunk_index=0,
        chroma_distance=0.0,
        source_url=f"https://www.kth.se/student/kurser/program/{program_code}",
        is_stale=False,
    )


__all__ = [
    "CoursePrereqInfo",
    "known_program_codes",
    "cohort_data_exists",
    "course_data",
    "build_chunks",
    "roster_chunk",
]
