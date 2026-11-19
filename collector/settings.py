"""Parse the `Job Search Settings` section of candidate-profile.md."""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# label (lowercase, parenthetical removed) -> attribute
LABELS = {
    "target titles": "target_titles",
    "exclude titles": "exclude_titles",
    "locations": "locations",
    "remote in": "remote_in",
    "work model": "work_model",
    "employment type": "employment_type",
    "salary floor": "salary_floor",
    "must-have keywords": "must_have",
    "dealbreaker keywords": "dealbreakers",
    "excluded companies": "excluded_companies",
    "target companies": "target_companies",
    "base resume": "base_resume",
    "email cutoff": "email_cutoff",
}
SYSTEMS = {"greenhouse", "lever", "ashby"}


@dataclass
class TargetCompany:
    name: str
    system: str
    board: str


@dataclass
class Settings:
    target_titles: list = field(default_factory=list)
    exclude_titles: list = field(default_factory=list)
    locations: list = field(default_factory=list)
    remote_in: list = field(default_factory=list)
    work_model: list = field(default_factory=list)
    employment_type: list = field(default_factory=list)
    salary_floor: float | None = None
    must_have: list = field(default_factory=list)
    dealbreakers: list = field(default_factory=list)
    excluded_companies: list = field(default_factory=list)
    target_companies: list = field(default_factory=list)
    base_resume: str | None = None  # parsed, unused in Phase 1
    email_cutoff: float | None = None

    def filters_off(self) -> list[str]:
        """Settings left blank, i.e. filters/features that are switched off."""
        names = {v: k for k, v in LABELS.items()}
        return [names[a] for a in LABELS.values()
                if a != "base_resume" and getattr(self, a) in (None, [], "")]


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _split(value: str, sep: str = ",") -> list[str]:
    return [p.strip() for p in value.split(sep) if p.strip()]


def _number(label: str, value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "").replace("$", ""))
    except ValueError:
        log.warning("Setting %r: %r is not a number, ignoring", label, value)
        return None


def extract_section(text: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lstrip("#").strip().rstrip(":").lower() == "job search settings":
            section = []
            for nxt in lines[i + 1:]:
                if _is_heading(nxt):
                    break
                section.append(nxt)
            return section
    raise ValueError("No 'Job Search Settings' heading found in profile")


def parse(text: str) -> Settings:
    s = Settings()
    for raw in extract_section(text):
        line = raw.strip().lstrip("-*").strip()
        if not line:
            continue
        if ":" not in line:
            log.warning("Settings line without 'Label:' ignored: %r", line)
            continue
        label, value = line.split(":", 1)
        key = re.sub(r"\(.*?\)", "", label).strip().lower()
        key = re.sub(r"\s+", " ", key)
        value = value.strip()
        attr = LABELS.get(key)
        if attr is None:
            log.warning("Unknown setting label %r ignored", label.strip())
            continue
        if attr == "salary_floor" or attr == "email_cutoff":
            setattr(s, attr, _number(label, value))
        elif attr == "base_resume":
            s.base_resume = value or None
        elif attr == "target_companies":
            for entry in _split(value, ";"):
                parts = [p.strip() for p in entry.split("|")]
                if len(parts) != 3 or not all(parts) or parts[1].lower() not in SYSTEMS:
                    log.warning("Target company %r ignored: expected 'Name | greenhouse|lever|ashby | board'", entry)
                    continue
                s.target_companies.append(TargetCompany(parts[0], parts[1].lower(), parts[2]))
        else:
            setattr(s, attr, _split(value))
    return s


def set_value(text: str, attr: str, value: str) -> str:
    """Rewrite one setting's value inside the Job Search Settings section, keeping its label.
    Appends the line at the end of the section if it's missing."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines)
                 if l.strip().lstrip("#").strip().rstrip(":").lower() == "job search settings")
    end = next((i for i in range(start + 1, len(lines)) if _is_heading(lines[i])), len(lines))
    for i in range(start + 1, end):
        line = lines[i].strip().lstrip("-*").strip()
        if ":" in line:
            label = line.split(":", 1)[0]
            key = re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", label).strip().lower())
            if LABELS.get(key) == attr:
                prefix = lines[i][:lines[i].index(":") + 1]
                lines[i] = f"{prefix} {value}".rstrip() + "\n"
                return "".join(lines)
    label = {v: k for k, v in LABELS.items()}[attr].capitalize()
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # insert before the blank lines that precede the next heading
    if lines and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, f"{label}: {value}".rstrip() + "\n")
    return "".join(lines)


def load(path) -> Settings:
    if not Path(path).exists():
        raise FileNotFoundError(f"No profile at {path}. Fill in the Settings page (python -m collector ui) to create one.")
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def describe(s: Settings) -> str:
    out = []
    for label, attr in LABELS.items():
        v = getattr(s, attr)
        if attr == "target_companies":
            v = "; ".join(f"{c.name} | {c.system} | {c.board}" for c in v)
        elif isinstance(v, list):
            v = f"({len(v)}) " + ", ".join(v) if v else ""
        out.append(f"  {label:<22} {v if v not in (None, '') else '<off>'}")
    out.append(f"  {'filters off':<22} {', '.join(s.filters_off()) or 'none'}")
    return "\n".join(out)
